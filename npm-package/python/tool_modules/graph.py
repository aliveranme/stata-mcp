"""图形工具模块：stata_graph / stata_scheme。

本模块在 server.py import 时通过 register(mcp, deps) 装配工具，deps 由主服务器
注入（见 register 的 deps 命名空间说明），模块自身**绝不** import server。

stata_graph 是图形工具族里依赖最重的：
- command 是自由文本，会被原样拼进执行串（导出模式下还会进入临时 do 文件），
  因此必须与 stata_run 走同一层护栏（deps.precheck_command，对**解析后的块**
  逐块检查 —— `sh/*x*/ell …` 在原始文本里不含 shell 一词）；
- 导出模式把 graph + export 包进 { } 复合块原子执行，复合块的 ``capture`` 会
  吞掉错误（rc 恒为 0），成败改以「文件是否被本次调用写入」为准（mtime 比对），
  与 stata_graph 的 command 里不能出现会破坏复合块的裸 ``}``（deps.has_unsafe_brace）；
- 尺寸/格式选项按扩展名适配（deps.graph_size_options / deps.graph_format_options），
  不适用的选项**先丢弃再提示** —— 传给 Stata 会 r(198)，而 capture 让导出无声失败；
- 导出成功后经 deps.register_resource 登记为 MCP 资源。

stata_scheme 只查/切主题：scheme 名用正向白名单（deps.validate_scheme_name），
`set scheme` 支持逗号后选项，黑名单漏掉 `,` 就能改变命令语义。

统一约定（与 server.py 既有工具一致）：
- scheme / fontface → deps.validate_scheme_name / deps.validate_fontface
- export → deps.validate_path + deps.normalize_path；文件是否存在经
  ``os.path.isfile``（与 export.py 一致，测试的 ``patch("server.os.path.isfile")``
  打在共享的 os 模块属性上，仍能截获）
- 校验失败一律 return deps.result_or_error(err)；错误文本以 "错误: " 开头、中文
"""
import os
from typing import Any


