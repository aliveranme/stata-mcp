"""数据探索工具模块：describe / summarize / list / codebook / tabulate / display /
correlate / return_list。

本模块在 server.py import 时通过 register(mcp, deps) 装配工具，deps 由主服务器
注入（见 register 的 deps 命名空间说明），模块自身**绝不** import server。

八个工具都是**只读探索**——不改动数据集、不落盘，故一律 readOnlyHint=True、
destructiveHint=False。

统一约定（与 server.py 既有数据探索工具一致）：
- varlist  → deps.validate_varlist（留空 = 全部变量）
- condition/in_range → deps.validate_filter_expr，经 deps.filter_clause 拼在
                      **逗号之前**（拼到逗号后 Stata 当未知选项报 r(198)）
- options  → deps.validate_no_injection，拼在逗号之后
- 校验失败一律 return deps.result_or_error(err)；错误文本以 "错误: " 开头、中文
- **stata_list 的 in 由它自己的 in_range/n 逻辑负责**，只把 condition 交给
  filter_clause —— 否则会拼出 `list … in 1/20 in 1/20`
"""
from typing import Any


def register(mcp: Any, deps: Any) -> dict[str, Any]:
    """在 mcp 上注册本模块的全部数据探索工具。

    Returns:
        {工具名: 函数} —— 供主服务器 ``globals().update()`` 暴露为模块属性，
        与 server.py 内既有 ``stata_*`` 工具保持一致（E2E 与
        ``from server import stata_xxx`` 都依赖模块属性）。
    """

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def stata_describe(
        varlist: str = "", simple: bool = False, options: str = ""
    ) -> str | deps.ToolResult:
        """描述当前数据集的变量信息。

        显示变量名、存储类型、显示格式、变量标签和值标签。
        使用 simple=True 可获得更精简的输出。

        Args:
            varlist: 要描述的变量（空格分隔），留空 = 全部变量。
            simple: 是否使用精简模式（默认 False）。

        Returns:
            变量描述信息表。
        """
        if err := deps.validate_varlist(varlist, "varlist"):
            return deps.result_or_error(err)
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)
        cmd = f"describe {varlist}".strip()
        opts = " ".join(p for p in ("simple" if simple else "", options.strip()) if p)
        if opts:
            cmd += f", {opts}"
        return deps.run_stata_command(cmd)

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def stata_summarize(
        varlist: str = "",
        detail: bool = False,
        condition: str = "",
        in_range: str = "",
        options: str = "",
    ) -> str | deps.ToolResult:
        """计算变量的摘要统计量。

        包括观测数、均值、标准差、最小值、最大值。
        使用 detail=True 可获得百分位数、偏度、峰度等。

        Args:
            varlist: 变量列表（空格分隔），留空 = 全部变量。
            detail: 是否显示详细统计量（默认 False）。
            condition: if 条件子句（可选）。例："!missing(price) & foreign == 1"。

        Returns:
            摘要统计量表格。
        """
        if err := deps.validate_varlist(varlist, "varlist"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(condition, "condition"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(in_range, "in_range"):
            return deps.result_or_error(err)
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)
        cmd = f"summarize {varlist}".strip()
        cmd += deps.filter_clause(condition, in_range)
        opts = " ".join(p for p in ("detail" if detail else "", options.strip()) if p)
        if opts:
            cmd += f", {opts}"
        return deps.run_stata_command(cmd)

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def stata_list(
        varlist: str = "",
        n: int = 10,
        in_range: str = "",
        condition: str = "",
        options: str = "",
    ) -> str | deps.ToolResult:
        """列出当前数据集中的数据值。

        以表格形式展示观测数据。默认显示前 10 条。

        **``n`` 与 ``condition`` 同时给出时要小心**：二者拼成
        ``list … if <condition> in 1/<n>``，Stata 的语义是「**前 n 条观测里**满足
        条件的」，而不是「满足条件的前 n 条」。若筛选结果稀疏，很容易得到空表并
        误以为没有匹配数据。想看「满足条件的前 n 条」，请传 ``n=0`` 取全部后翻页，
        或先用 ``stata_tabulate`` / ``stata_summarize`` 确认规模。

        Args:
            varlist: 要列出的变量（空格分隔），留空 = 全部。
            n: 显示前 n 条观测（默认 10，设为 0 显示全部，慎用）。与 condition
                叠加时的语义见上。
            in_range: 观测范围如 "1/20" 或 "1/l"。给出时优先于 n。
            condition: if 条件子句（可选）。
            options: 额外的官方选项，如 "noobs clean"、"separator(0)"、"abbreviate(12)"。

        Returns:
            数据表格。
        """
        if err := deps.validate_varlist(varlist, "varlist"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(condition, "condition"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(in_range, "in_range"):
            return deps.result_or_error(err)
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)
        if n < 0:
            return deps.make_error("错误: n 不能为负数")
        cmd = "list"
        if varlist.strip():
            cmd += f" {varlist}"
        # in 子句由下面的 in_range/n 逻辑负责，故这里只交 condition 给 _filter_clause，
        # 否则会拼出 `list … in 1/20 in 1/20`。
        cmd += deps.filter_clause(condition, "")
        if in_range.strip():
            cmd += f" in {in_range.strip()}"
        elif n > 0:
            cmd += f" in 1/{n}"
        if options.strip():
            cmd += f", {options.strip()}"
        return deps.run_stata_command(cmd)

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def stata_codebook(
        varlist: str = "",
        compact: bool = False,
        condition: str = "",
        in_range: str = "",
        options: str = "",
    ) -> str | deps.ToolResult:
        """生成数据集的 Codebook（变量字典）。

        显示变量标签、值标签、缺失值、分布信息等。
        比 describe 更详细。

        Args:
            varlist: 变量列表（空格分隔），留空 = 全部变量。
            compact: 是否使用紧凑模式（默认 False）。
            condition: if 条件子句（可选）。

        Returns:
            Codebook 报告。
        """
        if err := deps.validate_varlist(varlist, "varlist"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(condition, "condition"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(in_range, "in_range"):
            return deps.result_or_error(err)
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)
        cmd = f"codebook {varlist}".strip()
        cmd += deps.filter_clause(condition, in_range)
        opts = " ".join(p for p in ("compact" if compact else "", options.strip()) if p)
        if opts:
            cmd += f", {opts}"
        return deps.run_stata_command(cmd)

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def stata_tabulate(
        varname: str,
        byvar: str = "",
        chi2: bool = False,
        condition: str = "",
        in_range: str = "",
        options: str = "",
    ) -> str | deps.ToolResult:
        """创建频数分布表或交叉表。

        单变量：频数分布表。双变量：二维交叉表，可选卡方检验。

        Args:
            varname: 主变量名。
            byvar: 可选的第二个变量，用于交叉表。
            chi2: 是否显示卡方检验结果（默认 False）。
            condition: if 条件子句（可选）。

        Returns:
            频数/交叉表。
        """
        if not varname.strip():
            return deps.make_error("错误：请提供至少一个变量名。")
        if err := deps.validate_identifier(varname, "varname", required=True):
            return deps.result_or_error(err)
        if err := deps.validate_identifier(byvar, "byvar"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(condition, "condition"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(in_range, "in_range"):
            return deps.result_or_error(err)
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)
        cmd = f"tabulate {varname}"
        if byvar.strip():
            cmd += f" {byvar}"
        cmd += deps.filter_clause(condition, in_range)
        # chi2 是 twoway 专属选项，单变量表传了会 r(198)
        opts = " ".join(
            p for p in ("chi2" if (byvar.strip() and chi2) else "", options.strip()) if p
        )
        if opts:
            cmd += f", {opts}"
        return deps.run_stata_command(cmd)

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def stata_display(expression: str) -> str | deps.ToolResult:
        """计算并显示 Stata 表达式的结果。

        可用于简单计算、宏展开、返回值查看。
        适合查看 r(mean)、e(N)、e(r2) 等存储结果。

        Args:
            expression: Stata 表达式，如 "2+2"、"r(mean)"、"e(r2)"。

        Returns:
            表达式计算结果。
        """
        if err := deps.validate_no_injection(expression, "expression"):
            return deps.result_or_error(err)
        return deps.run_stata_command(f"display {expression}")

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def stata_correlate(
        varlist: str = "",
        pairwise: bool = False,
        options: str = "",
        condition: str = "",
        in_range: str = "",
    ) -> str | deps.ToolResult:
        """计算相关系数矩阵。

        Args:
            varlist: 变量列表（空格分隔），留空 = 全部变量。
            pairwise: True 用 ``pwcorr``（成对删除缺失，可配 sig/star 选项）；
                False 用 ``correlate``（列表删除缺失，默认）。
            options: 额外选项。pwcorr 支持 "sig"、"star(.05)"、"bonferroni"；
                correlate 支持 "covariance"（改报协方差）等。
            condition: if 条件子句（可选）。

        Returns:
            相关系数矩阵。
        """
        if err := deps.validate_varlist(varlist, "varlist"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(condition, "condition"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(in_range, "in_range"):
            return deps.result_or_error(err)
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)
        base = "pwcorr" if pairwise else "correlate"
        cmd = base
        if varlist.strip():
            cmd += f" {varlist}"
        cmd += deps.filter_clause(condition, in_range)
        if options.strip():
            cmd += f", {options}"
        return deps.run_stata_command(cmd)

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def stata_return_list(kind: str = "r") -> str | deps.ToolResult:
        """一次列出全部返回值（``return`` / ``ereturn`` / ``creturn list``）。

        比 ``stata_display("r(mean)")`` 逐个取高效得多 —— Agent 通常先看有哪些值
        再决定取哪个。

        Args:
            kind: ``r``（默认，``r()``：summarize/tabulate 等一般命令的返回值）、
                ``e``（``e()``：估计命令的结果，如 e(N)、e(r2)、e(b)）、
                ``c``（``c()``：系统常量与设置，如 c(pwd)、c(N)、c(scheme)）。

        Returns:
            返回值清单（名称 = 值）。
        """
        prefix = {"r": "return", "e": "ereturn", "c": "creturn"}.get(kind)
        if not prefix:
            return deps.make_error(
                f'错误: kind 只能是 "r" / "e" / "c"（收到 {kind!r}）'
            )
        return deps.run_stata_command(f"{prefix} list")

    # 收集本模块的工具函数（只认 `stata_` 前缀的可调用局部变量）
    return {k: v for k, v in locals().items() if k.startswith("stata_") and callable(v)}
