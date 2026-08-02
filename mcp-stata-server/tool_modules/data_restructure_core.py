"""数据重构工具模块（core）：merge / append / reshape / collapse / frame / verify。

本模块在 server.py import 时通过 register(mcp, deps) 装配工具，deps 由主服务器
注入（见 register 的 deps 命名空间说明），模块自身**绝不** import server。

六个工具处理数据集的**结构**而非变量生成（后者在 data_restructure.py 的
replace/drop/keep/rename/recode/destring）：
- merge（横向合并）、append（纵向追加）、reshape（长宽互转）、collapse（按组聚合）
  都会改动当前数据集，故 readOnlyHint=False、destructiveHint=True。
- frame（多数据集 frame 管理）同理，destructiveHint=True。
- verify（数据完整性检查）标 readOnlyHint=True —— 它只回答「数据对不对」；
  ``duplicates drop``/``tag()`` 这类会改数据的子命令被挡在门外。

统一约定（与 server.py 既有工具一致）：
- varlist / identifier → deps.validate_varlist / deps.validate_identifier
- condition/in_range → deps.validate_no_injection（本族工具不接受 filter_expr
  的字符串外记法限制之外的写法），经 deps.filter_clause 拼在 **逗号之前**
- options → deps.validate_no_injection，拼在逗号之后
- 校验失败一律 return deps.result_or_error(err)；错误文本以 "错误: " 开头、中文
- merge/append 的路径解析经 deps.split_using_paths（晚绑定 server 的
  ``_split_using_paths``），require_file 经 deps.run_stata_command(...) 透传，
  触发锁内权威路径解析
"""
import os
import re
from typing import Any

# merge 的匹配基数（[D] merge）。m:m 官方明确不推荐，但仍是合法形式。
_MERGE_KINDS = ("1:1", "m:1", "1:m", "m:m")


# frame 的 action 集合与参数要求。
_FRAME_ACTIONS = frozenset(
    {"dir", "current", "create", "change", "drop", "copy", "rename"}
)
_FRAME_NEED_NAME = frozenset({"create", "change", "drop", "copy", "rename"})
_FRAME_NEED_NEWNAME = frozenset({"copy", "rename"})


# 数据校验：各自是独立命令，但都在回答「数据对不对」，故合成一个工具。
_VERIFY_CHECKS = frozenset({"count", "assert", "duplicates", "isid", "missing"})


