"""数据生成工具模块：generate / egen / predict / xtset。

本模块在 server.py import 时通过 register(mcp, deps) 装配工具，deps 由主服务器
注入（见 register 的 deps 命名空间说明），模块自身**绝不** import server。

四个工具覆盖「创建/声明变量与面板结构」：
- generate（`generate`，普通生成）、egen（`egen`，扩展生成函数）—— 支持官方的
  ``[type]`` 存储类型位置（byte/int/long/float/double/str#/strL，白名单校验）、
  ``[if] [in]``（经 deps.filter_clause 拼在逗号之前）与 options 逃生舱；
  egen 另有 ``bysort <by>`` 前缀支持组内聚合。
- predict（`predict`，估计后生成预测值/残差）—— 前提是先跑过一个估计命令；
  newvar 必填（required=True）。
- xtset（`xtset` / `tsset`，声明面板/时间序列结构）—— 是 stata_xtreg 与全部
  ``xt*`` / ``ts*`` 命令的前提；按给出的变量自动选命令。

统一约定（与 server.py 既有工具一致）：
- newvar / panelvar / timevar → deps.validate_identifier（required=True 用于必填）
- by → deps.validate_varlist；condition/in_range → deps.validate_filter_expr，经
  deps.filter_clause 拼在 **逗号之前**
- vartype → deps.validate_storage_type（正向白名单，拒绝可逃逸括号的记号）
- options / expression / fcn → deps.validate_no_injection
- 校验失败一律 return deps.result_or_error(err)；错误文本以 "错误: " 开头、中文
- 四个工具都会改动内存数据集（创建变量 / 声明结构），readOnlyHint=False、
  destructiveHint=False（与 server.py 原注解一致）
"""

from typing import Any


