"""扩展估计工具模块：logit / mlogit / nbreg / qreg / mixed。

本模块在 server.py import 时通过 register(mcp, deps) 装配工具，deps 由主服务器
注入（见 register 的 deps 命名空间说明），模块自身**绝不** import server。

五个工具都是「读估计」——运行估计、不改动数据集、不落盘，故一律
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
import re
from typing import Any

# mlogit 的 baseoutcome 必须是类别取值（含 0 —— 0/1/2 编码很常见）；拒绝前导零
# 与非数字；"" 表示不指定（Stata 默认以取值最小的类别为基准）。官方还接受值
# 标签名作基准，本工具未开放（可用 options 传入）。
_BASEOUTCOME_RE = re.compile(r"^(0|[1-9]\d*)$")


def register(mcp: Any, deps: Any) -> dict[str, Any]:
    """在 mcp 上注册本模块的全部扩展估计工具。

    Returns:
        {工具名: 函数} —— 供主服务器 ``globals().update()`` 暴露为模块属性，
        与 server.py 内既有 50 个 ``stata_*`` 工具保持一致（E2E 与
        ``from server import stata_xxx`` 都依赖模块属性）。
    """

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def stata_logit(
        depvar: str,
        indepvars: str,
        options: str = "",
        condition: str = "",
        in_range: str = "",
        timeout: int = 60,
    ) -> str | deps.ToolResult:
        """运行 Logit 回归分析。

        对二元因变量建模，报告**原始系数（对数几率 / log-odds）**。想要优势比
        （OR）请改用 `stata_logistic`（`logistic` 命令默认即输出 OR，等价于
        `logit, or`）—— 两者是同一模型的不同展示面，系数可互相换算。

        Args:
            depvar: 二元因变量名（取值 0/1）。
            indepvars: 自变量列表（空格分隔）。
            options: 额外选项，如 "robust"（稳健标准误）、"vce(cluster id)"、
                "level(90)"、"noconstant"；想要 OR 可传 "or"。
            condition: if 条件子句（可选）。例："age >= 18 & sex == 1"。
            in_range: 观测范围（可选），如 "1/500"。
            timeout: 命令超时秒数（默认 60，钳制 10–1800）。长模型/大样本可显式调大。

        Returns:
            Logit 回归结果表（系数、标准误、z 值、p 值与模型诊断统计量）。
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
        cmd = f"logit {depvar} {indepvars}"
        cmd += deps.filter_clause(condition, in_range)
        if options.strip():
            cmd += f", {options.strip()}"
        safe_timeout = max(10, min(timeout, 1800))
        return deps.run_stata_command(cmd, timeout=safe_timeout)

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def stata_mlogit(
        depvar: str,
        indepvars: str,
        baseoutcome: str = "",
        options: str = "",
        condition: str = "",
        in_range: str = "",
        timeout: int = 60,
    ) -> str | deps.ToolResult:
        """运行多分类 Logit 回归分析（multinomial logit）。

        因变量是取 0/1/2... 的分类变量，估计各类别相对**基准类别**的选择概率。

        Args:
            depvar: 多分类因变量名（取值 0/1/2... 的整数值）。
            indepvars: 自变量列表（空格分隔）。
            baseoutcome: 基准类别（正整数，str）。不传时 Stata 默认以取值最小
                的类别为基准。例：``baseoutcome="2"`` 且 ``options="robust"``
                拼出 ``mlogit y x1 x2, baseoutcome(2) robust``。
            options: 额外选项，如 "robust"、"baselevels"、"rrr"（相对风险比）。
            condition: if 条件子句（可选）。例："income > 0"。
            in_range: 观测范围（可选），如 "1/500"。
            timeout: 命令超时秒数（默认 60，钳制 10–1800）。长模型/大样本可显式调大。

        Returns:
            多分类 Logit 回归结果表（各类别的相对风险比/系数）。
        """
        if err := deps.validate_identifier(depvar, "depvar", required=True):
            return deps.result_or_error(err)
        if err := deps.validate_varlist(indepvars, "indepvars"):
            return deps.result_or_error(err)
        if baseoutcome.strip() and not _BASEOUTCOME_RE.match(baseoutcome.strip()):
            return deps.result_or_error(
                "错误: baseoutcome 必须是类别取值（非负整数 str），如 \"0\" 或 \"2\"。"
                f"收到: '{baseoutcome}'。合法取值示例：baseoutcome=\"1\"、"
                'baseoutcome="0"。不传则使用 Stata 默认基准类别。'
            )
        if err := deps.validate_filter_expr(condition, "condition"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(in_range, "in_range"):
            return deps.result_or_error(err)
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)
        cmd = f"mlogit {depvar} {indepvars}"
        cmd += deps.filter_clause(condition, in_range)
        opts = []
        if baseoutcome.strip():
            opts.append(f"baseoutcome({baseoutcome.strip()})")
        if options.strip():
            opts.append(options.strip())
        if opts:
            cmd += ", " + " ".join(opts)
        safe_timeout = max(10, min(timeout, 1800))
        return deps.run_stata_command(cmd, timeout=safe_timeout)

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def stata_nbreg(
        depvar: str,
        indepvars: str,
        options: str = "",
        condition: str = "",
        in_range: str = "",
        timeout: int = 60,
    ) -> str | deps.ToolResult:
        """运行负二项回归分析（negative binomial regression）。

        用于**计数数据**且方差明显大于均值（过度离散）的场景，比 Poisson 回归
        多估计一个离散参数 alpha；当 alpha=0 时退化为 Poisson。

        Args:
            depvar: 计数因变量名（非负整数）。
            indepvars: 自变量列表（空格分隔）。
            options: 额外选项，如 "exposure(pop)"（暴露人口，要求单位时间/空间
                的发生率）、"robust"、"vce(cluster id)"、"irls"。
            condition: if 条件子句（可选）。例："year >= 2000"。
            in_range: 观测范围（可选），如 "1/500"。
            timeout: 命令超时秒数（默认 60，钳制 10–1800）。长模型/大样本可显式调大。

        Returns:
            负二项回归结果表（系数、alpha、对数似然与 LR 检验）。
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
        cmd = f"nbreg {depvar} {indepvars}"
        cmd += deps.filter_clause(condition, in_range)
        if options.strip():
            cmd += f", {options.strip()}"
        safe_timeout = max(10, min(timeout, 1800))
        return deps.run_stata_command(cmd, timeout=safe_timeout)

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def stata_qreg(
        depvar: str,
        indepvars: str,
        quantile: float = 0.5,
        options: str = "",
        condition: str = "",
        in_range: str = "",
        timeout: int = 60,
    ) -> str | deps.ToolResult:
        """运行分位数回归（quantile regression，qreg）。

        估计因变量在指定分位数上的条件分布，对离群值与重尾更稳健；默认 0.5
        即中位数回归（与 qreg 官方默认一致，故 0.5 时不拼 ``quantile()``）。

        Args:
            depvar: 因变量名。
            indepvars: 自变量列表（空格分隔）。
            quantile: 目标分位数，取值在 (0, 1) 开区间，如 0.25 / 0.5 / 0.9。
                例：``quantile=0.9`` 拼出 ``qreg ... , quantile(0.9)``。
            options: 额外选项，如 "vce(iid)"（同方差假设，官方默认）、
                "vce(robust)"（稳健）、"noconstant"。**注意 qreg 不接受
                vce(bootstrap)**（实测 r(198)）—— bootstrap 标准误请用
                ``stata_run("bsqreg depvar indeps, reps(200)")``。
            condition: if 条件子句（可选）。例："foreign == 1"。
            in_range: 观测范围（可选），如 "1/500"。
            timeout: 命令超时秒数（默认 60，钳制 10–1800）。长模型/大样本可显式调大。

        Returns:
            分位数回归结果表（系数、标准误、t 值、p 值）。
        """
        if err := deps.validate_identifier(depvar, "depvar", required=True):
            return deps.result_or_error(err)
        if err := deps.validate_varlist(indepvars, "indepvars"):
            return deps.result_or_error(err)
        try:
            q = float(quantile)
        except (TypeError, ValueError):
            return deps.result_or_error(
                "错误: quantile 必须是 (0,1) 之间的数值，如 0.25 / 0.5 / 0.9。"
                f"收到: {quantile!r}。"
            )
        if not (0.0 < q < 1.0):
            return deps.result_or_error(
                "错误: quantile 必须在 (0,1) 开区间内（不含端点），如 0.25 / 0.5 / 0.9。"
                f"收到: {quantile!r}。"
            )
        if err := deps.validate_filter_expr(condition, "condition"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(in_range, "in_range"):
            return deps.result_or_error(err)
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)
        cmd = f"qreg {depvar} {indepvars}"
        cmd += deps.filter_clause(condition, in_range)
        opts = []
        # qreg 官方默认就是中位数，0.5 不拼 quantile()，保持命令简洁。
        if q != 0.5:
            opts.append(f"quantile({q})")
        if options.strip():
            opts.append(options.strip())
        if opts:
            cmd += ", " + " ".join(opts)
        safe_timeout = max(10, min(timeout, 1800))
        return deps.run_stata_command(cmd, timeout=safe_timeout)

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def stata_mixed(
        depvar: str,
        indepvars: str,
        random: str = "",
        options: str = "",
        condition: str = "",
        in_range: str = "",
        timeout: int = 60,
    ) -> str | deps.ToolResult:
        """运行多层混合效应线性回归（multilevel mixed-effects，mixed）。

        用显式 ``||`` 记法表达随机效应，**不需要先 xtset**。可写多个 ``||`` 组
        表示嵌套/交叉结构，冒号左边是分组变量、右边是该组内的随机斜率变量。

        Args:
            depvar: 因变量名。
            indepvars: 固定效应自变量列表（空格分隔）。
            random: 随机效应部分，**必须以 "||" 开头**。例：
                ``random="|| id:"`` → ``mixed y x1 x2 || id:``（id 上的随机截距）；
                ``random="|| id: || time:"`` → 两层嵌套；
                ``random="|| id: x1"`` → id 上的随机截距 + 随机斜率。
            options: 额外选项，如 "reml"（默认）/"ml"（最大似然）、
                "vce(robust)"、"noconstant"。
            condition: if 条件子句（可选）。例："!missing(price)"。
            in_range: 观测范围（可选），如 "1/500"。
            timeout: 命令超时秒数（默认 60，钳制 10–1800）。长模型/大样本可显式调大。

        Returns:
            混合效应模型估计表（固定效应系数、随机效应方差分量、LR 检验）。
        """
        if err := deps.validate_identifier(depvar, "depvar", required=True):
            return deps.result_or_error(err)
        if err := deps.validate_varlist(indepvars, "indepvars"):
            return deps.result_or_error(err)
        if random.strip():
            if err := deps.validate_no_injection(random, "random"):
                return deps.result_or_error(err)
            if not random.strip().startswith("||"):
                return deps.result_or_error(
                    "错误: random 必须以 '||' 开头（随机效应记法）。"
                    f"收到: '{random}'。合法示例：random=\"|| id:\" → "
                    '"mixed y x1 x2 || id:"；random="|| id: || time:" → 两层嵌套。'
                )
        if err := deps.validate_filter_expr(condition, "condition"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(in_range, "in_range"):
            return deps.result_or_error(err)
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)
        # 官方语法：[if] [in] 属于固定效应方程，位于 || 随机效应**之前**。
        # 实测 mixed y x1 || id: if cond 也能跑，但顺序写对避免语义歧义。
        cmd = f"mixed {depvar} {indepvars}"
        cmd += deps.filter_clause(condition, in_range)
        if random.strip():
            cmd += f" {random.strip()}"
        if options.strip():
            cmd += f", {options.strip()}"
        safe_timeout = max(10, min(timeout, 1800))
        return deps.run_stata_command(cmd, timeout=safe_timeout)

    # 收集本模块的工具函数（只认 `stata_` 前缀的可调用局部变量）
    return {k: v for k, v in locals().items() if k.startswith("stata_") and callable(v)}
