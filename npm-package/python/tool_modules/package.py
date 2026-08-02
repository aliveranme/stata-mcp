"""包管理工具模块：install / uninstall / describe / find / list / help。

本模块在 server.py import 时通过 register(mcp, deps) 装配工具，deps 由主服务器
注入（见 register 的 deps 命名空间说明），模块自身**绝不** import server。

六个工具对应 Stata 的扩展包生命周期与官方帮助：
- install / uninstall：`ssc install` / `net install … from()` / `ado uninstall`。
  **install 是唯一的联网安装入口**（实测同一包 3–13s 波动，慢网络更久），执行期间
  独占串行锁、冻结整个 server，因此 docstring 明确要求单独调用；``timeout`` 是真实
  兜底，超时时看门狗干净中断、包不残留半装状态，并统一钳制在 10–1800s。
- describe：``ado describe``（本地，~12ms）或 ``ssc describe``（联网，1–7s）二选一，
  source 经 deps.validate_install_source 白名单校验，本地/联网各自独立成工具语义。
- find：``net search``（联网，0.6–2s），覆盖官方全部 scope 选项与 or/nosj/errnone。
- list：``ado dir`` 而非 ``ado describe`` —— 后者会吐出每个包的完整文档（实测 49K
  字符），前者 4.3K 字符即给出同样的包清单。
- help：headless 环境下把 SMCL 帮助渲染为纯文本（实测可用），覆盖全部内置命令。
  帮助主题只允许字母/数字/下划线/空格（多词子主题，如 "xtreg postestimation"），
  ``_HELP_TOPIC_RE`` 的白名单把 ! ; 换行 反引号 $ 引号 ( ) 等注入字符一律拒掉，
  杜绝把第二条命令拼进 help。分页通过 ``page`` 参数透传给 deps.run_stata_command
  （分页逻辑在主服务器内部完成），本模块不直接调用分页助手。

统一约定（与 server.py 既有工具一致）：
- 校验失败一律 return deps.result_or_error(err)；错误文本以 "错误: " 开头、中文
- 工具经 deps.run_stata_command 执行，patch("server._run_stata_command") 依然截获
"""
import re
from typing import Any

# 包详情来源白名单：本地已装 vs 联网查 SSC
_DESCRIBE_SOURCES = {"installed", "ssc"}

# 帮助主题：命令名 + 可选的多词子主题（如 "xtreg postestimation"、"estat firststage"）。
# 仅允许字母/数字/下划线/空格 —— 命令名与手册主题的全部合法字符都在此集内，
# 而 ! ; 换行 反引号 $ 引号 ( ) 等注入字符一律被拒，杜绝把第二条命令拼进 help。
_HELP_TOPIC_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_ ]{0,63}$")


