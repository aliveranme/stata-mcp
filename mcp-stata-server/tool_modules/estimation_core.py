"""核心估计工具模块：regress / logistic / ttest / probit / poisson / xtreg / ivregress。

本模块在 server.py import 时通过 register(mcp, deps) 装配工具，deps 由主服务器
注入（见 register 的 deps 命名空间说明），模块自身**绝不** import server。

七个工具都是「读估计」——运行估计、不改动数据集、不落盘，故一律
readOnlyHint=True、destructiveHint=False。

统一约定（与 server.py 既有估计工具一致）：
- depvar        → deps.validate_identifier(required=True)，必填
- indepvars     → deps.validate_varlist
- condition/in_range → deps.validate_filter_expr，经 deps.filter_clause 拼在
                      **逗号之前**（拼到逗号后 Stata 当未知选项报 r(198)）
- options       → deps.validate_no_injection，拼在逗号之后
- 校验失败一律 return deps.result_or_error(err)；错误文本以 "错误: " 开头、中文
- 估计类工具统一 timeout=60
"""
from typing import Any

# 面板估计量白名单：作为 xtreg 的选项拼接，用正向白名单杜绝注入
_XTREG_EFFECTS = {"fe", "re", "be", "mle", "pa"}

# IV 估计量白名单
_IVREGRESS_ESTIMATORS = {"2sls", "liml", "gmm"}


