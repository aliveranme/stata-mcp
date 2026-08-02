"""后估计核心工具模块：margins / test / estat / estimates。

本模块在 server.py import 时通过 register(mcp, deps) 装配工具，deps 由主服务器
注入（见 register 的 deps 命名空间说明），模块自身**绝不** import server。

四个工具覆盖对「已存储 / 当前活跃」估计结果的后估计操作：

- ``stata_margins``：边际效应 / 预测边际（margins）—— 唯一支持 ``[if] [in]``
  的后估计命令，子句经 deps.filter_clause 拼在**逗号之前**；
- ``stata_test``：Wald 检验（test）—— 作用于已存储的估计结果，**不接受**
  ``if`` / ``in``（实测传了会 r(198)）；
- ``stata_estat``：后估计诊断（estat）—— subcommand 经 deps.validate_identifier
  正向校验，options 走 deps.validate_no_injection；
- ``stata_estimates``：已存储估计结果的管理（estimates）—— 子命令分派表
  ``_ESTIMATES_ACTIONS`` 与需要名字的 ``_ESTIMATES_NEED_NAME`` 只被它用，
  故留在本模块模块级（不占 server.py）。

统一约定（与 server.py 既有工具一致）：
- varlist / identifier / condition / options → deps.validate_varlist /
  deps.validate_identifier / deps.validate_filter_expr / deps.validate_no_injection
- 校验失败一律 return deps.result_or_error(err)；错误文本以 "错误: " 开头、中文
- 全部四个工具都不改动数据集（margins 会在内存中临时替换变量求值后自动还原，
  estimates 会改动已存储的估计结果 —— 后者 readOnlyHint=False）
"""

from typing import Any

# estimates 的子命令。store/restore/save/use/drop 需要名字；dir/clear/table/stats
# 不需要（table/stats 的名字可选，留空即用当前活跃估计）。只被 stata_estimates
# 用到，故留在本模块模块级。
_ESTIMATES_ACTIONS = frozenset(
    {"store", "restore", "table", "stats", "dir", "drop", "clear", "describe", "replay"}
)
_ESTIMATES_NEED_NAME = frozenset({"store", "restore", "drop"})