def register(mcp: Any, deps: Any) -> dict[str, Any]:
    """在 mcp 上注册本模块的全部包管理工具。

    Returns:
        {工具名: 函数} —— 供主服务器 ``globals().update()`` 暴露为模块属性，
        与 server.py 内既有 ``stata_*`` 工具保持一致（E2E 与
        ``from server import stata_install_package`` 都依赖模块属性）。
    """

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def stata_install_package(
        package: str, source: str = "ssc", replace: bool = False, timeout: int = 300
    ) -> str | deps.ToolResult:
        """安装 Stata 扩展包（联网）。

        从 ssc 或完整 from() URL 安装 Stata 包。支持 replace 解决版本冲突。

        **这是唯一的联网安装入口，请单独调用、装完再继续原任务**：`ssc install`
        是网络阻塞调用（实测同一包 3–13s 波动，慢网络更久），执行期间独占串行锁、
        冻结整个 server。不要把 `ssc install` 内嵌进 `stata_run` 的分析步骤里。

        ``timeout`` 是真实兜底：安装超过它时看门狗会**干净中断**（实测超时的
        `ssc install` 被 break 后会话健康、包不残留半装状态），返回超时提示而非
        卡死。下限受 `stata_run` 约束为 10s；慢网络下装大包建议 120–300s。

        Args:
            package: 包名称（如 "outreg2"、"estout"、"ivreg2"）。
            source: 安装源 — "ssc"（默认）或完整的 from() URL。
                    例："https://fmwww.bc.edu/RePEc/bocode/o"
            replace: 是否强制替换已有文件（解决版本冲突，默认 False）。
            timeout: 安装超时秒数（默认 300）。超时则中断并提示，不会卡死会话。

        Returns:
            安装过程输出。
        """
        if err := deps.validate_identifier(package, "package", required=True):
            return deps.result_or_error(err)
        if err := deps.validate_install_source(source):
            return deps.result_or_error(err)
        replace_opt = ", replace" if replace else ""
        src_lower = source.lower().strip()
        if src_lower == "ssc":
            cmd = f"ssc install {package}{replace_opt}"
        else:
            cmd = f"net install {package}{replace_opt}, from({source.strip()})"
        # 与 stata_run / stata_run_do_file 一致地钳制 —— 此前完全未钳制：timeout=1
        # 会架起 1 秒看门狗（而 ssc install 实测需 3–13 秒，必然被 break），
        # timeout=10**6 则突破 docstring 与 CLAUDE.md 所述的 1800 秒上限。
        return deps.run_stata_command(cmd, timeout=max(10, min(timeout, 1800)))

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def stata_uninstall_package(package: str) -> str | deps.ToolResult:
        """卸载一个已安装的 Stata 扩展包（删除其 ado 文件）。

        与 ``stata_install_package`` 对称，补全包的安装/卸载生命周期。执行
        ``ado uninstall <package>``，这是**纯本地**操作（只删文件，不联网），
        实测约 20ms，不存在 SSC 网络请求卡死 DLL 的风险。

        包未安装时 Stata 返回 r(111) ``package not found``。不确定包名时先用
        ``stata_list_packages`` 查已装清单。

        Args:
            package: 要卸载的包名（须与 ``stata_list_packages`` 列出的名称一致）。

        Returns:
            卸载确认信息。
        """
        if err := deps.validate_identifier(package, "package", required=True):
            return deps.result_or_error(err)
        return deps.run_stata_command(f"ado uninstall {package}")

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def stata_describe_package(package: str, source: str = "installed") -> str | deps.ToolResult:
        """查看某个扩展包的详情（作者、功能、包含的文件）。

        两种来源：
        - ``source="installed"``（默认）：``ado describe <package>``，**本地**读取
          已安装包的信息，实测约 12ms，无网络风险。包未安装则报错。
        - ``source="ssc"``：``ssc describe <package>``，**联网**查询 SSC 存档，可在
          安装**前**了解一个包（实测约 1–7s）。网络不可达时会等到超时。

        安装决策流程：``stata_find_package`` 搜索 → ``stata_describe_package(pkg,
        source="ssc")`` 看详情 → ``stata_install_package`` 安装。

        Args:
            package: 包名。
            source: "installed"（本地已装，默认）或 "ssc"（联网查 SSC）。

        Returns:
            包详情文本。
        """
        if err := deps.validate_identifier(package, "package", required=True):
            return deps.result_or_error(err)
        src = source.strip().lower()
        if src not in _DESCRIBE_SOURCES:
            return deps.make_error(
                f"错误: source 只能是 {', '.join(sorted(_DESCRIBE_SOURCES))} 之一，收到 '{source}'"
            )
        if src == "ssc":
            return deps.run_stata_command(f"ssc describe {package}", timeout=120)
        return deps.run_stata_command(f"ado describe {package}")

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def stata_find_package(
        keyword: str,
        scope: str = "",
        match_any: bool = False,
        exclude_sj: bool = False,
        error_if_none: bool = False,
        options: str = "",
    ) -> str | deps.ToolResult:
        """搜索可安装的 Stata 扩展包（联网）。

        使用 ``net search``，覆盖 SSC 与 Stata Journal 等 net 资源，返回包名、
        来源 URL 与简介；拿到包名后用 ``stata_describe_package`` 看详情、
        ``stata_install_package`` 安装。

        访问 www.stata.com，实测单次 0.6–2 秒。**宽泛的多词查询输出很大** ——
        实测 "difference in differences" 默认返回 94K 字符（24 页），用
        ``scope="toc"`` 可收窄到 12K。仅搜本机已装帮助用
        ``stata_run("search <词>, local")``。

        Args:
            keyword: 搜索关键词，可多词（默认要求**全部**命中）。
            scope: 搜索范围 —— ``toc``（只搜目录，最省输出）/ ``pkg``（只搜包）/
                ``tocpkg``（默认，两者都搜）/ ``everywhere`` / ``filenames``。
            match_any: 命中**任一**关键词即可（官方 ``or`` 选项）。
                **实测显著变慢**：同一查询默认 2.3s，加 or 后 30s。
            exclude_sj: 排除 Stata Journal 来源，只看 SSC 等（官方 ``nosj``）。
            error_if_none: 无匹配时返回错误结果而非普通文本（官方 ``errnone``，
                rc=111）。默认 False —— 搜不到东西本身不是错误。
            options: 其余官方选项的自由文本逃生舱。

        Returns:
            匹配的包列表及简要描述。
        """
        if err := deps.validate_no_injection(keyword, "keyword"):
            return deps.result_or_error(err)
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)
        if not keyword.strip():
            return deps.make_error("错误: 请提供搜索关键词。")
        if scope and scope not in ("toc", "pkg", "tocpkg", "everywhere", "filenames"):
            return deps.make_error(
                '错误: scope 只能是 "toc" / "pkg" / "tocpkg" / "everywhere" / '
                f'"filenames"（收到 {scope!r}）'
            )

        opts = " ".join(
            p for p in (
                scope,
                "or" if match_any else "",
                "nosj" if exclude_sj else "",
                "errnone" if error_if_none else "",
                options.strip(),
            ) if p
        )
        cmd = f"net search {keyword.strip()}"
        if opts:
            cmd += f", {opts}"
        return deps.run_stata_command(cmd, timeout=120)

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def stata_list_packages() -> str | deps.ToolResult:
        """列出当前已安装的所有 Stata 扩展包（包名 + 一句简介）。

        用 ``ado dir`` 而非 ``ado describe``：后者会把每个包的完整文档全文吐出来，
        实测本机 49516 字符 / 13 页，而 ``ado dir`` 只要 4330 字符就给出同样的包
        清单。需要某个包的详情时再用 ``stata_run("ado describe <包名>")``。

        Returns:
            已安装包列表。
        """
        return deps.run_stata_command("ado dir", timeout=120)

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def stata_help(command: str, page: int = 1) -> str | deps.ToolResult:
        """查询任意 Stata 命令的官方帮助文档（语法、选项、示例）。

        ``help`` 在 headless 环境下把 SMCL 帮助渲染为纯文本返回（实测可用，不会
        卡在图形查看器），因此本工具**覆盖全部内置命令**（3500+）以及任何已安装的
        外置命令 —— 需要某条命令的权威语法时，先用它查，而不是凭记忆拼命令。

        支持多词主题：
        - 命令：``stata_help("xtreg")``、``stata_help("reghdfe")``
        - 后估计：``stata_help("regress postestimation")``
        - 子命令：``stata_help("estat firststage")``

        帮助文档常常很长，超过阈值会自动分页；用 ``page`` 翻页，或随后调用
        ``stata_more(page=N)``。找不到命令时返回 Stata 的「help for X not found」
        提示（不报错），可改用 ``stata_find_package`` 联网搜索可安装的包。

        Args:
            command: 命令名或帮助主题（可含空格分隔的子主题）。
            page: 分页页码（默认第 1 页）。

        Returns:
            该命令的帮助文本（可能分页）。
        """
        topic = command.strip()
        if not topic:
            return deps.make_error("错误: 请提供要查询的命令名。")
        if not _HELP_TOPIC_RE.match(topic):
            return deps.make_error(
                "错误: 命令名只能包含字母、数字、下划线和空格（用于子主题，"
                "如 'xtreg postestimation'）。含其他字符的帮助请用 stata_run 查询。"
            )
        return deps.run_stata_command(f"help {topic}", page=page, timeout=30)

    # 收集本模块的工具函数（只认 `stata_` 前缀的可调用局部变量）
    return {k: v for k, v in locals().items() if k.startswith("stata_") and callable(v)}
