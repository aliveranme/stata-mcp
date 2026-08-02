"""数据/回归表导出工具模块：export_excel / etable / export_delimited。

本模块在 server.py import 时通过 register(mcp, deps) 装配工具，deps 由主服务器
注入（见 register 的 deps 命名空间说明），模块自身**绝不** import server。

三个工具都写文件、可覆盖，故 readOnlyHint=False、destructiveHint=True。

统一约定（与 server.py 既有工具一致）：
- filepath/export → deps.validate_path + deps.normalize_path
- varlist         → deps.validate_varlist
- condition/in_range → deps.validate_filter_expr，经 deps.filter_clause 拼在
                      **逗号之前**（拼到逗号后 Stata 当未知选项报 r(198)）
- options         → deps.validate_no_injection，拼在逗号之后
- 校验失败一律 return deps.result_or_error(err)；错误文本以 "错误: " 开头、中文
- 导出成败以「文件是否被本次调用写入」为准（mtime 比对），不能只看文件存在

stata_export_excel(results=True) 的 estout 探测与回归表导出是**本模块最难的部分**：
探测必须持 ``deps.stata_lock()`` 在锁内执行（Stata DLL 非线程安全，且
``deps.execute_safe`` 会 drain 输出缓冲，不加锁会抢走并发命令的输出），并以
``deps.RC_RECOVERED`` / ``deps.RC_NO_OUTPUT`` 区分「崩溃已恢复」与「无输出」。
"""

import os
from typing import Any

# etable 支持的导出格式，逐一在 Stata 19.5 MP 上实测过：
# .csv 与 .rtf 报 r(198) 且不产出文件，其余九种均 rc=0 且文件真实写出。
# 必须在入口拦下不支持的格式 —— etable 会先把表格正常打印出来再报错，
# r(198) 淹没在表格输出里，用户很容易以为导出成功了。
_ETABLE_EXPORT_EXTS = frozenset(
    {".docx", ".xlsx", ".xls", ".html", ".pdf", ".tex", ".md", ".txt", ".smcl"}
)


