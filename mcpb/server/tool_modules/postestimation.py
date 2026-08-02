"""后估计工具模块：作用于已存储估计结果的线性/非线性组合检验与模型比较。

本模块提供 3 个**只读**的后估计工具：

- ``stata_lincom``：对当前估计结果做线性组合的 Wald 检验（lincom）；
- ``stata_nlcom``：对当前估计结果做非线性组合的 delta 法检验（nlcom）；
- ``stata_hausman``：对两个已存储的估计结果做 Hausman 设定检验。

三者都作用于「当前活跃 / 已存储」的估计结果，因此**不接受** ``if`` / ``in``
子句（官方 ``lincom`` / ``nlcom`` / ``hausman`` 语法本身也不支持）—— 需要限定
子样本时，请在估计命令（如 ``stata_regress``）上限定后重新估计，再执行本模块
的检验。

``register(mcp, deps)`` 由主服务器在 import 时调用；本模块**不 import server**，
只依赖注入的 deps 命名空间（ToolAnnotations / ToolResult / run_stata_command /
make_error / result_or_error / 各校验器）。
"""

from typing import Any


def register(mcp: Any, deps: Any) -> dict[str, Any]:
    """在 mcp 上注册本模块的全部后估计工具。

    Returns:
        {工具名: 函数} —— 供主服务器 ``globals().update()`` 暴露为模块属性，
        与 server.py 内既有 50 个 ``stata_*`` 工具保持一致（E2E 与
        ``from server import stata_xxx`` 都依赖模块属性）。
    """

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def stata_lincom(
        expression: str, options: str = "", timeout: int = 60
    ) -> str | deps.ToolResult:
        """对当前估计结果做线性组合的 Wald 检验（``lincom``）。

        **前提**：先运行过一个估计命令（``regress`` / ``logit`` 等），可用
        ``stata_status`` 确认「当前活跃」估计存在。``lincom`` 检验 ``expression``
        所示**线性**组合是否为 0（F 或 chi2 统计量 + p 值），系数一律用
        ``_b[变量名]`` 引用。

        常用例子：
            - ``_b[mpg] + _b[weight]``   两系数之和是否为 0
            - ``2*_b[mpg] - _b[weight]`` 系数的线性组合
            - ``_b[mpg] - 1``            系数是否等于 1

        非线性组合（如 ``exp(_b[mpg])``）请改用 ``stata_nlcom``（delta 法）。

        Args:
            expression: 线性组合表达式（必填），如 ``_b[mpg] + _b[weight]``。
            options: 官方选项，如 ``level(95)``。
            timeout: 命令超时秒数（默认 60，钳制 10–1800）。长模型/大样本可显式调大。

        Returns:
            Wald 检验结果（统计量与 p 值）。
        """
        if not expression.strip():
            return deps.make_error('错误: expression 必填，如 "_b[mpg] + _b[weight]"')
        if err := deps.validate_no_injection(expression, "expression"):
            return deps.result_or_error(err)
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)
        cmd = f"lincom {expression.strip()}"
        if options.strip():
            cmd += f", {options.strip()}"
        safe_timeout = max(10, min(timeout, 1800))
        return deps.run_stata_command(cmd, timeout=safe_timeout)

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def stata_nlcom(expression: str, options: str = "", timeout: int = 60) -> str | deps.ToolResult:
        """对当前估计结果做非线性组合的 delta 法检验（``nlcom``）。

        **前提**：先运行过一个估计命令，可用 ``stata_status`` 确认「当前活跃」
        估计存在。``nlcom`` 对 ``expression`` 所示的**非线性**函数做 delta 法
        （一阶泰勒展开）近似其标准误，系数用 ``_b[变量名]`` 引用。

        常用例子：
            - ``exp(_b[mpg])``            系数的指数函数
            - ``_b[x1]/_b[x2]``           系数之比（ratio）
            - ``(_b[x1]/_b[x2]) (exp(_b[x1]))``  多个表达式可并列一次检验

        线性组合请用更直接的 ``stata_lincom``（exact 而非近似）。

        Args:
            expression: 非线性组合表达式（必填），如 ``exp(_b[mpg])``；多个
                表达式用空格或括号分组并列，如 ``(_b[x1]/_b[x2]) (exp(_b[x1]))``。
            options: 官方选项，如 ``level(95)``。
            timeout: 命令超时秒数（默认 60，钳制 10–1800）。长模型/大样本可显式调大。

        Returns:
            delta 法检验结果（各表达式的估计值、标准误、置信区间与 p 值）。
        """
        if not expression.strip():
            return deps.make_error(
                '错误: expression 必填，如 "exp(_b[mpg])" 或 "(_b[x1]/_b[x2]) (exp(_b[x1]))"'
            )
        if err := deps.validate_no_injection(expression, "expression"):
            return deps.result_or_error(err)
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)
        cmd = f"nlcom {expression.strip()}"
        if options.strip():
            cmd += f", {options.strip()}"
        safe_timeout = max(10, min(timeout, 1800))
        return deps.run_stata_command(cmd, timeout=safe_timeout)

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def stata_hausman(
        consistent: str, efficient: str = "", options: str = "", timeout: int = 60
    ) -> str | deps.ToolResult:
        """对两个已存储的估计结果做 Hausman 设定检验（``hausman``）。

        **前提**：先用 ``stata_estimates(action="store", name="...")`` 存入两个
        模型 —— ``consistent`` 是一致（但可能非有效）的估计，``efficient`` 是
        在原假设下有效（但可能不一致）的估计；``efficient`` 留空时默认取**当前
        活跃**的估计结果。

        典型用法（固定效应 vs 随机效应面板模型）：
            1. ``stata_xtreg("y", "x1 x2", effects="fe")`` 后 ``estimates store fe``
            2. ``stata_xtreg("y", "x1 x2", effects="re")`` 后 ``estimates store re``
            3. ``stata_hausman("fe", "re")``  —— p 值显著拒绝随机效应假设

        Args:
            consistent: 一致估计的存储名（必填），如 "fe"。
            efficient: 有效估计的存储名（可选），留空用当前活跃估计，如 "re"。
            options: 官方选项，如 ``sigmamore``（用一致估计的协方差）、
                ``constant``（含截距项比较）、``alleqs``（比较全部方程）。
            timeout: 命令超时秒数（默认 60，钳制 10–1800）。长模型/大样本可显式调大。

        Returns:
            Hausman 检验结果（chi2 统计量与 p 值）。
        """
        if not consistent.strip():
            return deps.make_error(
                "错误: consistent 必填（一致估计的存储名），如 stata_hausman('fe', 're')"
            )
        if err := deps.validate_identifier(consistent, "consistent", required=True):
            return deps.result_or_error(err)
        if efficient.strip():
            if err := deps.validate_identifier(efficient, "efficient"):
                return deps.result_or_error(err)
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)
        cmd = f"hausman {consistent.strip()}"
        if efficient.strip():
            cmd += f" {efficient.strip()}"
        if options.strip():
            cmd += f", {options.strip()}"
        safe_timeout = max(10, min(timeout, 1800))
        return deps.run_stata_command(cmd, timeout=safe_timeout)

    # 收集本模块的工具函数（只认 `stata_` 前缀的可调用局部变量）
    return {k: v for k, v in locals().items() if k.startswith("stata_") and callable(v)}
