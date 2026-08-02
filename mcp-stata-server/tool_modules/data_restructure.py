"""数据重构工具模块：在既有数据集上做结构性修改。

提供 6 个改数据（非只读）的工具，全部 readOnlyHint=False、destructiveHint=True：

- ``stata_replace``   替换既有变量的取值（replace）
- ``stata_drop``      删除变量或观测（drop，二选一）
- ``stata_keep``      保留变量或观测（keep，二选一）
- ``stata_rename``    重命名变量（rename，含批量形式）
- ``stata_recode``    按规则组重编码取值（recode）
- ``stata_destring``  字符串变量转数值（destring）

这些工具都会**修改内存中的数据集**，且多数不可逆（replace/drop/keep/recode
直接覆盖原变量或删除数据）。执行前建议先用 stata_summarize / stata_list 确认
影响范围；确需保留原值的重构请改用 stata_generate 新建变量。
"""

from typing import Any


def register(mcp: Any, deps: Any) -> dict[str, Any]:
    """在 mcp 上注册本模块的全部数据重构工具。

    Returns:
        {工具名: 函数} —— 供主服务器 ``globals().update()`` 暴露为模块属性，
        与 server.py 内既有 50 个 ``stata_*`` 工具保持一致（E2E 与
        ``from server import stata_xxx`` 都依赖模块属性）。
    """

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def stata_replace(
        varname: str,
        expression: str,
        condition: str = "",
        in_range: str = "",
        options: str = "",
    ) -> str | deps.ToolResult:
        """替换既有变量的取值（replace）。

        **破坏性操作**：直接覆盖原变量的值，不可逆。想保留原值请改用
        ``stata_generate`` 新建变量。

        Args:
            varname: 要修改的变量名（须是合法标识符）。
            expression: 赋值表达式，自由文本，可含函数、运算符、宏引用，如
                "weight/100"、"ln(price)"、"age^2"、"(foreign==1)"。
            condition: if 条件子句（可选）—— 仅对满足条件的观测赋值，
                其余观测保持不变。
            in_range: in 观测范围子句（可选），如 "1/100"。
            options: 其他官方选项（可选），如 "nopromote"（不提升变量类型）。

        Returns:
            替换确认信息。

        示例:
            stata_replace("mpg", "weight/100", condition="foreign == 1")
            → "replace mpg = weight/100 if foreign == 1"
        """
        if err := deps.validate_identifier(varname, "varname", required=True):
            return deps.result_or_error(err)
        if not expression.strip():
            return deps.make_error("错误: 请提供赋值表达式 expression，如 weight/100 或 ln(price)")
        # 表达式拼在 [if] 之前，自由文本里的 // /* */ 会把保护性 if/in 整段注释掉、
        # 使破坏性命令静默作用于全部观测 —— 必须走 filter_expr 级校验（拒字符串外
        # 的注释记号/独立 using/未闭合引号），no_injection 级别不够。
        if err := deps.validate_filter_expr(expression, "expression"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(condition, "condition"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(in_range, "in_range"):
            return deps.result_or_error(err)
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)
        cmd = f"replace {varname} = {expression.strip()}"
        cmd += deps.filter_clause(condition, in_range)
        if options.strip():
            cmd += f", {options.strip()}"
        return deps.run_stata_command(cmd, timeout=60)

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def stata_drop(
        varlist: str = "",
        condition: str = "",
        in_range: str = "",
    ) -> str | deps.ToolResult:
        """删除变量或观测（drop）。

        两种形式**二选一**：
        - 删**变量**：给 varlist（"price mpg" 多变量，或 "_all" 删全部变量）。
        - 删**观测**：只给 condition/in_range，如 condition="foreign == 1"。

        同时给 varlist 与 condition/in_range 会报错 —— 官方 drop 语法里
        [varlist] 与 [if] [in] 不能同时出现，且语义互斥。这是不可逆的破坏性
        操作，执行前请先用 stata_summarize / stata_list 确认影响范围。

        Args:
            varlist: 要删除的变量（可 "_all"），与 condition/in_range 二选一。
            condition: if 条件子句（可选）—— 仅删除满足条件的观测。
            in_range: in 观测范围子句（可选），如 "1/20"。

        Returns:
            删除确认信息。
        """
        if varlist.strip() and (condition.strip() or in_range.strip()):
            return deps.make_error(
                "错误: 删除变量与删除观测二选一 —— 给 varlist 时不能再给 condition/in_range"
            )
        if not varlist.strip() and not condition.strip() and not in_range.strip():
            return deps.make_error(
                "错误: 请二选一 —— 给 varlist 删变量（如 price mpg 或 _all），"
                "或给 condition/in_range 删观测（如 condition=\"foreign == 1\"）"
            )
        if err := deps.validate_varlist(varlist, "varlist"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(condition, "condition"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(in_range, "in_range"):
            return deps.result_or_error(err)
        if varlist.strip():
            cmd = f"drop {varlist.strip()}"
        else:
            cmd = "drop" + deps.filter_clause(condition, in_range)
        return deps.run_stata_command(cmd, timeout=60)

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def stata_keep(
        varlist: str = "",
        condition: str = "",
        in_range: str = "",
    ) -> str | deps.ToolResult:
        """保留变量或观测，删除其余（keep）。

        与 drop 互为镜像，两种形式**二选一**：
        - 保**变量**：给 varlist（"price mpg" 多变量，或 "_all" 全部变量）。
        - 保**观测**：只给 condition/in_range，如 condition="foreign == 1"。

        同时给 varlist 与 condition/in_range 会报错 —— 官方 keep 语法里
        [varlist] 与 [if] [in] 不能同时出现。被剔除的变量/观测不可恢复，
        执行前请确认影响范围。

        Args:
            varlist: 要保留的变量，与 condition/in_range 二选一。
            condition: if 条件子句（可选）—— 仅保留满足条件的观测。
            in_range: in 观测范围子句（可选），如 "1/20"。

        Returns:
            保留确认信息。
        """
        if varlist.strip() and (condition.strip() or in_range.strip()):
            return deps.make_error(
                "错误: 保留变量与保留观测二选一 —— 给 varlist 时不能再给 condition/in_range"
            )
        if not varlist.strip() and not condition.strip() and not in_range.strip():
            return deps.make_error(
                "错误: 请二选一 —— 给 varlist 留变量（如 price mpg 或 _all），"
                "或给 condition/in_range 留观测（如 condition=\"foreign == 1\"）"
            )
        if err := deps.validate_varlist(varlist, "varlist"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(condition, "condition"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(in_range, "in_range"):
            return deps.result_or_error(err)
        if varlist.strip():
            cmd = f"keep {varlist.strip()}"
        else:
            cmd = "keep" + deps.filter_clause(condition, in_range)
        return deps.run_stata_command(cmd, timeout=60)

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def stata_rename(
        oldname: str,
        newname: str,
        options: str = "",
    ) -> str | deps.ToolResult:
        """重命名变量（rename）。

        支持两种形式：
        - 单变量：oldname/newname 各一个合法标识符。
        - 批量：oldname/newname 都以 "(" 开头，按位置逐个重命名（官方
          rename group 语法），如 rename (a b c) (x y z)。

        Args:
            oldname: 原变量名，或 "(" 开头的批量原变量组。
            newname: 新变量名，或 "(" 开头的批量新变量组（与 oldname 同长度）。
            options: 其他官方选项（可选），如 "dryrun"（仅报告不执行）。

        Returns:
            重命名确认信息。

        示例:
            stata_rename("price", "price_new") → "rename price price_new"
            stata_rename("(a b c)", "(x y z)") → "rename (a b c) (x y z)"
        """
        old_batch = oldname.strip().startswith("(")
        new_batch = newname.strip().startswith("(")
        if old_batch != new_batch:
            return deps.make_error(
                "错误: 批量形式要成对 —— oldname 与 newname 要么都用 (a b) 批量形式，"
                "要么都写单个变量名"
            )
        if old_batch:
            if err := deps.validate_no_injection(oldname, "oldname（批量形式）"):
                return deps.result_or_error(err)
        else:
            if err := deps.validate_identifier(oldname, "oldname", required=True):
                return deps.result_or_error(err)
        if new_batch:
            if err := deps.validate_no_injection(newname, "newname（批量形式）"):
                return deps.result_or_error(err)
        else:
            if err := deps.validate_identifier(newname, "newname", required=True):
                return deps.result_or_error(err)
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)
        cmd = f"rename {oldname.strip()} {newname.strip()}"
        if options.strip():
            cmd += f", {options.strip()}"
        return deps.run_stata_command(cmd, timeout=60)

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def stata_recode(
        varlist: str,
        values: str,
        condition: str = "",
        in_range: str = "",
        options: str = "",
    ) -> str | deps.ToolResult:
        """按规则组重编码变量取值（recode）。

        **破坏性操作**：values 规则组直接改写原变量的取值，不可逆；想保留
        原变量请给 options="generate(newvar)"。

        Args:
            varlist: 要重编码的变量（空格分隔可多个，如 "price mpg"）。
            values: 官方 (…) 规则组（已带括号），如 "(1=0) (2/4=1)"（把 1
                改为 0、2 到 4 改为 1）、"nonmiss=1"（非缺失改为 1）、
                "9=."（把 9 改为缺失）。范围 / 由 values 自由文本承载，
                不经过 varlist 校验。
            condition: if 条件子句（可选）。
            in_range: in 观测范围子句（可选）。
            options: 其他官方选项（可选），如 "generate(newvar)"、"prefix(newvar)"。

        Returns:
            重编码确认信息。

        示例:
            stata_recode("price", "(1=0) (2/4=1)")
            → "recode price (1=0) (2/4=1)"
        """
        if not varlist.strip():
            return deps.make_error("错误: 请提供要重编码的变量 varlist，如 price 或 price mpg")
        if err := deps.validate_varlist(varlist, "varlist"):
            return deps.result_or_error(err)
        if not values.strip():
            return deps.make_error(
                "错误: 请提供 recode 规则组 values（官方 (…) 规则组），"
                '如 "(1=0) (2/4=1)" 或 "nonmiss=1"'
            )
        # 与 expression 同理：values 拼在 [if] 之前，// 会注释掉保护性子句
        if err := deps.validate_filter_expr(values, "values"):
            return deps.result_or_error(err)
        # 官方规则：括号仅在「单变量且不定义值标签」时可省略；多变量（空格分隔
        # 或多个变量的范围 price-mpg）必须写括号组，否则拼出非法命令
        if "(" not in values and (" " in varlist.strip() or "-" in varlist.strip().split()[0]):
            return deps.make_error(
                "错误: 多变量 recode 必须给括号规则组 —— 官方仅对单变量允许裸规则"
                "（如 nonmiss=1），多变量请写成 (1=0) (2/4=1) 形式"
            )
        if err := deps.validate_filter_expr(condition, "condition"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(in_range, "in_range"):
            return deps.result_or_error(err)
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)
        cmd = f"recode {varlist.strip()} {values.strip()}"
        cmd += deps.filter_clause(condition, in_range)
        if options.strip():
            cmd += f", {options.strip()}"
        return deps.run_stata_command(cmd, timeout=60)

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def stata_destring(
        varlist: str = "",
        replace: bool = False,
        force: bool = False,
        ignore: str = "",
        options: str = "",
    ) -> str | deps.ToolResult:
        """把字符串变量转为数值变量（destring）。

        **必填二选一**：replace=True 直接覆盖原变量；或在 options 里给
        generate() / gen()（保留原字符串变量、新建数值变量）。两者都不给时
        destring 只报告错误而不写回（Stata 要求明确的输出目标）。

        Args:
            varlist: 要转换的变量（可空 = 全部字符串变量；也可 "price mpg"
                指定）。
            replace: 直接覆盖原变量（True 时字符串内容需可被解释为数值）。
            force: 忽略不可转换的值并置为缺失。与 ignore() 二选一，不能同用。
            ignore: 被当作缺失的字符串字面量，如 "-"（拼成 ignore("-")）。
            options: 其他官方选项（可选），如 "generate(newvar)"、"gen(newvar)"。

        Returns:
            转换确认信息。

        示例:
            stata_destring("price mpg", replace=True, force=True)
            → "destring price mpg, replace force"
        """
        if not replace and "generate(" not in options and "gen(" not in options:
            return deps.make_error(
                "错误: 必须二选一 —— replace=True 覆盖原变量，"
                "或在 options 里给 generate(newvar) 保留原变量新建数值变量"
            )
        if replace and ("generate(" in options or "gen(" in options):
            return deps.make_error(
                "错误: replace 与 generate() 互斥 —— 要么 replace=True 覆盖原变量，"
                "要么在 options 里给 generate(newvar) 新建数值变量"
            )
        if force and ignore.strip():
            return deps.make_error(
                "错误: force 与 ignore() 二选一 —— force 忽略全部不可转换值，"
                'ignore() 只把指定字符串（如 "-"）当缺失'
            )
        if err := deps.validate_varlist(varlist, "varlist"):
            return deps.result_or_error(err)
        if err := deps.validate_no_injection(ignore, "ignore"):
            return deps.result_or_error(err)
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)
        parts: list[str] = []
        if replace:
            parts.append("replace")
        if force:
            parts.append("force")
        if ignore.strip():
            parts.append(f'ignore("{ignore.strip()}")')
        if options.strip():
            parts.append(options.strip())
        cmd = "destring"
        if varlist.strip():
            cmd += f" {varlist.strip()}"
        if parts:
            cmd += ", " + " ".join(parts)
        return deps.run_stata_command(cmd, timeout=60)

    # 收集本模块的工具函数（只认 `stata_` 前缀的可调用局部变量）
    return {k: v for k, v in locals().items() if k.startswith("stata_") and callable(v)}