def register(mcp: Any, deps: Any) -> dict[str, Any]:
    """在 mcp 上注册本模块的全部后估计核心工具。

    Returns:
        {工具名: 函数} —— 供主服务器 ``globals().update()`` 暴露为模块属性，
        与 server.py 内既有 ``stata_*`` 工具保持一致（E2E 与
        ``from server import stata_margins`` 都依赖模块属性）。
    """

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def stata_margins(
        marginlist: str = "",
        dydx: str = "",
        at: str = "",
        options: str = "",
        condition: str = "",
        in_range: str = "",
        timeout: int = 60,
    ) -> str | deps.ToolResult:
        """估计边际效应 / 预测边际（margins，后估计命令）。

        **前提**：先运行过一个估计命令（regress/logit/probit 等）。probit/logit 的
        系数不可直接解读，``margins, dydx(*)`` 给出平均边际效应。

        Args:
            marginlist: 因子变量的边际（如 "foreign"、"i.rep78"），可留空。
            dydx: 求哪些变量的边际效应，如 "price"、"*"（全部）。
            at: 在何处求值，如 "(mean) _all"、"age=(20 40 60)"。
            options: 额外选项，如 "atmeans"、"vce(unconditional)"。
            condition: if 条件子句（可选）—— 只在满足条件的子样本上求边际。
            in_range: 观测范围（可选），如 "1/100"。
            timeout: 命令超时秒数（默认 60，钳制 10–1800）。长模型/大样本可显式调大。

        Returns:
            边际效应表。
        """
        if err := deps.validate_varlist(marginlist, "marginlist"):
            return deps.result_or_error(err)
        if err := deps.validate_no_injection(dydx, "dydx"):
            return deps.result_or_error(err)
        if err := deps.validate_no_injection(at, "at"):
            return deps.result_or_error(err)
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(condition, "condition"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(in_range, "in_range"):
            return deps.result_or_error(err)
        cmd = "margins"
        if marginlist.strip():
            cmd += f" {marginlist.strip()}"
        cmd += deps.filter_clause(condition, in_range)
        opt_parts = []
        if dydx.strip():
            opt_parts.append(f"dydx({dydx.strip()})")
        if at.strip():
            opt_parts.append(f"at({at.strip()})")
        if options.strip():
            opt_parts.append(options.strip())
        if opt_parts:
            cmd += f", {' '.join(opt_parts)}"
        safe_timeout = max(10, min(timeout, 1800))
        return deps.run_stata_command(cmd, timeout=safe_timeout)

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def stata_test(spec: str, options: str = "", timeout: int = 60) -> str | deps.ToolResult:
        """对上一个估计结果做 Wald 检验（test，后估计命令）。

        **前提**：先运行过一个估计命令。``test`` 作用于已存储的估计结果，
        因此**不接受** ``if`` / ``in``（实测传了会 r(198)）—— 要限定子样本，
        请在估计命令上加 ``condition`` / ``in_range`` 后重新估计。

        Args:
            spec: 检验设定。例：
                - "weight mpg"        联合显著性：weight=0 且 mpg=0
                - "weight = mpg"      系数相等
                - "weight = 0.5"      系数等于某值
            options: 官方选项，如 "mtest"（多重比较校正）、"accumulate"（累积
                前次检验）、"notest"（只累积不输出）、"common"、"df(#)"。
            timeout: 命令超时秒数（默认 60，钳制 10–1800）。长模型/大样本可显式调大。

        Returns:
            Wald 检验结果（F 或 chi2 统计量与 p 值）。
        """
        if not spec.strip():
            return deps.make_error("错误: 请提供检验设定，如 'weight mpg' 或 'weight = mpg'")
        if err := deps.validate_no_injection(spec, "spec"):
            return deps.result_or_error(err)
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)
        cmd = f"test {spec.strip()}"
        if options.strip():
            cmd += f", {options.strip()}"
        safe_timeout = max(10, min(timeout, 1800))
        return deps.run_stata_command(cmd, timeout=safe_timeout)

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def stata_estat(subcommand: str, options: str = "", timeout: int = 60) -> str | deps.ToolResult:
        """运行后估计诊断（``estat``）。

        **前提**：先运行过一个估计命令（可用 ``stata_status`` 确认「当前活跃」）。
        可用子命令随模型而变，用 ``stata_help("<估计命令> postestimation")`` 查全。

        常用：``vif``（方差膨胀因子）、``hettest``（Breusch–Pagan 异方差）、
        ``ovtest``（Ramsey RESET 遗漏变量）、``ic``（AIC/BIC）、``summarize``
        （估计样本的描述统计）、``firststage``（IV 第一阶段）、``imtest``。

        Args:
            subcommand: estat 子命令名，如 "vif"、"hettest"。
            options: 该子命令的官方选项，如 "rhs iid"、"all"。
            timeout: 命令超时秒数（默认 60，钳制 10–1800）。长模型/大样本可显式调大。

        Returns:
            诊断结果表。
        """
        if not subcommand.strip():
            return deps.make_error('错误: 请提供 estat 子命令，如 "vif"、"hettest"')
        if err := deps.validate_identifier(subcommand, "subcommand", required=True):
            return deps.result_or_error(err)
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)
        cmd = f"estat {subcommand.strip()}"
        if options.strip():
            cmd += f", {options.strip()}"
        safe_timeout = max(10, min(timeout, 1800))
        return deps.run_stata_command(cmd, timeout=safe_timeout)

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def stata_estimates(
        action: str = "dir", name: str = "", options: str = "", timeout: int = 60
    ) -> str | deps.ToolResult:
        """管理已存储的估计结果（``estimates``）。

        典型用法：跑完多个模型后逐个 ``store``，再用 ``table`` 并排比较。
        当前已存了哪些可用 ``stata_status`` 或 ``action="dir"`` 查看。

        Args:
            action: ``store`` / ``restore`` / ``drop``（均需 name）、
                ``table`` / ``stats`` / ``describe`` / ``replay``（name 可选）、
                ``dir`` / ``clear``（无需 name）。
            name: 估计结果名；``table`` / ``stats`` 可给多个（空格分隔）。
            options: 官方选项，如 "star stats(N r2)"（table）、"aic bic"（stats）。
            timeout: 命令超时秒数（默认 60，钳制 10–1800）。长模型/大样本可显式调大。

        Returns:
            操作确认或比较表。
        """
        if action not in _ESTIMATES_ACTIONS:
            return deps.make_error(
                f"错误: action 只能是 {sorted(_ESTIMATES_ACTIONS)}（收到 {action!r}）"
            )
        if err := deps.validate_varlist(name, "name"):
            return deps.result_or_error(err)
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)
        if action in _ESTIMATES_NEED_NAME and not name.strip():
            return deps.make_error(f'错误: action="{action}" 必须提供 name')

        cmd = f"estimates {action}"
        if name.strip():
            cmd += f" {name.strip()}"
        if options.strip():
            cmd += f", {options.strip()}"
        safe_timeout = max(10, min(timeout, 1800))
        return deps.run_stata_command(cmd, timeout=safe_timeout)

    # 收集本模块的工具函数（只认 `stata_` 前缀的可调用局部变量）
    return {k: v for k, v in locals().items() if k.startswith("stata_") and callable(v)}