def register(mcp: Any, deps: Any) -> dict[str, Any]:
    """在 mcp 上注册本模块的全部数据重构工具。

    Returns:
        {工具名: 函数} —— 供主服务器 ``globals().update()`` 暴露为模块属性，
        与 server.py 内既有 ``stata_*`` 工具保持一致（E2E 与
        ``from server import stata_merge`` 都依赖模块属性）。
    """

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def stata_frame(
        action: str = "dir",
        name: str = "",
        newname: str = "",
        timeout: int = 60,
    ) -> str | deps.ToolResult:
        """管理数据 frame —— 在内存中同时持有多个数据集（Stata 16+）。

        合并前把两份数据各放一个 frame、或一边建模一边保留原始数据时用得上。
        当前有哪些 frame 也可从 ``stata_status`` 看到。

        Args:
            action: ``dir``（默认，列出全部）/ ``current``（当前 frame 名）/
                ``create`` / ``change`` / ``drop``（均需 name）/
                ``copy`` / ``rename``（需 name 与 newname）。
            name: 目标 frame 名。
            newname: copy / rename 的新名字。
            timeout: 命令超时秒数（默认 60，钳制 10–1800）。长命令/大文件可显式调大。

        Returns:
            frame 清单或操作确认。
        """
        if action not in _FRAME_ACTIONS:
            return deps.make_error(
                f"错误: action 只能是 {sorted(_FRAME_ACTIONS)}（收到 {action!r}）"
            )
        if err := deps.validate_identifier(name, "name"):
            return deps.result_or_error(err)
        if err := deps.validate_identifier(newname, "newname"):
            return deps.result_or_error(err)
        if action in _FRAME_NEED_NAME and not name.strip():
            return deps.make_error(f'错误: action="{action}" 必须提供 name')
        if action in _FRAME_NEED_NEWNAME and not newname.strip():
            return deps.make_error(f'错误: action="{action}" 必须提供 newname')

        safe_timeout = max(10, min(timeout, 1800))
        if action == "dir":
            return deps.run_stata_command("frames dir", timeout=safe_timeout)
        if action == "current":
            # `frame pwf` = print working frame，与 c(frame) 等价但更自解释。
            return deps.run_stata_command("frame pwf", timeout=safe_timeout)
        if action in _FRAME_NEED_NEWNAME:
            return deps.run_stata_command(
                f"frame {action} {name.strip()} {newname.strip()}",
                timeout=safe_timeout,
            )
        return deps.run_stata_command(
            f"frame {action} {name.strip()}", timeout=safe_timeout
        )

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def stata_verify(
        check: str = "count",
        varlist: str = "",
        expression: str = "",
        condition: str = "",
        in_range: str = "",
        options: str = "",
        timeout: int = 60,
    ) -> str | deps.ToolResult:
        """数据完整性检查（``count`` / ``assert`` / ``duplicates`` / ``isid`` /
        ``misstable``）。

        分析前先跑一遍能挡掉大部分「结果诡异」的根因：重复键、缺失值、
        标识变量不唯一。

        Args:
            check: ``count``（默认，计数）/ ``assert``（断言，不成立即报错）/
                ``duplicates``（重复观测）/ ``isid``（varlist 是否唯一标识）/
                ``missing``（缺失值汇总，走 ``misstable summarize``）。
            varlist: 变量列表 —— ``duplicates`` / ``isid`` / ``missing`` 用。
                ``isid`` 必填。
            expression: ``assert`` 的断言表达式，如 "price > 0"。
            condition: if 条件子句 —— ``count`` / ``assert`` / ``duplicates`` 用。
            in_range: 观测范围（同上）。
            options: ``duplicates`` 的子命令（``report`` 默认 / ``list`` /
                ``examples`` / ``tag(newvar)`` / ``drop``），或其他官方选项。
            timeout: 命令超时秒数（默认 60，钳制 10–1800）。长命令/大文件可显式调大。

        Returns:
            检查结果；``assert`` 不成立时以错误结果返回。
        """
        if check not in _VERIFY_CHECKS:
            return deps.make_error(
                f"错误: check 只能是 {sorted(_VERIFY_CHECKS)}（收到 {check!r}）"
            )
        if err := deps.validate_varlist(varlist, "varlist"):
            return deps.result_or_error(err)
        for value, label in ((expression, "expression"), (condition, "condition"),
                             (in_range, "in_range"), (options, "options")):
            if err := deps.validate_no_injection(value, label):
                return deps.result_or_error(err)

        safe_timeout = max(10, min(timeout, 1800))
        if check == "count":
            return deps.run_stata_command(
                "count" + deps.filter_clause(condition, in_range), timeout=safe_timeout
            )

        if check == "assert":
            if not expression.strip():
                return deps.make_error('错误: check="assert" 必须提供 expression')
            cmd = f"assert {expression.strip()}" + deps.filter_clause(condition, in_range)
            if options.strip():
                cmd += f", {options.strip()}"
            return deps.run_stata_command(cmd, timeout=safe_timeout)

        if check == "duplicates":
            # duplicates 的第一个词是子命令而非选项，故从 options 取，缺省 report。
            sub = options.strip() or "report"
            # 本工具标 readOnlyHint=True，而 `drop` 删除观测、`tag()` 创建变量 ——
            # 都是「修改」而非「校验」。遵循 MCP 注解的客户端会对只读工具跳过确认，
            # 放行等于静默改数据。挡在门外，比给一个「除非传某个选项否则只读」的
            # 工具更安全；真要改数据走 stata_run，那里的注解是诚实的。
            if re.match(r"^(drop|tag)\b", sub, re.IGNORECASE):
                return deps.make_error(
                    f"错误: stata_verify 是只读工具，不执行会修改数据的 `duplicates {sub}`"
                    "（drop 删除观测、tag() 创建变量）。"
                    f'请改用 stata_run("duplicates {sub}")，那里会按非只读工具处理。'
                )
            cmd = f"duplicates {sub}"
            if varlist.strip():
                cmd += f" {varlist.strip()}"
            cmd += deps.filter_clause(condition, in_range)
            return deps.run_stata_command(cmd, timeout=safe_timeout)

        if check == "isid":
            if not varlist.strip():
                return deps.make_error(
                    '错误: check="isid" 必须提供 varlist（要检验唯一性的变量）'
                )
            cmd = f"isid {varlist.strip()}"
            if options.strip():
                cmd += f", {options.strip()}"
            return deps.run_stata_command(cmd, timeout=safe_timeout)

        cmd = "misstable summarize"
        if varlist.strip():
            cmd += f" {varlist.strip()}"
        cmd += deps.filter_clause(condition, in_range)
        if options.strip():
            cmd += f", {options.strip()}"
        return deps.run_stata_command(cmd, timeout=safe_timeout)

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def stata_merge(
        kind: str,
        keyvars: str,
        using: str,
        keepusing: str = "",
        condition: str = "",
        in_range: str = "",
        options: str = "",
        timeout: int = 120,
    ) -> str | deps.ToolResult:
        """横向合并数据集（``merge``）。

        官方语法：``merge 1:1|m:1|1:m|m:m varlist using filename [, options]``。
        合并结果记在 ``_merge`` 变量里（1=仅主数据、2=仅使用数据、3=两边都有），
        合并后用 ``stata_tabulate("_merge")`` 检查匹配情况。

        Args:
            kind: 匹配基数 —— "1:1" / "m:1" / "1:m" / "m:m"（官方不推荐 m:m）。
            keyvars: 匹配键变量（空格分隔）；按观测号合并时传 "_n"。
            using: 被合并的 .dta 文件路径。
            keepusing: 只从使用数据中带入这些变量（留空 = 全部）。
            condition: ``merge`` 官方语法不支持 if；传入会明确拒绝，避免生成非法命令。
            in_range: ``merge`` 官方语法不支持 in；传入会明确拒绝，避免生成非法命令。
            options: 官方选项，如 "nogenerate"、"keep(match)"、"assert(match)"、
                "update replace"、"force"、"noreport"。
            timeout: 命令超时秒数（默认 120，钳制 10–1800）。长命令/大文件可显式调大。

        Returns:
            合并结果的匹配汇总表。
        """
        if kind not in _MERGE_KINDS:
            return deps.make_error(
                f"错误: kind 只能是 {list(_MERGE_KINDS)}（收到 {kind!r}）"
            )
        if not keyvars.strip():
            return deps.make_error('错误: 请提供匹配键变量（按观测号合并传 "_n"）')
        if err := deps.validate_varlist(keyvars, "keyvars"):
            return deps.result_or_error(err)
        if err := deps.validate_varlist(keepusing, "keepusing"):
            return deps.result_or_error(err)
        for value, label in ((condition, "condition"), (in_range, "in_range"),
                             (options, "options")):
            if err := deps.validate_no_injection(value, label):
                return deps.result_or_error(err)
        if condition.strip() or in_range.strip():
            return deps.make_error(
                "错误: merge 官方语法不支持 condition/in_range（if/in）。"
                "请先用 stata_use_dataset 筛选主数据，并为 using 数据另存筛选后的 .dta，"
                "再执行 merge。"
            )
        paths, err = deps.split_using_paths(using, single=True)
        if err:
            return deps.result_or_error(err)

        cmd = f'merge {kind} {keyvars.strip()} using "{paths[0]}"'
        opts = " ".join(
            p for p in (
                f"keepusing({keepusing.strip()})" if keepusing.strip() else "",
                options.strip(),
            ) if p
        )
        if opts:
            cmd += f", {opts}"
        safe_timeout = max(10, min(timeout, 1800))
        return deps.run_stata_command(cmd, timeout=safe_timeout, require_file=paths[0])

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def stata_append(
        using: str,
        options: str = "",
        timeout: int = 120,
    ) -> str | deps.ToolResult:
        """纵向追加数据集（``append``）。

        官方语法：``append using filename [filename …] [, options]`` —— 可一次
        接多个文件。变量按名字对齐，缺的补缺失值。

        Args:
            using: 一个或多个 .dta 文件路径（空格分隔）。
            options: 官方选项，如 "generate(src)"（标记来源）、"keep(varlist)"、
                "nolabel"、"nonotes"、"force"（允许字符/数值类型不一致）。
            timeout: 命令超时秒数（默认 120，钳制 10–1800）。长命令/大文件可显式调大。

        Returns:
            追加确认信息。
        """
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)
        paths, err = deps.split_using_paths(using)
        if err:
            return deps.result_or_error(err)
        missing = [path for path in paths if not os.path.isfile(path)]
        if missing:
            shown = ", ".join(missing[:3])
            if len(missing) > 3:
                shown += f" 等 {len(missing)} 个文件"
            return deps.make_error(f"错误: 文件不存在 — {shown}")

        cmd = "append using " + " ".join(f'"{p}"' for p in paths)
        if options.strip():
            cmd += f", {options.strip()}"
        safe_timeout = max(10, min(timeout, 1800))
        return deps.run_stata_command(cmd, timeout=safe_timeout, require_file=paths[0])

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def stata_reshape(
        direction: str,
        stub: str,
        i: str,
        j: str = "",
        options: str = "",
        timeout: int = 120,
    ) -> str | deps.ToolResult:
        """长宽表互转（``reshape``）。

        官方语法：``reshape long|wide stub, i(i) j(j)``。
        面板分析（``xtreg`` 等）要求**长表**：每个个体-时点一行。

        ``long``：宽转长，把 ``inc1980 inc1981`` 合成 ``inc`` 加一列 ``year``。
        ``wide``：长转宽，反向操作。

        **非数值 j 必须传 ``options="string"``**：宽表后缀是字符（如 bpwide 的
        ``bp_before``/``bp_after``，j 取 before/after）时，long 与 wide 两个方向
        都报错（r(498) 变量含全部缺失值 / r(109) 类型不匹配）—— 两个方向都要
        加 ``options="string"``。

        Args:
            direction: "long" 或 "wide"。
            stub: 变量名前缀，如 "inc"（对应 inc1980、inc1981 …）。可给多个。
            i: 个体标识变量（转换前后都唯一标识一行/一组）。
            j: 区分列的变量 —— long 方向可省略（Stata 默认新建 ``_j``），wide
               方向必须提供一个已存在的 j 变量。
            options: 官方选项，如 "string"（j 是字符串，非数值后缀必传）、
               "atwl(_)"。
            timeout: 命令超时秒数（默认 120，钳制 10–1800）。长命令/大文件可显式调大。

        Returns:
            转换前后的形态汇总。
        """
        if direction not in ("long", "wide"):
            return deps.make_error(
                f'错误: direction 只能是 "long" 或 "wide"（收到 {direction!r}）'
            )
        if not stub.strip():
            return deps.make_error('错误: 请提供变量名前缀 stub，如 "inc"')
        if not i.strip():
            return deps.make_error("错误: 请提供个体标识变量 i")
        if err := deps.validate_varlist(stub, "stub"):
            return deps.result_or_error(err)
        if err := deps.validate_varlist(i, "i"):
            return deps.result_or_error(err)
        if err := deps.validate_varlist(j, "j"):
            return deps.result_or_error(err)
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)
        if direction == "wide" and not j.strip():
            return deps.make_error(
                '错误: direction="wide" 时必须提供 j（已存在的区分列变量）'
            )

        cmd = f"reshape {direction} {stub.strip()}, i({i.strip()})"
        if j.strip():
            cmd += f" j({j.strip()})"
        if options.strip():
            cmd += f" {options.strip()}"
        safe_timeout = max(10, min(timeout, 1800))
        return deps.run_stata_command(cmd, timeout=safe_timeout)

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def stata_collapse(
        clist: str,
        by: str = "",
        condition: str = "",
        in_range: str = "",
        options: str = "",
        timeout: int = 120,
    ) -> str | deps.ToolResult:
        """按组聚合，把数据集**就地替换**为汇总结果（``collapse``）。

        官方语法：``collapse clist [if] [in] [weight] [, by(varlist) options]``。
        **原始数据会被替换** —— 需要保留请先 ``stata_save_dataset`` 或用
        ``stata_run("preserve")`` / ``restore``。

        Args:
            clist: 聚合表达式，如 ``"(mean) price (sd) mpg"``、
                ``"(sum) sales (max) peak=price"``（可给目标变量名）。
                统计量：mean（默认）/ median / sd / sum / count / min / max /
                first / last / p1–p99 等。
            by: 分组变量（空格分隔），留空 = 整体聚合成一行。
            condition: if 条件子句（可选）。
            in_range: 观测范围（可选）。
            options: 官方选项，如 "cw"（成列删除缺失）、"fast"（不 preserve）。
            timeout: 命令超时秒数（默认 120，钳制 10–1800）。长命令/大文件可显式调大。

        Returns:
            聚合确认信息。
        """
        if not clist.strip():
            return deps.make_error(
                '错误: 请提供聚合表达式 clist，如 "(mean) price (sd) mpg"'
            )
        if err := deps.validate_varlist(by, "by"):
            return deps.result_or_error(err)
        for value, label in ((clist, "clist"), (condition, "condition"),
                             (in_range, "in_range"), (options, "options")):
            if err := deps.validate_no_injection(value, label):
                return deps.result_or_error(err)

        cmd = f"collapse {clist.strip()}"
        cmd += deps.filter_clause(condition, in_range)
        opts = " ".join(
            p for p in (f"by({by.strip()})" if by.strip() else "", options.strip()) if p
        )
        if opts:
            cmd += f", {opts}"
        safe_timeout = max(10, min(timeout, 1800))
        return deps.run_stata_command(cmd, timeout=safe_timeout)

    # 收集本模块的工具函数（只认 `stata_` 前缀的可调用局部变量）
    return {k: v for k, v in locals().items() if k.startswith("stata_") and callable(v)}