def register(mcp: Any, deps: Any) -> dict[str, Any]:
    """在 mcp 上注册本模块的全部图形工具。

    Returns:
        {工具名: 函数} —— 供主服务器 ``globals().update()`` 暴露为模块属性，
        与 server.py 内既有 ``stata_*`` 工具保持一致（E2E 与
        ``from server import stata_graph`` 都依赖模块属性）。
    """

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def stata_graph(
        command: str,
        scheme: str = "",
        export: str = "",
        width: int = 800,
        height: int = 0,
        replace: bool = False,
        quality: int = 0,
        mag: int = 0,
        fontface: str = "",
        timeout: int = 120,
    ) -> str | deps.ToolResult:
        """生成 Stata 图形并可选导出为文件。

        当指定 export 时,使用 { } 复合块将 graph + export 包装在单次
        StataSO_Execute 调用中,避免图形窗口在 headless 环境中丢失。

        **不适用于目标格式的选项会被丢弃并在返回信息中说明** —— 传给 Stata 会
        r(198)，而复合块的 capture 会吞掉错误，表现为导出无声失败。

        官方支持的后缀（[G-2] graph export）：ps eps svg emf pdf png tif gif jpg。
        可用性依运行环境而变：emf 仅 Windows、gif 仅 Mac GUI、tif 不支持 console 模式；
        本 MCP 以 headless console 运行，实测仅 png/jpg/pdf/svg/eps/ps 可用。

        Args:
            command: 图形命令(scatter mpg weight, histogram price 等)。
            scheme: 图形方案。**留空（默认）= 不改变当前 scheme**，沿用 Stata 或用户
                    的设置（Stata 19 默认为 stcolor）。传值才会 `set scheme`，且不会
                    在调用后还原。用 stata_scheme() 可列出全部可用方案。
            export: 导出图形文件路径（留空不导出）；Stata 按扩展名推断格式。
            width: 导出宽度（默认 800）。单位随格式而变：.png/.jpg/.tif/.gif 与 .svg
                   是**像素**（位图 8–16000）；.pdf 是**英寸**（0.5–20）；
                   .eps/.ps/.emf/.wmf **不支持**尺寸选项。
            height: 导出高度，单位同 width（默认 0 表示不指定）。
            replace: 是否覆盖已有文件(默认 False)。
            quality: JPEG 压缩质量 1–100（默认 0 = 不指定，Stata 默认 90）。**仅 .jpg**。
            mag: 缩放百分比 1–10000（默认 0 = 不指定，Stata 默认 100）。
                 **仅 .pdf/.eps/.ps**。
            fontface: 默认字体名（默认空 = 不指定）。**仅 .pdf/.eps/.ps/.svg**。
            timeout: 命令超时秒数（默认 120，钳制 10–1800）。长命令/大文件可显式调大。

        Returns:
            图形生成确认信息。
        """
        try:
            if "\x00" in command or "\n" in command or "\r" in command:
                return deps.make_error("错误: command 包含非法控制字符")
            # command 是自由文本，会被原样拼进要执行的命令串（导出模式下还会进入
            # 临时 do 文件），因此必须与 stata_run 走同一层护栏 —— 实测
            # stata_graph(command='!touch /tmp/x') 曾能真实创建文件。
            # 同样要校验解析后的块：`sh/*x*/ell …` 在原始文本里不含 shell 一词。
            if reason := deps.precheck_command(command):
                return deps.make_error(reason)
            if scheme and (err := deps.validate_scheme_name(scheme)):
                return deps.result_or_error(err)
            if fontface and (err := deps.validate_fontface(fontface)):
                return deps.result_or_error(err)
            # 负值会被原样拼成 width(-100) 交给 Stata；实测虽因图形命令先失败而未暴露，
            # 但语义上无意义，应在入口拒绝而不是依赖下游偶然报错。
            for label, value in (
                ("width", width),
                ("height", height),
                ("quality", quality),
                ("mag", mag),
            ):
                if value < 0:
                    return deps.make_error(f"错误: {label} 不能为负数（{value}）")
            safe_timeout = max(10, min(timeout, 1800))
            if export:
                if err := deps.validate_path(export):
                    return deps.result_or_error(err)
                if deps.has_unsafe_brace(command):
                    return deps.make_error(
                        "错误: graph command 中包含会破坏复合块的 '}'，"
                        "请避免在 command 中使用未转义的右花括号（字符串内除外）"
                    )

            # scheme 留空时不发 `set scheme` —— 那会把用户当前的主题（Stata 19 默认
            # 是 stcolor）悄悄改掉且不还原，是覆盖而非设定。
            scheme_line = f"set scheme {scheme}\n" if scheme else ""

            if not export:
                return deps.run_stata_command(f"{scheme_line}{command}", timeout=safe_timeout)

            # 导出模式：使用 { } 复合块确保 graph + export 原子执行
            export_path = deps.normalize_path(export)
            replace_opt = "replace" if replace else ""
            size_opts, size_note = deps.graph_size_options(export_path, width, height)
            fmt_opts, fmt_note = deps.graph_format_options(export_path, quality, mag, fontface)
            export_opts = " ".join(p for p in (replace_opt, size_opts, fmt_opts) if p)

            # 复合块内的错误被 capture 吞掉（rc 恒为 0），无法据此判断成败；
            # 改以「文件是否被这次调用新写入」为准，故先记录调用前的状态。
            # 只看文件存在与否不够：replace=False 且目标已存在时 Stata 会拒绝写入，
            # 而文件依旧在，会被误判成功。
            before_ns = deps.mtime_ns(export_path) if os.path.isfile(export_path) else None

            compound = (
                f"capture noisily {{\n"
                f"    set graphics off\n"
                f"{'    ' + scheme_line if scheme_line else ''}"
                f"    {command}\n"
                f'    graph export "{export_path}", {export_opts}\n'
                f"}}\n"
                # 只清匿名图，不能 `graph drop _all`：具名图正是「我要在后续命令里
                # 引用它」的显式表达，而 _all 会把它们一起摧毁 —— combine 出一张图
                # 导出后，再换个布局导出第二张就会发现源图已经没了。
                # 真机确认（Stata 19.5 MP）：匿名图名为 `Graph`（`graph combine` 的
                # 结果同样叫 `Graph`），`graph drop Graph` 只删它、具名图存活。
                # 匿名图不会累积 —— 每次绘图都覆盖同名的那一个。
                f"capture noisily graph drop Graph"
            )

            result = deps.run_stata_command(compound, timeout=safe_timeout)

            # 若 run_stata_command 已标记错误，直接透传，不追加成功提示
            if isinstance(result, deps.ToolResult):
                return result

            # 以文件是否被本次调用写入为准，而非 rc —— capture 已把块内错误吞掉。
            if not deps.file_written_since(export_path, before_ns) or not deps.file_is_nonempty(
                export_path
            ):
                hint = ""
                if before_ns is not None and not replace:
                    hint = "\n提示：目标文件已存在且 replace=False，如需覆盖请传 replace=True。"
                parent = os.path.dirname(export_path)
                if parent and not os.path.isdir(parent):
                    hint += f"\n提示：目标目录不存在: {parent} —— 请先创建目录。"
                # 实战发现：真实原因（Stata 输出，如 variable not found）被埋在「文件为空」
                # 之后。Stata 原因放最前，结论在后，Agent 第一眼就看到根因。
                return deps.make_error(
                    f"图形导出失败：\n{result.strip()}\n(未生成文件或文件为空 {export_path}){hint}"
                )

            result += f"\n(图形已导出: {export_path}, {deps.format_size(export_path)})"
            reg_err = deps.register_resource(export_path, "stata_graph")
            if reg_err is None:
                result += f"\n(已登记为资源: {deps.resource_uri(export_path)})"
            else:
                result += f"\n(登记资源失败: {reg_err})"
            for note in (size_note, fmt_note):
                if note:
                    result += f"\n{note}"
            return result

        except Exception as e:
            return deps.make_error(f"图形生成失败: {type(e).__name__}: {e}")

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def stata_scheme(
        action: str = "list",
        scheme: str = "",
        permanently: bool = False,
        timeout: int = 60,
    ) -> str | deps.ToolResult:
        """查询或设置 Stata 图形主题（scheme）。

        scheme 决定配色、字体、坐标轴与图例的整体外观。Stata 19 的默认是 ``stcolor``
        （实测 ``c(scheme)``），本机内置 26 个方案；``ssc install`` 的第三方方案
        （cleanplots、plottig、schemepack 等）装好后也会出现在列表里。

        Args:
            action: ``list``（默认，列出全部可用方案）/ ``get``（当前方案）/
                    ``set``（切换方案）。
            scheme: 方案名，仅 action="set" 时必填。
            permanently: 是否写入 Stata 配置、跨会话保留（默认 False，仅本会话生效）。
            timeout: 命令超时秒数（默认 60，钳制 10–1800）。长命令/大文件可显式调大。

        Returns:
            方案清单、当前方案名，或设置确认。
        """
        if action not in ("list", "get", "set"):
            return deps.make_error(
                f'错误: action 只能是 "list" / "get" / "set"（收到 {action!r}）'
            )

        safe_timeout = max(10, min(timeout, 1800))
        if action == "list":
            # 官方查询命令；ssc 没有对应子命令，`graph query, schemes` 是唯一入口。
            return deps.run_stata_command("graph query, schemes", timeout=safe_timeout)

        if action == "get":
            # 不能用裸 `set scheme` 查询 —— 那是设置命令，不带参数时行为不同。
            return deps.run_stata_command("display c(scheme)", timeout=safe_timeout)

        if not scheme.strip():
            # 空值会拼出裸 `set scheme`，改变命令语义而非报错。
            return deps.make_error('错误: action="set" 时必须提供 scheme 名')
        if err := deps.validate_scheme_name(scheme):
            return deps.result_or_error(err)

        suffix = ", permanently" if permanently else ""
        return deps.run_stata_command(
            f"set scheme {scheme.strip()}{suffix}", timeout=safe_timeout
        )

    # 收集本模块的工具函数（只认 `stata_` 前缀的可调用局部变量）
    return {k: v for k, v in locals().items() if k.startswith("stata_") and callable(v)}