def register(mcp: Any, deps: Any) -> dict[str, Any]:
    """在 mcp 上注册本模块的全部导出工具。

    Returns:
        {工具名: 函数} —— 供主服务器 ``globals().update()`` 暴露为模块属性，
        与 server.py 内既有 ``stata_*`` 工具保持一致（E2E 与
        ``from server import stata_export_excel`` 都依赖模块属性）。
    """

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def stata_export_excel(
        filepath: str,
        varlist: str = "",
        sheet: str = "Sheet1",
        replace: bool = False,
        results: bool = False,
        sheet_mode: str = "",
        cell: str = "",
        firstrow: str = "variables",
        nolabel: bool = False,
        condition: str = "",
        in_range: str = "",
        options: str = "",
        timeout: int = 120,
    ) -> str | deps.ToolResult:
        """将当前数据集导出为 Excel (.xlsx/.xls) 文件，或将回归结果导出为 CSV。

        使用 Stata 的 export excel 命令导出数据。
        当 results=True 时，使用 esttab 导出回归结果表；esttab 不支持 xlsx
        与 sheet() 选项，因此强制输出为 CSV（如原路径为 .xlsx，会自动改
        为 .csv 并提示）。

        Args:
            filepath: 导出路径（数据导出建议 .xlsx；回归结果导出会改为 .csv）。
            varlist: 要导出的变量列表（空格分隔），留空 = 全部变量。
                     仅用于数据导出；results=True 时 esttab 按已存储的估计结果出表，
                     该参数会被忽略。
            sheet: Excel 工作表名（默认 "Sheet1"，仅用于数据导出）。
            replace: 是否覆盖已有**文件**（默认 False）。
            results: 若为 True，将当前存储的回归结果导出为 CSV 表格而非原始数据。
            sheet_mode: 目标**工作表**已存在时的处理 —— "modify"（保留其他表，改写本表）
                        或 "replace"（清空本表重写）。留空则沿用 Stata 默认：工作表
                        已存在时报 r(602)。注意这与 ``replace``（针对整个文件）不同。
            cell: 起始单元格（左上角），如 "B3"。留空 = 从 A1 开始。
            firstrow: 首行内容 —— "variables"（默认，变量名）、"varlabels"（变量标签）
                      或 "none"（不写首行）。
            nolabel: 导出数值本身而非值标签（默认 False）。
            condition: if 条件子句（可选），如 "foreign == 1"。
            in_range: 观测范围（可选），如 "1/100"。
            options: 其余官方选项的自由文本逃生舱，如
                     ``'keepcellfmt missing("NA") datestring("%td")'``。
            timeout: 命令超时秒数（默认 120，钳制 10–1800）。长命令/大文件可显式调大。

        Returns:
            导出确认信息。
        """
        if err := deps.validate_path(filepath):
            return deps.result_or_error(err)
        if err := deps.validate_varlist(varlist, "varlist"):
            return deps.result_or_error(err)
        if err := deps.validate_sheet_name(sheet):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(condition, "condition"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(in_range, "in_range"):
            return deps.result_or_error(err)
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)
        if err := deps.validate_cell_reference(cell):
            return deps.result_or_error(err)
        if sheet_mode and sheet_mode not in ("modify", "replace"):
            return deps.make_error(
                f'错误: sheet_mode 只能是 "modify" 或 "replace"（收到 {sheet_mode!r}）'
            )
        if sheet_mode and replace and not results:
            # 实测 Stata：invalid syntax; option sheet(...,replace) may not be combined
            # with option replace。二者语义冲突 —— 文件级 replace 重建整个文件，
            # 不可能有工作表冲突；sheet_mode 则是针对已存在文件里的某张表。
            return deps.make_error(
                "错误: sheet_mode 与 replace 不能同时使用（Stata 会 r(198)）。\n"
                "  · 想重建整个文件 → 只传 replace=True\n"
                f'  · 想保留文件、改写其中一张表 → 只传 sheet_mode="{sheet_mode}"'
            )
        if firstrow not in ("variables", "varlabels", "none"):
            return deps.make_error(
                f'错误: firstrow 只能是 "variables" / "varlabels" / "none"（收到 {firstrow!r}）'
            )

        safe_timeout = max(10, min(timeout, 1800))
        export_path = deps.normalize_path(filepath)
        replace_opt = "replace" if replace else ""
        firstrow_opt = "" if firstrow == "none" else f"firstrow({firstrow})"

        if results:
            # esttab 不支持 xlsx/sheet，统一输出 CSV
            base, ext = os.path.splitext(export_path)
            if ext.lower() != ".csv":
                export_path = base + ".csv"
                if ext.lower() == ".xlsx":
                    changed_msg = f"提示：回归结果导出不支持 .xlsx/sheet()，已自动改用 CSV 路径：{export_path}\n"
                else:
                    changed_msg = f"提示：回归结果已导出为 CSV：{export_path}\n"
            else:
                changed_msg = ""

            # 前置探测 estout 是否已安装：缺失则直接报错，引导用户用
            # stata_install_package("estout") 手动安装。不在此内嵌 ssc install ——
            # 但原因不是「损坏 DLL」：那条结论已被实测推翻（Stata 19.5 MP，多场景
            # 复现无一崩溃，超时也能被 SetBreak 干净中断、包不残留半装状态）。
            # 真正的问题是它整段独占 _stata_lock：同一个包耗时在 3–13 秒间波动，
            # 慢网络更久，内嵌进分析步骤会意外冻结整个 server。故包安装独立成工具，
            # 由用户控制时机、timeout 参数真实兜底。
            #
            # 必须用裸 which，不能加 capture：capture 的语义就是吞掉命令自身的错误、
            # 只写入 _rc，实测（Stata 19.5 MP）`capture which <pkg>` 在已装与未装
            # 两种情况下一律返回 rc=0，无法区分。裸 which 已装返回 0、未装返回 111。
            #
            # 锁内执行：Stata DLL 非线程安全，且 execute_safe 会 drain 输出缓冲，
            # 不加锁会抢走并发命令的输出。_ping_stata 不持 _stata_lock，无重入风险。
            # 探测与下方 esttab 分属两段临界区（Lock 不可重入，不能跨 run_stata_command）。
            with deps.stata_lock():
                probe_rc, probe_out = deps.execute_safe(deps.ESTOUT_PROBE_CMD, timeout=20)

            if probe_rc == 998:
                # DLL 无响应：透传原始诊断（含「重启 MCP Server」指引）。误报为
                # 「未安装」会让用户去装包，而错过真正需要的恢复步骤。
                return deps.make_error(probe_out)
            if probe_rc == deps.RC_RECOVERED:
                # 崩溃已恢复、探测命令未执行：按 997 契约返回非致命提示，不标 isError。
                return probe_out.strip()
            if probe_rc not in (0, deps.RC_NO_OUTPUT):
                return deps.result_or_error(
                    "错误: 未安装 estout（esttab 所依赖），无法导出回归结果。\n"
                    "请执行这一条安装命令（联网，会阻塞几秒到十几秒）：\n"
                    '    stata_install_package("estout", source="ssc", timeout=120)\n'
                    "装好后重试本次导出即可，无需改动其他步骤。"
                )

            cmd = f'esttab using "{export_path}", csv {replace_opt} plain nogaps nomtitles nonumber'
        else:
            changed_msg = ""
            # Stata 的 export excel 在目标没有扩展名时会实际写成 ``.xlsx``；
            # 显式补上后再做 mtime/大小校验，避免把真实成功误报成「未写入」。
            export_path = deps.append_default_extension(export_path, ".xlsx")
            # 导出数据集为 Excel。[if] [in] 属于命令的另一个语法位置，必须在逗号之前。
            sheet_opt = f'sheet("{sheet}", {sheet_mode})' if sheet_mode else f'sheet("{sheet}")'
            opts = " ".join(
                p
                for p in (
                    replace_opt,
                    firstrow_opt,
                    sheet_opt,
                    f"cell({cell.strip()})" if cell.strip() else "",
                    "nolabel" if nolabel else "",
                    options.strip(),
                )
                if p
            )
            cmd = "export excel"
            if varlist.strip():
                cmd += f" {varlist.strip()}"
            cmd += f' using "{export_path}"'
            cmd += deps.filter_clause(condition, in_range)
            cmd += f", {opts}"

        # 导出成败以「文件是否被这次调用写入」为准，不能只看文件是否存在：
        # 上次运行留下的同名文件会把失败伪装成成功。实测 rc=997（崩溃已恢复、
        # 命令未执行）时，旧文件仍在，原实现回报「已导出 28 B」。
        before_ns = deps.mtime_ns(export_path) if os.path.isfile(export_path) else None

        result = deps.run_stata_command(cmd, timeout=safe_timeout)

        # 若 run_stata_command 已标记错误，透传；空选择这类误导性诊断补一句解释。
        if isinstance(result, deps.ToolResult):
            raw = result.content[0].text if result.content else ""
            if hint := deps.empty_selection_hint(raw, condition, in_range):
                return deps.make_error(raw + hint)
            return result

        if not deps.file_written_since(export_path, before_ns):
            hint = ""
            if before_ns is not None and not replace:
                hint = "\n提示：目标文件已存在且 replace=False，如需覆盖请传 replace=True。"
            return deps.make_error(
                f"错误: 导出失败，未写入文件 {export_path}{hint}\n{changed_msg}{result.strip()}"
            )

        reg_err = deps.register_resource(export_path, "stata_export_excel")
        note = "" if reg_err is None else f"\n(登记资源失败: {reg_err})"
        return (
            f"{changed_msg}已导出 {deps.format_size(export_path)} -> {export_path}\n{result}{note}"
        )

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def stata_etable(
        estimates: str = "",
        export: str = "",
        replace: bool = False,
        stars: bool = False,
        stats: str = "",
        title: str = "",
        options: str = "",
        timeout: int = 60,
    ) -> str | deps.ToolResult:
        """生成回归结果表并可导出（官方 ``etable``，Stata 17+）。

        这是「把回归表交出去」的官方路径：**不需要任何第三方包**，且能直接产出
        Word/Excel/PDF/LaTeX。对照 ``stata_export_excel(results=True)`` —— 那条路
        依赖第三方 ``estout``，且只能产出 CSV。

        典型用法：跑完多个模型各自 ``stata_estimates(action="store", name="m1")``，
        再用 ``estimates="m1 m2 m3"`` 并排成表导出。不传 ``estimates`` 时用当前
        活跃的估计结果。

        Args:
            estimates: 已存储的估计结果名（空格分隔）。留空则用当前活跃估计。
            export: 导出路径。支持 .docx / .xlsx / .xls / .html / .pdf / .tex /
                .md / .txt / .smcl（实测 .csv 与 .rtf 会 r(198)）。留空只打印。
            replace: 覆盖已存在的文件（默认 False）。
            stars: 显示显著性星号并附星号说明（``showstars showstarsnote``）。
            stats: 附加的模型统计量，空格分隔，如 "N r2 r2_a aic"
                （官方语法是每个各写一个 ``mstat()``，此处自动展开）。
            title: 表标题。
            options: 其余官方选项，如 "column(dvlabel)"、"cstat(_r_b, nformat(%9.3f))"。
            timeout: 命令超时秒数（默认 60，钳制 10–1800）。长命令/大文件可显式调大。

        Returns:
            表格文本；导出时附确认信息。
        """
        if err := deps.validate_varlist(estimates, "estimates"):
            return deps.result_or_error(err)
        if err := deps.validate_varlist(stats, "stats"):
            return deps.result_or_error(err)
        if err := deps.validate_sheet_name(title):
            return deps.result_or_error(err.replace("工作表名", "title"))
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)

        export_path = ""
        if export:
            if err := deps.validate_path(export):
                return deps.result_or_error(err)
            ext = os.path.splitext(export)[1].lower()
            if ext not in _ETABLE_EXPORT_EXTS:
                return deps.make_error(
                    f"错误: etable 不支持导出为 {ext or '<无扩展名>'}"
                    f"（实测 .csv 与 .rtf 会 r(198)）。可用: "
                    f"{', '.join(sorted(_ETABLE_EXPORT_EXTS))}"
                )
            export_path = deps.normalize_path(export)

        opts = []
        if estimates.strip():
            opts.append(f"estimates({estimates.strip()})")
        if stars:
            opts.append("showstars showstarsnote")
        # 官方语法是每个统计量各写一个 mstat()，不是 mstat(N r2)
        opts.extend(f"mstat({s})" for s in stats.split())
        if title.strip():
            opts.append(f'title("{title.strip()}")')
        if export_path:
            replace_opt = ", replace" if replace else ""
            opts.append(f'export("{export_path}"{replace_opt})')
        if options.strip():
            opts.append(options.strip())

        cmd = "etable" + (f", {' '.join(opts)}" if opts else "")

        before_ns = (
            deps.mtime_ns(export_path) if export_path and os.path.isfile(export_path) else None
        )
        safe_timeout = max(10, min(timeout, 1800))
        result = deps.run_stata_command(cmd, timeout=safe_timeout)
        if isinstance(result, deps.ToolResult) or not export_path:
            return result

        # 以文件是否被本次调用写入为准：etable 会先打印表格再报导出错误，
        # 只看输出很容易把失败当成功（与 stata_graph 同一判定思路）。
        if not deps.file_written_since(export_path, before_ns):
            hint = "" if replace else "\n提示：目标文件已存在时需传 replace=True。"
            # 实战发现：已传 replace=True 却仍报 r(602)「文件已存在」时，真实根因
            # 往往是目标目录不存在（Stata 自身怪癖：不存在目录 + replace 报 602）。
            parent = os.path.dirname(export_path)
            if replace and parent and not os.path.isdir(parent):
                hint = f"\n提示：目标目录不存在: {parent} —— 请先创建目录。"
            return deps.make_error(f"错误: 表格未能写入 {export_path}{hint}\n{result}")
        reg_err = deps.register_resource(export_path, "stata_etable")
        note = "" if reg_err is None else f"\n(登记资源失败: {reg_err})"
        return f"已导出 {deps.format_size(export_path)} -> {export_path}\n{result}{note}"

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def stata_export_delimited(
        filepath: str,
        varlist: str = "",
        delimiter: str = "",
        novarnames: bool = False,
        nolabel: bool = False,
        datafmt: bool = False,
        quote: bool = False,
        replace: bool = False,
        condition: str = "",
        in_range: str = "",
        options: str = "",
        timeout: int = 120,
    ) -> str | deps.ToolResult:
        """将当前数据集导出为分隔文本文件（CSV / TSV / 自定义分隔符）。

        对应官方的 ``export delimited``。相比 Excel，它无依赖、体积小、任何工具都能读，
        是跨程序交换数据的首选；文件名不带扩展名时 Stata 默认按 ``.csv`` 处理。

        Args:
            filepath: 导出路径（如 "out/data.csv"）。
            varlist: 要导出的变量列表（空格分隔），留空 = 全部变量。
            delimiter: 分隔符 —— 留空 = 逗号（Stata 默认）；``"tab"`` 用制表符；
                       或单个字符如 ``";"``、``"|"``。
            novarnames: 不写变量名首行（默认 False，即写）。
            nolabel: 导出数值本身而非值标签（默认 False）。
            datafmt: 按变量的显示格式导出（默认 False）。
            quote: 字符串一律用双引号包裹（默认 False，仅必要时包裹）。
            replace: 是否覆盖已有文件（默认 False）。
            condition: if 条件子句（可选）。
            in_range: 观测范围（可选），如 "1/100"。
            options: 其余官方选项的自由文本逃生舱。
            timeout: 命令超时秒数（默认 120，钳制 10–1800）。长命令/大文件可显式调大。

        Returns:
            导出确认信息。
        """
        if err := deps.validate_path(filepath):
            return deps.result_or_error(err)
        if err := deps.validate_varlist(varlist, "varlist"):
            return deps.result_or_error(err)
        # condition/in_range 拼在 `using "<路径>"` 之后，`//` 等注释记号会把已校验
        # 的路径整段注释掉 —— 必须走 filter_expr 级校验（与 export_excel 一致，
        # 实战审查发现此前漏用较弱的 no_injection）。
        for value, label in ((condition, "condition"), (in_range, "in_range")):
            if err := deps.validate_filter_expr(value, label):
                return deps.result_or_error(err)
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)

        # delimiter 拼进 delimiter("<c>")，双引号会提前闭合；tab 是关键字不加引号。
        # 实测 Stata 对 delimiter("tab") 与 delimiter(tab) 一视同仁（都产出制表符，
        # 不会把 "tab" 当三字符分隔符），此处取官方文档的无引号写法。
        delim_opt = ""
        if delimiter:
            if err := deps.validate_delimiter(delimiter):
                return deps.result_or_error(err)
            delim_opt = "delimiter(tab)" if delimiter == "tab" else f'delimiter("{delimiter}")'

        # Stata 的 export delimited 默认把无扩展名目标写成 ``.csv``。命令、
        # mtime 校验与成功提示必须使用同一个实际路径。
        export_path = deps.append_default_extension(deps.normalize_path(filepath), ".csv")
        opts = " ".join(
            p
            for p in (
                delim_opt,
                "novarnames" if novarnames else "",
                "nolabel" if nolabel else "",
                "datafmt" if datafmt else "",
                "quote" if quote else "",
                "replace" if replace else "",
                options.strip(),
            )
            if p
        )

        cmd = "export delimited"
        if varlist.strip():
            cmd += f" {varlist.strip()}"
        cmd += f' using "{export_path}"'
        cmd += deps.filter_clause(condition, in_range)
        if opts:
            cmd += f", {opts}"

        # 与 stata_graph / stata_export_excel 一致：以文件是否被本次调用写入为准。
        # 只判断「文件存在」会把上次留下的同名文件当成本次成功。
        before_ns = deps.mtime_ns(export_path) if os.path.isfile(export_path) else None

        safe_timeout = max(10, min(timeout, 1800))
        result = deps.run_stata_command(cmd, timeout=safe_timeout)
        if isinstance(result, deps.ToolResult):
            raw = result.content[0].text if result.content else ""
            if hint := deps.empty_selection_hint(raw, condition, in_range):
                return deps.make_error(raw + hint)
            return result

        if not deps.file_written_since(export_path, before_ns):
            hint = ""
            if before_ns is not None and not replace:
                hint = "\n提示：目标文件已存在且 replace=False，如需覆盖请传 replace=True。"
            return deps.make_error(
                f"错误: 导出失败，未写入文件 {export_path}{hint}\n{result.strip()}"
            )

        reg_err = deps.register_resource(export_path, "stata_export_delimited")
        note = "" if reg_err is None else f"\n(登记资源失败: {reg_err})"
        return f"已导出 {deps.format_size(export_path)} -> {export_path}\n{result}{note}"

    # 收集本模块的工具函数（只认 `stata_` 前缀的可调用局部变量）
    return {k: v for k, v in locals().items() if k.startswith("stata_") and callable(v)}