def register(mcp: Any, deps: Any) -> dict[str, Any]:
    """在 mcp 上注册本模块的全部核心估计工具。

    Returns:
        {工具名: 函数} —— 供主服务器 ``globals().update()`` 暴露为模块属性，
        与 server.py 内既有 50 个 ``stata_*`` 工具保持一致（E2E 与
        ``from server import stata_xxx`` 都依赖模块属性）。
    """

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def stata_regress(
        depvar: str,
        indepvars: str,
        options: str = "",
        condition: str = "",
        in_range: str = "",
    ) -> str | deps.ToolResult:
        """运行线性回归分析 (OLS)。

        返回系数表、标准误、t 值、p 值和模型诊断统计量。

        Args:
            depvar: 因变量名。
            indepvars: 自变量列表（空格分隔）。
            options: 额外选项，如 "robust"（稳健标准误）、"noconstant"。
            condition: if 条件子句（可选）。例："foreign == 1 & price < 10000"。

        Returns:
            回归分析结果表。
        """
        if err := deps.validate_identifier(depvar, "depvar", required=True):
            return deps.result_or_error(err)
        if err := deps.validate_varlist(indepvars, "indepvars"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(condition, "condition"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(in_range, "in_range"):
            return deps.result_or_error(err)
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)
        cmd = f"regress {depvar} {indepvars}"
        cmd += deps.filter_clause(condition, in_range)
        if options.strip():
            cmd += f", {options}"
        return deps.run_stata_command(cmd)

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def stata_logistic(
        depvar: str,
        indepvars: str,
        options: str = "",
        condition: str = "",
        in_range: str = "",
    ) -> str | deps.ToolResult:
        """运行 Logistic 回归分析。

        执行 Stata 原生 `logistic` 命令，默认输出优势比（OR）、标准误和模型拟合统计量。

        Args:
            depvar: 二元因变量名（取值 0/1）。
            indepvars: 自变量列表（空格分隔）。
            options: 额外选项，如 "robust"、"vce(cluster id)"、"level(90)"。
                ``logistic`` 默认即报告优势比（``or`` 可写但冗余）；想看原始系数
                用 ``coef``，或改用 ``stata_run("logit ...")``。
            condition: if 条件子句（可选）。例："age >= 18"。

        Returns:
            Logistic 回归结果表。
        """
        if err := deps.validate_identifier(depvar, "depvar", required=True):
            return deps.result_or_error(err)
        if err := deps.validate_varlist(indepvars, "indepvars"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(condition, "condition"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(in_range, "in_range"):
            return deps.result_or_error(err)
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)
        cmd = f"logistic {depvar} {indepvars}"
        cmd += deps.filter_clause(condition, in_range)
        if options.strip():
            cmd += f", {options}"
        return deps.run_stata_command(cmd)

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def stata_ttest(
        varname: str,
        byvar: str = "",
        compare_to: str = "",
        options: str = "",
        condition: str = "",
        in_range: str = "",
    ) -> str | deps.ToolResult:
        """运行 t 检验（单样本 / 按组两样本 / 配对 / 非配对）。

        官方的四种数据形式都能表达 —— **裸 `ttest varname` 不是合法命令**
        （实测报 ``by() option required`` → r(100)），必须二选一：

        ==================================  ================================
        形式                                 参数
        ==================================  ================================
        单样本 ``ttest v == #``              ``compare_to="5000"``
        按组两样本 ``ttest v, by(g)``        ``byvar="foreign"``
        配对 ``ttest v1 == v2``              ``compare_to="after"``
        非配对 ``ttest v1 == v2, unpaired``  ``compare_to="v2", options="unpaired"``
        ==================================  ================================

        Args:
            varname: 要检验的变量名（单个）。
            byvar: 分组变量 —— 做按组两样本检验。与 compare_to 互斥。
            compare_to: 比较对象 —— 数值（单样本，检验均值是否等于它）或另一个
                变量名（配对；加 options="unpaired" 则为非配对）。与 byvar 互斥。
            options: 额外选项，如 "unequal"、"welch"、"level(90)"、"unpaired"。
            condition: if 条件子句（可选）。例："!missing(price)".
            in_range: 观测范围（可选），如 "1/100"。

        Returns:
            t 检验结果表。
        """
        if err := deps.validate_identifier(varname, "varname", required=True):
            return deps.result_or_error(err)
        if err := deps.validate_identifier(byvar, "byvar"):
            return deps.result_or_error(err)
        if err := deps.validate_no_injection(compare_to, "compare_to"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(condition, "condition"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(in_range, "in_range"):
            return deps.result_or_error(err)
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)

        if byvar.strip() and compare_to.strip():
            return deps.make_error(
                "错误: byvar 与 compare_to 互斥 —— 前者是按组两样本检验"
                "（ttest v, by(g)），后者是单样本/配对检验（ttest v == x）。"
            )
        if not byvar.strip() and not compare_to.strip():
            # 裸 `ttest v` 会 r(100)；与其把非法命令发给 Stata，不如说明该给什么。
            return deps.make_error(
                "错误: 必须给出 byvar 或 compare_to 之一（裸 `ttest 变量` 不是合法命令）。\n"
                '  · 单样本检验均值是否等于某值 → compare_to="5000"\n'
                '  · 按组比较两样本         → byvar="foreign"\n'
                '  · 配对/非配对比较两变量   → compare_to="另一变量"'
                "（非配对再加 options=\"unpaired\"）"
            )

        lhs = f"{varname} == {compare_to.strip()}" if compare_to.strip() else varname
        cmd = f"ttest {lhs}"
        cmd += deps.filter_clause(condition, in_range)
        opts = " ".join(
            p for p in (f"by({byvar.strip()})" if byvar.strip() else "", options.strip()) if p
        )
        if opts:
            cmd += f", {opts}"
        return deps.run_stata_command(cmd)

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def stata_probit(
        depvar: str,
        indepvars: str,
        marginal_effects: bool = False,
        options: str = "",
        condition: str = "",
        in_range: str = "",
    ) -> str | deps.ToolResult:
        """运行 Probit 回归（二元因变量）。

        Args:
            depvar: 二元因变量名（取值 0/1）。
            indepvars: 自变量列表（空格分隔）。
            marginal_effects: True 时在回归后自动追加 ``margins, dydx(*)`` 报告
                平均边际效应（probit 系数不能直接解读，通常需要边际效应）。
            options: 额外选项，如 "robust"、"vce(cluster id)"。
            condition: if 条件子句（可选）。

        Returns:
            Probit 回归结果（可选附平均边际效应）。
        """
        if err := deps.validate_identifier(depvar, "depvar", required=True):
            return deps.result_or_error(err)
        if err := deps.validate_varlist(indepvars, "indepvars"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(condition, "condition"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(in_range, "in_range"):
            return deps.result_or_error(err)
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)
        cmd = f"probit {depvar} {indepvars}"
        cmd += deps.filter_clause(condition, in_range)
        if options.strip():
            cmd += f", {options}"
        if marginal_effects:
            cmd += "\nmargins, dydx(*)"
        return deps.run_stata_command(cmd)

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def stata_poisson(
        depvar: str,
        indepvars: str,
        irr: bool = False,
        options: str = "",
        condition: str = "",
        in_range: str = "",
    ) -> str | deps.ToolResult:
        """运行 Poisson 回归（计数因变量）。

        Args:
            depvar: 计数因变量名（非负整数）。
            indepvars: 自变量列表（空格分隔）。
            irr: True 时报告发生率比（incidence-rate ratios）而非系数。
            options: 额外选项，如 "robust"、"exposure(varname)"、"vce(cluster id)"。
            condition: if 条件子句（可选）。

        Returns:
            Poisson 回归结果表。
        """
        if err := deps.validate_identifier(depvar, "depvar", required=True):
            return deps.result_or_error(err)
        if err := deps.validate_varlist(indepvars, "indepvars"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(condition, "condition"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(in_range, "in_range"):
            return deps.result_or_error(err)
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)
        cmd = f"poisson {depvar} {indepvars}"
        cmd += deps.filter_clause(condition, in_range)
        opt_parts = [p for p in (("irr" if irr else ""), options.strip()) if p]
        if opt_parts:
            cmd += f", {' '.join(opt_parts)}"
        return deps.run_stata_command(cmd)

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def stata_xtreg(
        depvar: str,
        indepvars: str,
        effects: str = "fe",
        options: str = "",
        condition: str = "",
        in_range: str = "",
    ) -> str | deps.ToolResult:
        """运行面板数据回归（xtreg）。

        **前提**：必须先声明面板结构 —— ``stata_run("xtset panelvar timevar")``，
        否则报 r(459)。做 Hausman 检验时，分别用 ``effects="fe"`` 与 ``effects="re"``
        运行并各自 ``estimates store``，再 ``stata_run("hausman fe re")``。

        Args:
            depvar: 因变量名。
            indepvars: 自变量列表（空格分隔）。
            effects: 估计量，取值 fe(固定效应)/re(随机效应)/be(组间)/mle/pa（默认 fe）。
            options: 额外选项，如 "robust"、"vce(cluster id)"。
            condition: if 条件子句（可选）。

        Returns:
            面板回归结果表。
        """
        if err := deps.validate_identifier(depvar, "depvar", required=True):
            return deps.result_or_error(err)
        if err := deps.validate_varlist(indepvars, "indepvars"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(condition, "condition"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(in_range, "in_range"):
            return deps.result_or_error(err)
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)
        eff = effects.strip().lower()
        if eff not in _XTREG_EFFECTS:
            return deps.make_error(
                f"错误: effects 只能是 {', '.join(sorted(_XTREG_EFFECTS))} 之一，收到 '{effects}'"
            )
        cmd = f"xtreg {depvar} {indepvars}"
        cmd += deps.filter_clause(condition, in_range)
        opt_parts = [p for p in (eff, options.strip()) if p]
        cmd += f", {' '.join(opt_parts)}"
        return deps.run_stata_command(cmd)

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def stata_ivregress(
        depvar: str,
        endogenous: str,
        instruments: str,
        exogenous: str = "",
        estimator: str = "2sls",
        options: str = "",
        condition: str = "",
        in_range: str = "",
    ) -> str | deps.ToolResult:
        """运行工具变量回归（ivregress，2SLS/LIML/GMM）。

        拼出 ``ivregress <est> depvar [exog] (endog = instruments) [if], options``。
        诊断走后估计：弱工具变量用 ``stata_run("estat firststage")``，
        过度识别用 ``stata_run("estat overid")``。

        Args:
            depvar: 因变量名。
            endogenous: 内生自变量列表（空格分隔）。
            instruments: 排除的工具变量列表（空格分隔），需 ≥ 内生变量个数。
            exogenous: 外生自变量列表（空格分隔，可留空）。
            estimator: 估计量 2sls/liml/gmm（默认 2sls）。
            options: 额外选项，如 "robust"、"first"、"vce(cluster id)"。
            condition: if 条件子句（可选）。

        Returns:
            工具变量回归结果表。
        """
        if err := deps.validate_identifier(depvar, "depvar", required=True):
            return deps.result_or_error(err)
        if err := deps.validate_varlist(endogenous, "endogenous"):
            return deps.result_or_error(err)
        if not endogenous.strip():
            return deps.make_error("错误: 至少需要一个内生变量")
        if err := deps.validate_varlist(instruments, "instruments"):
            return deps.result_or_error(err)
        if not instruments.strip():
            return deps.make_error("错误: 至少需要一个工具变量")
        if err := deps.validate_varlist(exogenous, "exogenous"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(condition, "condition"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(in_range, "in_range"):
            return deps.result_or_error(err)
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)
        est = estimator.strip().lower()
        if est not in _IVREGRESS_ESTIMATORS:
            return deps.make_error(
                f"错误: estimator 只能是 {', '.join(sorted(_IVREGRESS_ESTIMATORS))} 之一，收到 '{estimator}'"
            )
        exog = f" {exogenous.strip()}" if exogenous.strip() else ""
        cmd = f"ivregress {est} {depvar}{exog} ({endogenous.strip()} = {instruments.strip()})"
        cmd += deps.filter_clause(condition, in_range)
        if options.strip():
            cmd += f", {options}"
        return deps.run_stata_command(cmd)

    # 收集本模块的工具函数（只认 `stata_` 前缀的可调用局部变量）
    return {k: v for k, v in locals().items() if k.startswith("stata_") and callable(v)}