def register(mcp: Any, deps: Any) -> dict[str, Any]:
    """在 mcp 上注册本模块的全部数据生成工具。

    Returns:
        {工具名: 函数} —— 供主服务器 ``globals().update()`` 暴露为模块属性，
        与 server.py 内既有 ``stata_*`` 工具保持一致（E2E 与
        ``from server import stata_generate`` 都依赖模块属性）。
    """

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def stata_generate(
        newvar: str,
        expression: str,
        condition: str = "",
        in_range: str = "",
        vartype: str = "",
        options: str = "",
    ) -> str | deps.ToolResult:
        """创建新变量（generate）。

        变量名已存在时 Stata 报 r(110)；此时应改用 ``stata_run("replace ...")``
        覆盖，或换个新名。

        Args:
            newvar: 新变量名（须是合法标识符，且当前不存在）。
            expression: 赋值表达式，如 "ln(price)"、"price/100"、"age^2"、
                "(foreign==1)"。
            condition: if 条件子句（可选）—— 仅对满足条件的观测赋值，其余为缺失。

        Returns:
            创建确认信息。
        """
        if err := deps.validate_identifier(newvar, "newvar", required=True):
            return deps.result_or_error(err)
        if not expression.strip():
            return deps.make_error("错误: 请提供赋值表达式")
        if err := deps.validate_no_injection(expression, "expression"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(condition, "condition"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(in_range, "in_range"):
            return deps.result_or_error(err)
        if err := deps.validate_storage_type(vartype):
            return deps.result_or_error(err)
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)
        type_part = f"{vartype.strip()} " if vartype.strip() else ""
        cmd = f"generate {type_part}{newvar} = {expression.strip()}"
        cmd += deps.filter_clause(condition, in_range)
        if options.strip():
            cmd += f", {options.strip()}"
        return deps.run_stata_command(cmd)

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def stata_egen(
        newvar: str,
        fcn: str,
        by: str = "",
        condition: str = "",
        in_range: str = "",
        vartype: str = "",
        options: str = "",
    ) -> str | deps.ToolResult:
        """用扩展生成函数创建新变量（egen）。

        egen 提供 generate 没有的聚合/行运算函数。

        Args:
            newvar: 新变量名（须是合法标识符，且当前不存在）。
            fcn: egen 函数调用，如 "mean(price)"、"rowmean(x1 x2 x3)"、
                "group(id year)"、"total(sales)"、"rank(score)"、"tag(id)"。
            by: 分组变量（可选，空格分隔）—— 拼成 ``bysort <by>: egen ...``，
                用于组内聚合，如按 industry 求组内均值。
            condition: if 条件子句（可选）。

        Returns:
            创建确认信息。
        """
        if err := deps.validate_identifier(newvar, "newvar", required=True):
            return deps.result_or_error(err)
        if not fcn.strip():
            return deps.make_error("错误: 请提供 egen 函数，如 mean(price)")
        if err := deps.validate_no_injection(fcn, "fcn"):
            return deps.result_or_error(err)
        if err := deps.validate_varlist(by, "by"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(condition, "condition"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(in_range, "in_range"):
            return deps.result_or_error(err)
        if err := deps.validate_storage_type(vartype):
            return deps.result_or_error(err)
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)
        prefix = f"bysort {by.strip()}: " if by.strip() else ""
        type_part = f"{vartype.strip()} " if vartype.strip() else ""
        cmd = f"{prefix}egen {type_part}{newvar} = {fcn.strip()}"
        cmd += deps.filter_clause(condition, in_range)
        if options.strip():
            cmd += f", {options.strip()}"
        return deps.run_stata_command(cmd)

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def stata_predict(
        newvar: str,
        options: str = "",
        condition: str = "",
        in_range: str = "",
    ) -> str | deps.ToolResult:
        """在估计后生成预测值 / 残差等（predict，后估计命令）。

        **前提**：先运行过一个估计命令（regress/logit 等）。它会创建一个新变量。

        Args:
            newvar: 存放结果的新变量名。
            options: 预测类型，如 "xb"（线性预测，默认）、"residuals"（残差）、
                "pr"（logit/probit 的预测概率）、"stdp"（预测标准误）、
                "cooksd"（Cook 距离）。
            condition: if 条件子句（可选）。

        Returns:
            创建确认信息。
        """
        if err := deps.validate_identifier(newvar, "newvar", required=True):
            return deps.result_or_error(err)
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(condition, "condition"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(in_range, "in_range"):
            return deps.result_or_error(err)
        cmd = f"predict {newvar}"
        cmd += deps.filter_clause(condition, in_range)
        if options.strip():
            cmd += f", {options.strip()}"
        return deps.run_stata_command(cmd)

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def stata_xtset(
        panelvar: str = "",
        timevar: str = "",
        action: str = "set",
        options: str = "",
    ) -> str | deps.ToolResult:
        """声明面板 / 时间序列结构（``xtset`` / ``tsset``）。

        这是 ``stata_xtreg`` 与全部 ``xt*`` / ``ts*`` 命令的**前提**：未声明时它们
        报 r(459) "panel variable not set"。当前设定也可用 ``stata_status`` 查看。

        按给出的变量自动选命令：给 ``panelvar`` 走 ``xtset``（面板），只给
        ``timevar`` 走 ``tsset``（纯时序）。

        Args:
            panelvar: 面板（个体）标识变量，如 "idcode"、"firm_id"。
            timevar: 时间变量，如 "year"、"date"。面板数据可省略（只声明个体维度）。
            action: ``set``（默认，声明）/ ``show``（查询当前设定）/ ``clear``（清除）。
            options: 官方选项，如 "delta(1)"、"format(%ty)"、"yearly"、"daily"。

        Returns:
            设定确认（含 Panel/Time variable 与 Delta），或当前设定。
        """
        if action not in ("set", "show", "clear"):
            return deps.make_error(
                f'错误: action 只能是 "set" / "show" / "clear"（收到 {action!r}）'
            )
        if action == "show":
            # 裸 xtset 是查询，但未设定时报 r(459)；capture noisily 既不中断
            # 命令链，又保留 "panel variable not set" 这句有用的诊断。
            # 实测 xtset 对纯时序数据也照报 "Time variable: …"，无需再发 tsset。
            return deps.run_stata_command("capture noisily xtset")
        if action == "clear":
            return deps.run_stata_command("xtset, clear")

        if err := deps.validate_identifier(panelvar, "panelvar"):
            return deps.result_or_error(err)
        if err := deps.validate_identifier(timevar, "timevar"):
            return deps.result_or_error(err)
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)
        if not panelvar.strip() and not timevar.strip():
            return deps.make_error(
                "错误: 至少要给出 panelvar 或 timevar。\n"
                '  · 面板数据 → panelvar="个体变量"（可再加 timevar="时间变量"）\n'
                '  · 纯时序   → timevar="时间变量"'
            )

        if panelvar.strip():
            cmd = f"xtset {panelvar.strip()}"
            if timevar.strip():
                cmd += f" {timevar.strip()}"
        else:
            cmd = f"tsset {timevar.strip()}"
        if options.strip():
            cmd += f", {options.strip()}"
        return deps.run_stata_command(cmd)

    # 收集本模块的工具函数（只认 `stata_` 前缀的可调用局部变量）
    return {k: v for k, v in locals().items() if k.startswith("stata_") and callable(v)}
