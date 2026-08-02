"""数据 I/O 工具模块：use_dataset / import / save_dataset / set_cwd / use_example。

本模块在 server.py import 时通过 register(mcp, deps) 装配工具，deps 由主服务器
注入（见 register 的 deps 命名空间说明），模块自身**绝不** import server。

五个工具覆盖数据集的载入、导入、保存与工作目录切换：
- use_dataset（`use`，.dta 载入）、save_dataset（`save`，.dta 落盘）——
  文件路径经入口预检（deps.validate_path）后嵌入命令，存在性交由
  deps.run_stata_command(...) 的 require_file 透传，触发锁内权威路径解析。
- import（官方 `import` 命令族 excel/delimited/sas/spss/dbase/parquet）——
  约三百行格式分派逻辑与 `_IMPORT_*` 常量族、`_resolve_import_path` 均在本模块
  模块级（这些符号只被 import 用到，不留在 server.py）。
- set_cwd（`cd`）、use_example（`sysuse`/`webuse`）。

统一约定（与 server.py 既有工具一致）：
- varlist / identifier → deps.validate_varlist / deps.validate_identifier
- condition/in_range → deps.validate_filter_expr（接受字符串内记法），经
  deps.filter_clause 拼在 **逗号之前**
- options / sheet / delimiter / encoding → deps.validate_no_injection /
  deps.validate_sheet_name / deps.validate_delimiter 等
- 校验失败一律 return deps.result_or_error(err)；错误文本以 "错误: " 开头、中文
- 全部五个工具都改数据 / 换目录，readOnlyHint=False、destructiveHint=True
"""

import os
import re
from typing import Any

# 模块级纯路径助手只被 _resolve_import_path 用（stata_helpers 是纯辅助层，
# 不触碰服务器状态；工具内部的同类调用统一走 deps，保持晚绑定可 patch）。
from stata_helpers import _normalize_path, _path_has_extension  # noqa: E402

# 扩展名 → 官方 import 子命令（依据 [D] import 的方法表）。
# .dta 不在此列 —— 它走 `use`，不属于 import 命令族。
_IMPORT_FORMAT_BY_EXT = {
    ".xlsx": "excel",
    ".xls": "excel",
    ".csv": "delimited",
    ".tsv": "delimited",
    ".txt": "delimited",
    ".dat": "delimited",
    ".sas7bdat": "sas",
    ".sav": "spss",
    ".zsav": "spss",
    ".dbf": "dbase",
    ".parquet": "parquet",
}
_IMPORT_FORMATS = frozenset(_IMPORT_FORMAT_BY_EXT.values())
# 各选项的适用格式（实测：传给不适用的格式一律 r(198)）。
# cellrange 只可能是 A1、A1:B10 这类单元格引用；varnames 只可能是行号或 nonames。
# 二者都被**裸拼**进 opt(...)，故用正向白名单而非黑名单 —— 一个 `)` 就能逃逸。
_IMPORT_CELLRANGE_RE = re.compile(r"^[A-Za-z]+\d+(:[A-Za-z]+\d+)?$")
_IMPORT_VARNAMES_RE = re.compile(r"^(\d+|nonames)$", re.IGNORECASE)

_IMPORT_EXCEL_ONLY = frozenset({"excel"})
_IMPORT_DELIMITED_ONLY = frozenset({"delimited"})
# Stata 的 import sas/spss 也接受 encoding()；此前只给 delimited 放行，
# 导致调用方传入编码后被静默丢弃。
_IMPORT_ENCODING_FORMATS = frozenset({"delimited", "sas", "spss"})
# 显式 format 且文件名没有扩展名时，优先使用已有候选文件；都不存在时
# 仍选该格式的默认扩展名，让 require_file 与真正送给 Stata 的路径一致。
_IMPORT_DEFAULT_EXTENSIONS = {
    "excel": (".xlsx", ".xls"),
    "delimited": (".csv", ".tsv", ".txt", ".dat"),
    "sas": (".sas7bdat",),
    "spss": (".sav", ".zsav"),
    "dbase": (".dbf",),
    "parquet": (".parquet",),
}
# case() 除 parquet 外各格式都有。
_IMPORT_CASE_FORMATS = frozenset({"excel", "delimited", "sas", "spss", "dbase"})
# [if] [in] 只有 sas / spss 有（`import sas [namelist] [if] [in] using file`）。
_IMPORT_FILTER_FORMATS = frozenset({"sas", "spss"})
# varlist 位置的**语义随格式而变**，不能统一映射：
#   sas/spss 的 namelist 与 parquet 的 columnlist 是「只导入这些列」（筛选）；
#   excel/delimited 的 extvarlist 却是「给导入的列命名」（重命名）。
# 把重命名当筛选用会静默导入错的数据，故只对筛选语义的三种格式放行。
_IMPORT_SELECT_FORMATS = frozenset({"sas", "spss", "parquet"})


def _resolve_import_path(filepath: str, fmt: str) -> str:
    """解析显式 import 格式的无扩展名路径，并返回实际候选文件路径。"""
    normalized = _normalize_path(filepath)
    if _path_has_extension(normalized):
        return normalized
    candidates = [normalized]
    candidates.extend(f"{normalized}{ext}" for ext in _IMPORT_DEFAULT_EXTENSIONS.get(fmt, ()))
    # 先保留一个真正存在的无扩展名文件；否则按官方默认扩展名优先级选取。
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return candidates[1] if len(candidates) > 1 else normalized


def register(mcp: Any, deps: Any) -> dict[str, Any]:
    """在 mcp 上注册本模块的全部数据 I/O 工具。

    Returns:
        {工具名: 函数} —— 供主服务器 ``globals().update()`` 暴露为模块属性，
        与 server.py 内既有 ``stata_*`` 工具保持一致（E2E 与
        ``from server import stata_use_dataset`` 都依赖模块属性）。
    """

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def stata_use_dataset(
        filepath: str,
        clear: bool = True,
        varlist: str = "",
        condition: str = "",
        in_range: str = "",
        options: str = "",
        timeout: int = 60,
    ) -> str | deps.ToolResult:
        """加载 Stata 数据集 (.dta 文件) 到内存中。

        加载后可使用 stata_describe、stata_summarize 等工具查看数据。

        官方语法允许**只载入子集**（``use [varlist] using file [if] [in]``），
        大数据集上先筛再载比全量载入后 drop 省内存。

        Args:
            filepath: .dta 文件的绝对路径。
            clear: 是否先清除内存中的已有数据（默认 True）。
            varlist: 只载入这些变量（空格分隔），留空 = 全部。
            condition: if 条件子句（可选）—— 只载入满足条件的观测。
            in_range: 观测范围（可选），如 "1/1000"。
            timeout: 命令超时秒数（默认 60，钳制 10–1800）。长命令/大文件可显式调大。

        Returns:
            数据集加载确认信息及变量列表。
        """
        if err := deps.validate_varlist(varlist, "varlist"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(condition, "condition"):
            return deps.result_or_error(err)
        if err := deps.validate_filter_expr(in_range, "in_range"):
            return deps.result_or_error(err)
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)
        normalized = deps.normalize_path(filepath)
        # 指定 varlist 时官方语法要求写成 `use <varlist> using "file"`。
        if varlist.strip():
            cmd = f'use {varlist.strip()} using "{normalized}"'
        else:
            cmd = f'use "{normalized}"'
        cmd += deps.filter_clause(condition, in_range)
        opts = " ".join(p for p in ("clear" if clear else "", options.strip()) if p)
        if opts:
            cmd += f", {opts}"
        safe_timeout = max(10, min(timeout, 1800))
        return deps.run_stata_command(cmd, timeout=safe_timeout, require_file=filepath)

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def stata_save_dataset(
        filepath: str, replace: bool = False, options: str = "", timeout: int = 60
    ) -> str | deps.ToolResult:
        """将当前内存中的数据集保存为 .dta 文件。

        Args:
            filepath: 保存路径（建议使用 .dta 扩展名）。
            replace: 是否覆盖已有文件（默认 False）。
            timeout: 命令超时秒数（默认 60，钳制 10–1800）。长命令/大文件可显式调大。

        Returns:
            保存确认信息。
        """
        if err := deps.validate_path(filepath):
            return deps.result_or_error(err)
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)
        normalized = deps.normalize_path(filepath)
        opts = " ".join(p for p in ("replace" if replace else "", options.strip()) if p)
        suffix = f", {opts}" if opts else ""
        safe_timeout = max(10, min(timeout, 1800))
        result = deps.run_stata_command(f'save "{normalized}"{suffix}', timeout=safe_timeout)
        if isinstance(result, deps.ToolResult):
            return result  # 保存失败，不登记资源
        reg_err = deps.register_resource(normalized, "stata_save_dataset")
        if reg_err is None:
            return deps.append_text(result, f"\n(已登记为资源: {deps.resource_uri(normalized)})")
        return deps.append_text(result, f"\n(文件已保存，但登记为资源失败: {reg_err})")

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def stata_set_cwd(path: str, timeout: int = 60) -> str | deps.ToolResult:
        """更改 Stata 的工作目录。

        Args:
            path: 新的工作目录路径。
            timeout: 命令超时秒数（默认 60，钳制 10–1800）。长命令/大文件可显式调大。

        Returns:
            当前工作目录确认信息。
        """
        if err := deps.validate_path(path):
            return deps.result_or_error(err)
        safe_timeout = max(10, min(timeout, 1800))
        return deps.run_stata_command(f'cd "{deps.normalize_path(path)}"', timeout=safe_timeout)

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def stata_use_example(
        name: str = "",
        source: str = "sysuse",
        clear: bool = True,
        action: str = "load",
        timeout: int = 0,
    ) -> str | deps.ToolResult:
        """加载 Stata 官方示例数据集（``sysuse`` / ``webuse``）。

        ``sysuse`` 用随 Stata 分发的本地数据集（auto、census、nlsw88 …，无需联网）；
        ``webuse`` 从 Stata Press 取（nlswork、lbw、grunfeld …，**需联网**）。
        验证分析流程或复现手册示例时最常用。

        Args:
            name: 数据集名，不含 .dta（如 "auto"、"nlswork"）。
            source: ``sysuse``（默认，本地）或 ``webuse``（联网）。
            clear: 加载前清空内存数据（默认 True）。
            action: ``load``（默认）或 ``list`` —— 列出本地可用示例
                （``sysuse dir``；webuse 没有对应子命令）。
            timeout: 命令超时秒数（钳制 10–1800）。webuse 联网取数默认 120、
                其余默认 60，显式传值优先。长命令/大文件可显式调大。

        Returns:
            加载确认与数据集概览。
        """
        if source not in ("sysuse", "webuse"):
            return deps.make_error(f'错误: source 只能是 "sysuse" 或 "webuse"（收到 {source!r}）')
        if action not in ("load", "list"):
            return deps.make_error(f'错误: action 只能是 "load" 或 "list"（收到 {action!r}）')
        # timeout=0（默认）表示「自动」：webuse 联网取数给足 120s，其余 60s；
        # 显式传值则以用户指定的为准。
        effective = timeout if timeout else (120 if source == "webuse" else 60)
        safe_timeout = max(10, min(effective, 1800))
        if action == "list":
            # webuse 没有 dir 子命令，列表一律走本地 sysuse dir。
            return deps.run_stata_command("sysuse dir", timeout=safe_timeout)
        if not name.strip():
            return deps.make_error('错误: 请提供数据集名，如 name="auto"')
        if err := deps.validate_identifier(name, "name", required=True):
            return deps.result_or_error(err)

        cmd = f"{source} {name.strip()}"
        if clear:
            cmd += ", clear"
        # webuse 要联网取数，给足超时。
        return deps.run_stata_command(cmd, timeout=safe_timeout)

    @mcp.tool(annotations=deps.ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def stata_import(
        filepath: str,
        format: str = "auto",
        clear: bool = True,
        sheet: str = "",
        cellrange: str = "",
        firstrow: bool = False,
        delimiter: str = "",
        varnames: str = "",
        encoding: str = "",
        case: str = "",
        varlist: str = "",
        condition: str = "",
        in_range: str = "",
        options: str = "",
        timeout: int = 120,
    ) -> str | deps.ToolResult:
        """导入非 .dta 格式的数据文件（与 stata_export_* 对称）。

        覆盖官方 ``import`` 命令族：excel / delimited / sas / spss / dbase / parquet。
        ``.dta`` 请用 ``stata_use_dataset``（它属于 ``use``，不是 import）。

        **不适用于目标格式的选项会被丢弃并说明** —— 实测传错会 r(198)：
        ``firstrow`` / ``sheet`` / ``cellrange`` 仅 excel；``delimiter`` /
        ``varnames`` 仅 delimited；``encoding`` 支持 delimited / sas / spss；
        ``[namelist] [if] [in]`` 支持 sas / spss。

        Args:
            filepath: 数据文件路径。
            format: 留空/``"auto"`` 时按扩展名推断；也可显式指定
                excel / delimited / sas / spss / dbase / parquet。
            clear: 导入前清空内存中的数据（默认 True）。
            sheet: Excel 工作表名。**仅 excel**。
            cellrange: Excel 单元格范围，如 "A1:C10"。**仅 excel**。
            firstrow: 用首行作变量名。**仅 excel**（delimited 用 varnames）。
            delimiter: 分隔符 —— 单字符或关键字 ``"tab"``。**仅 delimited**。
            varnames: 变量名所在行号，或 ``"nonames"``。**仅 delimited**。
            encoding: 文件编码，如 "utf-8"、"gbk"。支持 delimited / sas / spss。
            case: 变量名大小写 —— preserve / lower / upper。支持 excel /
                delimited / sas / spss / dbase（parquet 除外）。
            varlist: 只导入这些变量。支持 sas / spss / parquet。
            condition: if 条件子句。支持 sas / spss。
            in_range: 观测范围。支持 sas / spss。
            options: 其余官方选项的自由文本逃生舱。
            timeout: 命令超时秒数（默认 120，钳制 10–1800）。长命令/大文件可显式调大。

        Returns:
            导入确认信息（含变量与观测数概览）。
        """
        if err := deps.validate_path(filepath):
            return deps.result_or_error(err)
        if err := deps.validate_varlist(varlist, "varlist"):
            return deps.result_or_error(err)
        for value, label in ((condition, "condition"), (in_range, "in_range")):
            if err := deps.validate_filter_expr(value, label):
                return deps.result_or_error(err)
        if err := deps.validate_no_injection(options, "options"):
            return deps.result_or_error(err)
        # 这几项都被拼进 `opt("<值>")` 或 `opt(<值>)`，校验强度必须与
        # stata_export_excel 的 sheet 对称 —— 后者走 _validate_sheet_name 明确拒绝
        # `"`，而此处曾混在 _validate_no_injection 那批里（只拒换行/回车/空字节/分号），
        # 于是同一个值在两个工具里下场完全相反：`S1") cellrange(A1:A1) //` 能提前
        # 闭合引号并注入任意 import 选项。
        if err := deps.validate_sheet_name(sheet):
            return deps.result_or_error(err.replace("工作表名", "sheet"))
        for value, label, pattern in (
            (cellrange, "cellrange", _IMPORT_CELLRANGE_RE),
            (varnames, "varnames", _IMPORT_VARNAMES_RE),
        ):
            if value.strip() and not pattern.match(value.strip()):
                return deps.make_error(
                    f"错误: {label} 格式非法（收到 {value!r}）—— 它被原样拼进 "
                    f"{label}(...)，含 ) 或引号即可逃逸出括号注入其他选项"
                )
        if err := deps.validate_no_injection(encoding, "encoding"):
            return deps.result_or_error(err)
        if encoding and any(ch in encoding for ch in ('"', "(", ")", "`", "$")):
            return deps.make_error(
                '错误: encoding 不能包含引号、括号、反引号或 $（它被拼进 encoding("...")）'
            )
        if case and case not in ("preserve", "lower", "upper"):
            return deps.make_error(
                f'错误: case 只能是 "preserve" / "lower" / "upper"（收到 {case!r}）'
            )

        ext = os.path.splitext(filepath)[1].lower()
        if format in ("", "auto"):
            if ext == ".dta":
                return deps.make_error(
                    "错误: .dta 不属于 import 命令族，请改用 "
                    'stata_use_dataset("路径")（底层是 `use`）。'
                )
            fmt = _IMPORT_FORMAT_BY_EXT.get(ext)
            if not fmt:
                return deps.make_error(
                    f"错误: 无法从扩展名 {ext or '(无)'} 推断导入格式。"
                    f"请显式指定 format={sorted(_IMPORT_FORMATS)}，"
                    "或用 stata_run 执行官方 import 命令。"
                )
        elif format not in _IMPORT_FORMATS:
            return deps.make_error(
                f"错误: format 只能是 {sorted(_IMPORT_FORMATS)}（收到 {format!r}）"
            )
        else:
            fmt = format

        import_path = deps.normalize_path(filepath)
        require_path = filepath
        if format not in ("", "auto") and not deps.path_has_extension(import_path):
            import_path = _resolve_import_path(filepath, fmt)
            if import_path != deps.normalize_path(filepath):
                # 相对路径仍交给 _resolve_stata_path_locked 按 Stata cwd 解析；
                # 只把我们选中的隐式扩展名附在原始参数上，避免改变 cwd 语义。
                suffix = os.path.splitext(import_path)[1]
                require_path = deps.append_default_extension(filepath, suffix)
        opts, dropped = [], []

        def _take(value, allowed, label, rendered):
            if not value:
                return
            if fmt in allowed:
                opts.append(rendered)
            else:
                dropped.append(label)

        _take(sheet, _IMPORT_EXCEL_ONLY, "sheet", f'sheet("{sheet}")')
        _take(cellrange, _IMPORT_EXCEL_ONLY, "cellrange", f"cellrange({cellrange.strip()})")
        _take(firstrow, _IMPORT_EXCEL_ONLY, "firstrow", "firstrow")
        if delimiter:
            if err := deps.validate_delimiter(delimiter):
                return deps.result_or_error(err)
            if fmt not in _IMPORT_DELIMITED_ONLY:
                dropped.append("delimiter")
            elif delimiter == "tab":
                opts.append("delimiters(tab)")
            else:
                opts.append(f'delimiters("{delimiter}")')
        _take(varnames, _IMPORT_DELIMITED_ONLY, "varnames", f"varnames({varnames.strip()})")
        _take(encoding, _IMPORT_ENCODING_FORMATS, "encoding", f'encoding("{encoding.strip()}")')
        _take(case, _IMPORT_CASE_FORMATS, "case", f"case({case})")
        if clear:
            opts.append("clear")
        if options.strip():
            opts.append(options.strip())

        cmd = f"import {fmt}"
        extra_note = ""
        if varlist.strip():
            if fmt in _IMPORT_SELECT_FORMATS:
                cmd += f" {varlist.strip()}"
            else:
                dropped.append("varlist")
                # 不能顺手拼上去：excel/delimited 的同一语法位置是 extvarlist
                # （给导入列**命名**），当筛选用会静默导入错的数据。
                extra_note = (
                    f"\n注意：{fmt} 在该语法位置上是 extvarlist（给导入的列命名），"
                    "与 varlist 的「只导入这些列」语义不同，故未套用。"
                    "确需重命名请走 options。"
                )
        if fmt in _IMPORT_FILTER_FORMATS:
            cmd += deps.filter_clause(condition, in_range)
        else:
            for value, label in ((condition, "condition"), (in_range, "in_range")):
                if value.strip():
                    dropped.append(label)
        cmd += f' using "{import_path}"'
        if opts:
            cmd += f", {' '.join(opts)}"

        safe_timeout = max(10, min(timeout, 1800))
        result = deps.run_stata_command(cmd, timeout=safe_timeout, require_file=require_path)
        if isinstance(result, deps.ToolResult):
            return result
        if dropped:
            result += (
                f"\n提示：{fmt} 格式不支持 {', '.join(dropped)}"
                "（Stata 会报 option ... not allowed），已忽略。"
            )
        return result + extra_note

    # 收集本模块的工具函数（只认 `stata_` 前缀的可调用局部变量）
    return {k: v for k, v in locals().items() if k.startswith("stata_") and callable(v)}
