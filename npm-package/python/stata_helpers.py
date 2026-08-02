"""server.py 的纯辅助层：无服务器状态的校验、解析、路径与格式化助手。

这些函数只依赖 ``os`` / ``re`` / ``mimetypes`` / ``urllib.parse`` 与本模块
内部的同组函数、常量，**不触碰** ``_stata_lock`` / ``_last_output`` /
``_resource_registry`` / ``config`` / ``mcp`` 等服务器状态，因此被整体搬到
本模块。server.py 通过 ``from stata_helpers import ...`` 以同名属性重导出，
保证 ``patch("server.<name>")`` 与 ``from server import <name>`` 的测试面不变。

依赖有状态路径校验链（``_validate_path`` → ``_ALLOWED_ROOTS_CACHE``）的函数
（如 ``_split_using_paths``）与依赖执行层的函数（如 ``_file_written_since`` /
``_mtime_ns``）仍留在 server.py —— 它们不是纯辅助函数。
"""

import mimetypes
import os
import re
from urllib.parse import quote

# =============================================================================
# 分页
# =============================================================================

# 分页阈值：超过此大小自动分页
PAGE_SIZE = 4_000


def _paginate(text: str, page: int, page_size: int = PAGE_SIZE, truncated: bool = False) -> str:
    """将文本分页，返回指定页及导航信息。

    截断感知：``truncated=True`` 时页首给出明确提示 —— 截断原文在文本末尾，
    翻到最后一页才看得到，首页用户会误把截断总量当完整输出。

    Args:
        text: 完整文本
        page: 页码（1-based），0 表示返回全部
        page_size: 每页字符数

    Returns:
        指定页内容 + 导航信息
    """
    if not text:
        return "(无输出)"

    # page == 0 或 page_size <= 0 均表示「返回全部，不分页」；
    # 必须在计算 total_pages 前拦截，避免 page_size <= 0 时除零。
    if page == 0 or page_size <= 0:
        return text

    total_chars = len(text)
    total_pages = max(1, (total_chars + page_size - 1) // page_size)

    if page < 1:
        page = 1
    if page > total_pages:
        page = total_pages

    start = (page - 1) * page_size
    end = min(start + page_size, total_chars)

    chunk = text[start:end]
    header = f"── 第 {page}/{total_pages} 页（共 {total_chars} 字符）──\n"
    if truncated:
        header += "⚠ 输出已截断（超过 120K 上限，后半段已丢弃；缩小范围或用 save_output 取全量）\n"
    footer = f"\n── 第 {page}/{total_pages} 页"
    if page < total_pages:
        footer += f" — 使用 stata_more(page={page + 1}) 翻下页"
    if page > 1:
        footer += f" — stata_more(page={page - 1}) 翻上页"
    footer += " — stata_more(page=0) 显示全部"

    return header + chunk + footer


# =============================================================================
# 输入安全：正则白名单
# =============================================================================

# 输入安全：允许的 Stata 标识符（变量/包名）字符集合
# Stata 变量名最大 32 字符，必须以字母或下划线开头，后续可为字母/数字/下划线
_STATA_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,31}$")
# 图形方案名：字母、数字、下划线、连字符，可数字开头（538、s1color-asterisk）
_SCHEME_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# 允许的包来源：ssc 或 HTTPS URL。
# 主机段允许 :port（企业内网镜像常用），主机段后只允许 URL 安全字符
# （不含 ) ( 空白 引号 ; ` $），防止 source 提前闭合 from() 注入额外参数。
_INSTALL_SOURCE_RE = re.compile(
    r"^https://[a-zA-Z0-9][-a-zA-Z0-9.]*(:\d+)?(/[^\s()\";`$]*)?$", re.IGNORECASE
)

_CELL_REFERENCE_RE = re.compile(r"^[A-Za-z]+[1-9][0-9]*$")

# generate/egen 的 [type] 位置：官方允许的存储类型（str# / strL 亦合法）。
# 用白名单而非黑名单 —— 该值直接拼进命令的关键字位置，不容许任何自由文本。
_STORAGE_TYPE_RE = re.compile(r"^(byte|int|long|float|double|str[0-9]{1,4}|strL)$")


# =============================================================================
# 校验函数
# =============================================================================

# 危险字符：换行、回车、空字节、分号（可能分割命令）
_INJECTABLE_CHARS = {"\n", "\r", "\x00", ";"}
# varlist 中额外需要注意的 shell/Stata 元字符
_VARLIST_FORBIDDEN_CHARS = {"\n", "\r", "\x00", ";", "!", "|", "&", "`", "$"}


def _contains_injection_chars(value: str) -> bool:
    """检查字符串是否包含可能导致命令注入的分隔/控制字符。"""
    return bool(value) and any(ch in _INJECTABLE_CHARS for ch in value)


def _validate_no_injection(value: str, label: str = "参数") -> str | None:
    """拒绝含注入字符的输入；返回错误文本或 None。"""
    if value is None:
        return None
    if _contains_injection_chars(value):
        return f"错误: {label} 包含非法字符（换行、回车、空字节或分号）"
    return None


def _validate_identifier(value: str, label: str = "变量名", required: bool = False) -> str | None:
    """校验单个 Stata 标识符格式。

    Args:
        value: 待校验的标识符。
        label: 出错信息里的参数名。
        required: 该参数是否必填。空值对可选参数（如 ``byvar``）是合法的
            「不使用」，但对必填参数会静默产生错误结果 —— 实测
            ``stata_regress(depvar="", indepvars="weight")`` 拼出
            ``regress  weight``，Stata 把 weight 当因变量跑出一个**完全不同的
            回归**并返回成功。这种静默算错比报错危险得多。
    """
    if not value or not value.strip():
        if required:
            return f"错误: {label} 不能为空"
        return None
    value = value.strip()
    if _contains_injection_chars(value):
        return f"错误: {label} 包含非法字符"
    if not _STATA_IDENTIFIER_RE.match(value):
        return (
            f"错误: {label} '{value}' 不符合安全格式。只允许字母、数字、下划线，且不能以数字开头。"
        )
    return None


def _validate_varlist(value: str, label: str = "varlist") -> str | None:
    """校验 Stata 变量列表字符串，仅阻止注入与危险元字符，不限制合法语法。

    允许因子变量（i.var）、时间序列算子（L.var）、交互项（c.x##i.g）、
    权重子句（[aw=...]）、范围（x1-x10）、通配符（mpg*）等。

    额外拒绝 ``/``、``,`` 与独立的 ``using``：varlist 会被拼进
    ``export excel <varlist> using "<已校验路径>"`` 这类命令，含这些记号即可
    改写命令语义。实测 ``varlist='mpg using /evil/out.xlsx, replace //'``
    能构造出 ``export excel mpg using /evil/out.xlsx, replace // using "<安全路径>"``
    —— ``//`` 把经过 ``_validate_path`` 校验的路径整段注释掉，数据落到攻击者
    指定的位置，路径沙箱被完全绕过。这三种记号在合法 varlist 中都无用途。
    """
    if not value or not value.strip():
        return None
    if any(ch in value for ch in _VARLIST_FORBIDDEN_CHARS):
        return f"错误: {label} 包含非法字符（换行、回车、空字节、分号、!、|、&、反引号或 $）"
    if "/" in value:
        return f"错误: {label} 不能包含 '/'（可构成注释 // 或文件路径，会改写命令语义）"
    if "," in value:
        return f"错误: {label} 不能包含 ','（会被 Stata 解析为选项分隔符）"
    if any(tok.lower() == "using" for tok in value.replace("(", " ").replace(")", " ").split()):
        return f"错误: {label} 不能包含 'using'（会改写命令的目标文件）"
    # 基本的引号成对检查（未转义的双引号需成对）
    quotes = 0
    i = 0
    n = len(value)
    while i < n:
        if value[i] == '"':
            quotes += 1
        i += 1
    if quotes % 2 != 0:
        return f"错误: {label} 包含未闭合的双引号"
    return None


def _validate_filter_expr(value: str, label: str) -> str | None:
    """校验 ``[if]`` / ``[in]`` 子句：拒绝能改写命令目标文件的记号。

    这两个子句与经 ``_validate_path`` 校验的路径拼在同一条命令里，因此和
    ``_validate_varlist`` 面临同一类攻击 —— 只是入口不同。实测
    ``stata_import(filepath="<沙箱内>", condition='1 using "<越界>" //')``
    可拼出 ``import sas if 1 using "<越界>" // using "<沙箱内>", clear``，
    行内 ``//`` 把已校验的路径整段注释掉，数据从攻击者指定的位置读入。

    校验强度必须弱于 ``_validate_varlist``：``in_range`` 天然需要 ``/``
    （``1/100``），``condition`` 天然需要 ``"``（``make == "Honda"``）。故只拒绝
    三类在合法表达式里没有用途的记号，且都只在**字符串之外**判定
    （``strpos(url, "//")`` 是合法的）：

    - 注释起始 ``//`` 与 ``/*``、``*/`` —— 可截断命令余下部分
    - 独立的 ``using`` —— 可引入第二个文件路径
    - 未闭合的双引号 —— 会把后续的 ``using "路径"`` 吞成字符串内容

    本函数是 ``_validate_no_injection`` 的**超集**，两个子句统一走这里，避免
    「只有拼在路径之前的那个工具加了检查」这种按工具打补丁的漂移。
    """
    if err := _validate_no_injection(value, label):
        return err
    if not value or not value.strip():
        return None
    # 取出双引号**之外**的文本；引号内的 // 是数据不是注释
    outside_parts: list[str] = []
    in_string = False
    for ch in value:
        if ch == '"':
            in_string = not in_string
            outside_parts.append(" ")
            continue
        if not in_string:
            outside_parts.append(ch)
    if in_string:
        return f"错误: {label} 包含未闭合的双引号"
    outside = "".join(outside_parts)
    for marker in ("//", "/*", "*/"):
        if marker in outside:
            return (
                f"错误: {label} 不能包含注释记号 '{marker}' —— "
                "它会截断命令余下部分（含已校验的文件路径）"
            )
    if any(tok.lower() == "using" for tok in outside.replace("(", " ").replace(")", " ").split()):
        return f"错误: {label} 不能包含 'using'（会改写命令的目标文件）"
    return None


def _validate_install_source(source: str) -> str | None:
    """校验安装来源：仅允许 ssc 或符合基本格式的 HTTPS URL。"""
    src = source.strip()
    if src.lower() == "ssc":
        return None
    if _INSTALL_SOURCE_RE.match(src):
        return None
    return "错误: source 只允许 'ssc' 或以 https:// 开头的安全 URL"


def _validate_sheet_name(sheet: str) -> str | None:
    """校验 Excel 工作表名。

    工作表名可含空格、中文、括号等（如 "Q1 (2024)"），因命令中以双引号包裹
    sheet("...")，值内的 ) 对 Stata 是安全的。仅拒绝会破坏引号语法或注入
    命令的字符：双引号、换行、分号、空字节。返回错误文本或 None。
    """
    if sheet is None:
        return None
    if any(ch in sheet for ch in ('"', "\n", "\r", "\x00", ";")):
        return "错误: sheet 包含非法字符（双引号、换行、分号）"
    return None


def _validate_cell_reference(cell: str, label: str = "cell") -> str | None:
    """校验 Excel 起始单元格引用（如 ``A1`` / ``B3``）。

    ``cell()`` 被直接放入 ``export excel`` 的括号参数中；仅检查分号/换行
    不能阻止 ``B3) ...`` 关闭括号并改写后续选项。Excel 单元格地址本身不
    需要宏、引号或其他 Stata 表达式，因此使用正向白名单最稳妥。
    """
    if not cell or not cell.strip():
        return None
    if not _CELL_REFERENCE_RE.fullmatch(cell.strip()):
        return f"错误: {label} 必须是 Excel 单元格引用（如 A1、B3）"
    return None


def _validate_delimiter(delimiter: str, label: str = "delimiter") -> str | None:
    """校验单字符分隔符，拒绝会破坏 Stata 字符串/命令的控制字符。"""
    if not delimiter or delimiter == "tab":
        return None
    if len(delimiter) != 1:
        return f'错误: {label} 只能是单个字符或关键字 "tab"（收到 {delimiter!r}）'
    if delimiter in ('"', "`", "$", "\\") or ord(delimiter) < 32:
        return f"错误: {label} 不能是 {delimiter!r}"
    return None


def _validate_scheme_name(scheme: str) -> str | None:
    """校验 Stata 图形方案名（scheme）。

    Stata scheme 名允许字母、数字、下划线、连字符，可数字开头（如 538、
    s2color、s1color-asterisk、economist）。

    改用**正向白名单**而非黑名单：scheme 被拼进 ``set scheme {scheme}``，
    而 ``set scheme`` 支持逗号后的选项（``, permanently``）。黑名单原先漏了
    ``,``，`s2color,permanently` 这类值能穿过校验并改变命令语义 —— 白名单
    从根上排除了「又漏了某个字符」这类问题，也与本函数文档所述的字符集一致。

    返回错误文本或 None。
    """
    if not scheme or not scheme.strip():
        return "错误: scheme 为空"
    if not _SCHEME_NAME_RE.match(scheme.strip()):
        return "错误: scheme 只允许字母、数字、下划线和连字符"
    return None


def _validate_storage_type(vartype: str) -> str | None:
    """校验 generate/egen 的存储类型。返回错误文本或 None。"""
    if not vartype.strip():
        return None
    if not _STORAGE_TYPE_RE.match(vartype.strip()):
        return (
            "错误: vartype 只能是 byte/int/long/float/double/str#/strL"
            f"（收到 {vartype!r}）"
        )
    return None


def _validate_fontface(fontface: str) -> str | None:
    """校验 graph export 的 ``fontface()`` 字体名。

    字体名常含空格（"Times New Roman"），故用双引号包裹后传给 Stata；因此必须
    拒绝能提前闭合的字符：``"`` 结束字符串、``)`` 结束选项、``;`` 起新命令，
    以及宏展开用的 `` ` `` 与 ``$``。

    返回错误文本或 None。
    """
    if not fontface.strip():
        return "错误: fontface 为空"
    if len(fontface) > 128:
        return "错误: fontface 过长（上限 128 字符）"
    bad = [c for c in ('"', ")", "(", ";", "`", "$", "\n", "\r", "\x00") if c in fontface]
    if bad:
        return f"错误: fontface 含非法字符 {bad!r}"
    return None


# =============================================================================
# 危险前缀护栏
# =============================================================================

# 危险：stata_run 中可能导致主机命令执行或 Python 代码执行的显著前缀
# 危险命令前缀。Stata 允许命令从最短官方缩写到全写，之前只有全写导致旁路：
# 真机确认 `sh whoami`（shell）、`era /tmp/x`（erase，**删文件**）、
# `unixcmd ls`、`rmdir /tmp/x` 都能原样穿过旧护栏 —— 缩写形态是 shell-out /
# 文件销毁类命令的真实 bypass。覆盖四族：
#   shell-out：! sh shell xsh xshell winex winexec unixc unixcmd
#   文件/目录销毁：era erase rmd rmdir
#   代码执行：java plugin python: python(
# （python/mata 的裸命令形态另有单独分支，见 _match_dangerous_prefix。）
# 全写放前面，startswith 先命中更完整的形态，错误信息更清晰。
_DANGEROUS_COMMAND_PREFIXES = (
    "!",
    "shell", "sh", "xshell", "xsh", "winexec", "winex", "unixcmd", "unixc", "unix",
    "erase", "era", "rmdir", "rmd",
    "java", "plugin", "python:", "python(",
)

# Stata 通用前缀命令中**不带冒号**的一类，可任意叠加（`capture noisily …`）。
# 官方允许从最短缩写到全写，逐一列出以免正则误伤同名变量/命令。
_BARE_PREFIX_COMMANDS = frozenset(
    {
        "cap", "capt", "captu", "captur", "capture",
        "qui", "quie", "quiet", "quietl", "quietly",
        "noi", "nois", "noisi", "noisil", "noisily",
    }
)

# 带冒号的前缀里，冒号左侧本身就是危险关键字的情形 —— 不能当前缀剥掉，
# 否则 `mata: _stata(…)` 会被剥成 `_stata(…)` 而逃过检查。
_COLON_DANGEROUS_HEADS = frozenset({"mata", "python"})

# 已知的冒号前缀命令（供 _light_strip_prefixes 用）：`by g:` / `bysort g:` /
# `version 17:` / `svy:` / `xi:`。只认这些 —— 不像 _strip_command_prefixes 那样
# 对未知冒号形态一律剥离（那是护栏「多剥更严格」的刻意设计），因为审计需要
# **保留命令身份**：`merge 1:1 price using "f"` 的 `1:1` 不是前缀，被误剥后
# 审计会丢失 `merge` 命令名。
_KNOWN_COLON_PREFIXES = frozenset({"by", "bysort", "version", "svy", "xi"})


def _light_strip_prefixes(line: str) -> str:
    """只剥**已知**前缀（capture/quietly/noisily 及 by/version/svy/xi 冒号形态），
    保留命令身份。供自由文本路径审计识别数据命令用 —— 未知冒号形态（如 merge 1:1
    的匹配规格）不剥，避免把命令名一起吞掉。
    """
    cur = line.strip()
    for _ in range(_MAX_PREFIX_DEPTH):
        head = cur.split(None, 1)
        if not head:
            return cur
        tok = head[0].lower()
        if tok in _BARE_PREFIX_COMMANDS:
            cur = head[1].strip() if len(head) > 1 else ""
            continue
        segments = _split_top_level(cur, ":")
        if len(segments) >= 2:
            lead = segments[0].strip().lower().split()
            if lead and lead[0] in _KNOWN_COLON_PREFIXES:
                cur = ":".join(segments[1:]).strip()
                continue
        return cur
    return cur

# 前缀叠加的扫描上限：真实 Stata 最多几层，设上限纯粹为防御畸形输入死循环。
_MAX_PREFIX_DEPTH = 8


def _split_top_level(text: str, sep: str) -> list[str]:
    """按 ``sep`` 切分，跳过双引号字符串与复合字符串 `` `" … "' `` 内的分隔符。"""
    parts: list[str] = []
    buf: list[str] = []
    in_string = False
    in_compound = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if in_string:
            buf.append(ch)
            if in_compound:
                if ch == '"' and nxt == "'":
                    buf.append(nxt)
                    in_compound = in_string = False
                    i += 2
                    continue
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == "`" and nxt == '"':
            in_string = in_compound = True
            buf.append(ch)
            buf.append(nxt)
            i += 2
            continue
        if ch == '"':
            in_string = True
            buf.append(ch)
            i += 1
            continue
        if ch == sep:
            parts.append("".join(buf))
            buf.clear()
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


def _strip_command_prefixes(line: str) -> str:
    """剥掉 Stata 通用前缀，返回**真正被执行**的命令文本。

    Stata 的前缀命令有两种形态，都不改变被执行命令的语义：

    - 无冒号：``capture`` / ``quietly`` / ``noisily``（含全部官方缩写），可叠加
    - 带冒号：``by g:`` / ``bysort g:`` / ``version 17:`` / ``svy:`` / ``xi:`` …

    二者都能套在 ``shell`` / ``mata:`` / ``!`` 前面而效果不变，因此任何基于
    「行首」的护栏都必须先剥再判。真机验证（Stata 19.5 MP）：
    ``capture shell touch <f>`` 与 ``quietly mata: _stata("shell touch <f>")``
    都真实创建了文件，而修复前的护栏对二者一律放行。

    冒号形态要特别小心：``mata:`` / ``python:`` 自身就以冒号结尾，若无脑取
    冒号右侧，恰好会把最该拦的关键字剥掉。故仅当冒号左侧的首 token 不是危险
    关键字时才剥离。未知的冒号前缀一律按前缀处理 —— 多剥只会让护栏更严格。
    """
    cur = line.strip()
    for _ in range(_MAX_PREFIX_DEPTH):
        head = cur.split(None, 1)
        if not head:
            return cur
        if head[0].lower() in _BARE_PREFIX_COMMANDS:
            cur = head[1].strip() if len(head) > 1 else ""
            continue
        segments = _split_top_level(cur, ":")
        if len(segments) < 2:
            return cur
        lead = segments[0].strip().lower().split()
        if not lead or lead[0] in _COLON_DANGEROUS_HEADS:
            return cur
        cur = ":".join(segments[1:]).strip()
    return cur


def _match_dangerous_prefix(line: str) -> str | None:
    """对单条**已归一化**的命令做行首危险前缀匹配；返回原因或 None。"""
    lowered = line.lower()
    for prefix in _DANGEROUS_COMMAND_PREFIXES:
        if lowered.startswith(prefix):
            # shell/python: 等命令后通常跟随要执行的系统/Py 代码
            target = line[len(prefix) :].strip() or "<empty>"
            return (
                f"错误: 命令 '{line[:60]}' 包含危险前缀 '{prefix}'，"
                f"可能执行主机系统/Py 代码（目标: {target[:40]}）。"
                "如确有必要，请使用操作系统直接操作，而非通过 Stata MCP。"
            )
    # 检测 python 主机命令（部分 Stata 版本使用 python 子命令）
    if lowered == "python" or lowered.startswith("python "):
        return f"错误: 命令 '{line[:60]}' 尝试调用 Stata 内嵌 Python，已被禁止"
    # Mata 与内嵌 Python 同属「可执行任意代码的子语言」，须同等禁止：
    # 块内 _stata("...") 可调用任意 Stata 命令（包括 ! shell out），
    # unlink() / fopen() 可直接读写文件，而本函数是**行首**匹配，
    # 对 mata 块内的代码完全无效 —— 实测 `mata:` + `_stata("display 12345")`
    # 可原样穿过本护栏并成功执行。
    if lowered == "mata" or lowered.startswith("mata ") or lowered.startswith("mata:"):
        return (
            f"错误: 命令 '{line[:60]}' 尝试进入 Mata，已被禁止 —— "
            "Mata 可经 _stata() 执行任意 Stata 命令并直接读写文件。"
            "如确需 Mata 编程，请在 Stata 中直接操作。"
        )
    return None


def _has_dangerous_command_prefix(cmd: str) -> str | None:
    """检查命令是否包含明显的 shell/python 执行入口；返回原因或 None。

    「行首」不等于「命令首」，本函数据此做三重归一化后再匹配：

    1. **分号切分**：``#delimit ;`` 会把命令分隔符从换行改成 ``;``，此后 ``!``
       永远不在行首。真机验证：``#delimit ;`` + ``display 3 ; !touch <f> ;``
       真实创建了文件，而按行匹配的护栏全程放行。字符串内的 ``;`` 不切分。
    2. **前缀剥离**：``capture`` / ``quietly`` / ``by g:`` 等通用前缀套在危险命令
       前不改变其效果（见 ``_strip_command_prefixes``）。
    3. **原文与剥离后各判一次**：剥离只用于发现被藏起来的危险词，原文匹配保证
       ``mata:`` 这类自身即关键字的形态不被剥掉后漏判。

    该检查仍只是最后一层护栏，不能替代操作系统级沙箱：复杂的 Stata 宏/ado
    间接调用仍可能绕过，因此仍需以最小权限运行 MCP Server。
    """
    for raw_line in cmd.split("\n"):
        line = raw_line.strip()
        if not line or line.startswith("*") or line.startswith("//"):
            continue
        for raw_segment in _split_top_level(line, ";"):
            segment = raw_segment.strip()
            if not segment:
                continue
            if reason := _match_dangerous_prefix(segment):
                return reason
            stripped = _strip_command_prefixes(segment)
            if stripped != segment and (reason := _match_dangerous_prefix(stripped)):
                return reason
    return None


# =============================================================================
# 解析器
# =============================================================================


class UnbalancedBlockError(ValueError):
    """命令块未闭合（缺 ``}`` 或 ``end``）。

    单独成类而非复用 ValueError，是为了让调用方能把「用户写错了」与解析器
    自身的内部错误区分开 —— 前者要给可操作提示，后者该记日志。

    ``blocks`` 携带异常发生前已完整解析出的块，``pending`` 携带那个未闭合块
    已累积的文本。安全护栏必须能看到这些内容：危险命令恰好是最容易「未闭合」
    的一类（``mata:`` / ``python:`` 单独出现就会开启 end 块），若因解析失败
    就丢弃已知内容，护栏对最该拦的输入反而失效。
    """

    def __init__(self, message: str, blocks: list[str] | None = None, pending: str = ""):
        super().__init__(message)
        self.blocks = blocks or []
        self.pending = pending


def _flush_block(buffer: list[str], blocks: list[str]) -> None:
    """将 buffer 中非空内容作为一个命令块追加，然后清空 buffer。"""
    block_text = "\n".join(buffer)
    if block_text.strip():
        blocks.append(block_text)
    buffer.clear()


def _opens_end_block(line: str) -> bool:
    """判断该行是否开启一个以单独 ``end`` 结束的多行输入块。

    ``program`` 定义、``input`` 数据录入、``mata`` / ``python`` 子解释器都会让
    Stata 进入等待输入状态，直到读到单独一行 ``end``。若把这类块拆成单行分别
    交给 ``StataSO_Execute``，首行就会使会话挂死，且看门狗的 ``SetBreak`` 无法
    恢复（实测 Stata 19.5 MP）。故须整块收集，交由 ``_materialize_block``
    写入临时 do 文件执行。

    ``program`` 的 ``drop`` / ``dir`` / ``list`` 子命令不进入定义模式，排除。

    判定前须剥掉通用前缀：``quietly program define …`` / ``capture input …``
    都是合法且可用的 Stata（真机验证 Stata 19.5 MP：``quietly program define``
    定义成功、随后调用正常输出），但只看首 token 会让它们一律漏判，开启行被
    单独送执行 —— 正是上面那条挂死路径。
    """
    head = _strip_command_prefixes(line).split()
    if not head:
        return False
    kw = head[0].rstrip(":")
    if kw == "program":
        return len(head) < 2 or head[1] not in ("drop", "dir", "list")
    return kw in ("input", "mata", "python")


def _parse_command_blocks(cmd: str) -> list[str]:
    """将多行 Stata 输入解析为可执行块。

    处理规则：
    - 空行跳过
    - 行首 * 为完整行注释，跳过
    - 行首 // 为完整行注释，跳过
    - 行内 // 为注释（字符串内不生效）
    - /* ... */ 为块注释（字符串内不生效，支持跨行）
    - 行中或行尾 /// 为续行符（其后注释文本会被忽略），与下一行合并
    - { 与 } 用于复合块；字符串、注释内的花括号不计入深度
    - 复合块跨多行收集，深度归零后发出

    Args:
        cmd: 原始多行命令字符串。

    Returns:
        可执行命令块列表，每块作为单一 StataSO_Execute 调用。
    """

    def _scan_line(line: str, in_block_comment: bool):
        """扫描单行，返回 (有效内容, 是否有续行, 花括号深度变化, 是否仍在块注释中, 续行前是否有空格)。

        ``*`` 注释行不会走到这里 —— 主循环已在**逻辑行**层面把它们摘掉（见下方
        ``in_comment_line``）。这一点很重要：``///`` 续行之后的 ``*`` 是乘号而非
        注释（真机 ``display 1 ///`` + ``* 2`` 输出 2），只有逻辑行的**开头**才
        可能是注释。
        """
        content = []
        brace_delta = 0
        in_string = False
        in_compound_string = False
        n = len(line)
        i = 0

        if in_block_comment:
            # 先尝试结束当前跨行块注释
            while i < n:
                if line[i] == "*" and i + 1 < n and line[i + 1] == "/":
                    i += 2
                    in_block_comment = False
                    break
                i += 1
            if in_block_comment:
                # 本行仍未结束块注释，整行跳过
                return "", False, 0, True, False

        while i < n:
            ch = line[i]
            nxt = line[i + 1] if i + 1 < n else ""

            if in_string:
                content.append(ch)
                if in_compound_string:
                    # 复合字符串以 "' 结束（双引号 + 单引号）
                    if ch == '"' and nxt == "'":
                        content.append(nxt)
                        in_compound_string = False
                        in_string = False
                        i += 2
                        continue
                else:
                    if ch == '"':
                        in_string = False
                i += 1
                continue

            # 复合字符串 `" ... "' —— 开启是**反引号 + 双引号**。
            # 曾把开启符写成 "'（那其实是 Stata 的**结束**符），于是普通字符串里
            # 一出现 "' 就翻转状态。实测 `title("'90s")` 这类以撇号开头的字符串
            # （年代、千位记号、所有格）会让行尾的 /// 被当成字符串内容，续行失效，
            # 一条命令被劈成两条各自报错。
            if ch == "`" and nxt == '"':
                in_string = True
                in_compound_string = True
                content.append(ch)
                content.append(nxt)
                i += 2
                continue

            # 普通字符串
            if ch == '"':
                in_string = True
                content.append(ch)
                i += 1
                continue

            # 块注释 /* ... */
            if ch == "/" and nxt == "*":
                i += 2
                while i < n:
                    if line[i] == "*" and i + 1 < n and line[i + 1] == "/":
                        i += 2
                        break
                    i += 1
                else:
                    # 未在本行遇到 */，块注释延续到下一行
                    in_block_comment = True
                continue

            # 行注释或续行符
            if ch == "/" and nxt == "/":
                # 三个斜杠 /// 视为续行符，其后所有内容忽略
                if i + 2 < n and line[i + 2] == "/":
                    space_before = bool(content) and content[-1].isspace()
                    return "".join(content), True, brace_delta, False, space_before
                # 否则 // 注释到行尾
                break

            if ch == "{":
                brace_delta += 1
            elif ch == "}":
                brace_delta -= 1

            content.append(ch)
            i += 1

        return "".join(content), False, brace_delta, in_block_comment, False

    def _comment_continues(line: str) -> bool:
        """``*`` 注释行是否以 ``///`` 续行（其后各行一并并入注释）。

        真机验证：``* 注释 ///`` 之后的行不会执行，续行链可连续吞多行。
        注释内没有字符串语义，直接看行尾即可。
        """
        return line.rstrip().endswith("///")

    blocks = []
    buffer = []
    brace_depth = 0
    in_block_comment = False
    in_continuation = False
    cont_space_before = False
    in_end_block = False
    in_comment_line = False

    for raw_line in cmd.split("\n"):
        line = raw_line.strip("\r")

        # `*` 注释在**逻辑行开头**才成立，且允许缩进（真机验证：顶层与循环体内的
        # 缩进注释都合法）。旧实现只认第 1 列，于是缩进注释被当代码扫描，里面的
        # `{` / `}` 会改变 brace_depth —— 含 `{` 的注释让合法循环抛
        # UnbalancedBlockError，含 `}` 的把块提前切开。
        # 反过来，`///` 续行之后的 `*` 是乘号不是注释，故必须排除 in_continuation。
        if in_comment_line:
            in_comment_line = _comment_continues(line)
            continue
        if (
            not in_block_comment
            and not in_continuation
            and line.lstrip().startswith("*")
        ):
            in_comment_line = _comment_continues(line)
            continue

        if (
            not line.strip()
            and brace_depth == 0
            and not buffer
            and not in_block_comment
            and not in_continuation
        ):
            continue

        was_in_block_comment = in_block_comment
        content, has_cont, delta, in_block_comment, space_before = _scan_line(
            line, in_block_comment
        )
        brace_depth += delta
        if brace_depth < 0:
            brace_depth = 0

        if has_cont:
            if content.strip():
                # 只有当上一行也以 /// 结尾（续行链中间）时，才接到 buffer[-1]；
                # 否则本行是续行链的**第一行**，必须作为新行加入。
                #
                # 原实现无条件并入 buffer[-1]，在块内部会把块的上一行和本行拼成
                # 一行。实测后果：
                #   forvalues i=1/3 {        →  "forvalues i=1/3 {     display `i'"
                #       display ///             即 { 之后同行有代码，r(198)
                #           `i'
                #   program define hi        →  "program define hi     display ..."
                #       display ///             end 被甩到另一个块，配对失效，
                #           "hi"                Stata 进入定义模式**挂死会话**
                #   end
                if in_continuation and buffer:
                    last = buffer[-1]
                    sep = " " if cont_space_before else ""
                    buffer[-1] = last.rstrip() + sep + content.rstrip()
                else:
                    buffer.append(content.rstrip())
                in_continuation = True
                cont_space_before = space_before
            else:
                # /// 行无实质内容（如 /// comment 或行首 ///）时中断当前续行链，
                # 并把已累积的 buffer 作为一个 block 发出，避免后续行被错误拼接。
                in_continuation = False
                if brace_depth == 0 and not in_block_comment and not in_end_block:
                    _flush_block(buffer, blocks)
            continue

        if in_continuation and buffer:
            if content.strip():
                last = buffer[-1]
                sep = " " if cont_space_before else ""
                buffer[-1] = last.rstrip() + sep + content.lstrip()
            in_continuation = False
        elif was_in_block_comment and buffer:
            # `/*` 换行 `*/` 是官方**行连接符**（`///` 出现前的写法），不是两条命令。
            # 旧实现 buffer.append 成新行，于是
            #     regress price weight /*
            #     */ mpg foreign
            # 被劈成 `regress price weight` + `mpg foreign` —— 前半条独立跑出一个
            # **少两个回归元的模型**并「成功」，后半条报 r(199)。真机 e(cmdline) 为
            # `regress price weight  mpg foreign`（两个空格），故此处原样拼接、
            # 不做 strip，才能与 Stata 逐字一致。
            buffer[-1] = buffer[-1] + content
        else:
            buffer.append(content)

        # end 配对块（program / input / mata）：与 { } 同理必须整体执行，
        # 否则首行会让 Stata 进入等待输入状态而挂死。
        #
        # 判定对象必须是 buffer[-1]（续行合并后的完整命令），不能是当前扫描行
        # content。块的**开启行**若带 ///，如
        #     program define mymean ///
        #         , rclass
        # 则 content 只是 ", rclass"，`program` 一词落在上一行；用 content 判定
        # 会漏掉整个块，首行被单独送执行 → Stata 进入定义模式挂死会话。
        # （上一轮修好的是「块**内部**出现 ///」，与此互为镜像。）
        probe = buffer[-1] if buffer else ""
        if not in_end_block and brace_depth == 0 and _opens_end_block(probe):
            in_end_block = True
        elif in_end_block and probe.strip().endswith("end") and content.strip() == "end":
            in_end_block = False

        if brace_depth == 0 and not in_block_comment and not in_end_block:
            _flush_block(buffer, blocks)

    if buffer:
        # 输入结束时仍有未闭合的块 —— 把它送去执行会让 Stata 进入等待输入状态
        # 并**挂死整个会话**（看门狗的 SetBreak 也救不回，实测 `capture noisily {`
        # 单独一行即可复现）。抛错让调用方看到明确原因，比静默挂死好得多。
        if brace_depth > 0 or in_end_block:
            missing = "}" if brace_depth > 0 else "end"
            pending = "\n".join(buffer)
            raise UnbalancedBlockError(
                f"命令块未闭合（缺少 {missing}）：{pending[:80]}\n"
                f"提示：Stata 会等待后续输入直到读到 {missing}，"
                "在 MCP 会话中这会挂死整个连接。请补全后重试。",
                blocks=list(blocks),
                pending=pending,
            )
        _flush_block(buffer, blocks)

    return blocks


def _validate_command_blocks(command: str) -> str | None:
    """对**解析之后**的执行块逐块做危险前缀检查；返回原因或 None。

    必须校验解析器的输出而非它的输入。``_has_dangerous_command_prefix`` 做的是
    逐行**行首**匹配，而 ``_parse_command_blocks`` 在送执行前还会剥掉 ``/* */``
    块注释、按 ``///`` 拼接续行 —— 于是「原始文本的行首」与「真正交给
    ``StataSO_Execute`` 的行首」并不是同一个东西。实测（Stata 19.5 MP）全部绕过：

    ==============================  ==========================
    输入                            解析后真正执行的块
    ==============================  ==========================
    ``/*c*/shell echo hi``          ``shell echo hi``
    ``/**/python: import os``       ``python: import os``
    ``/* a */ mata: 1``             `` mata: 1``
    ``sh/*x*/ell echo hi``          ``shell echo hi``
    ``sh///\\nell echo hi``          ``shell echo hi``
    ==============================  ==========================

    最后两例尤其说明问题：原始文本里根本不存在 ``shell`` 这个词，是解析器把被
    注释/续行符劈开的 token 重新拼了回来，任何基于原始文本的模式匹配都无解。

    这与路径侧「校验路径 == 执行路径」（``_resolve_stata_path_locked``）是同一
    条原则。检查解析后的块还有个附带好处：真正被注释掉的内容不会产生执行块，
    因此注释里出现危险词也不会误伤。
    """
    try:
        blocks = _parse_command_blocks(command)
    except UnbalancedBlockError as e:
        # 块未闭合时仍要检查已解析出的内容。危险命令正是最容易未闭合的一类
        # （`mata:` / `python:` 单独出现即开启 end 块），丢弃已知内容会让护栏
        # 对最该拦的输入失效。未闭合本身由 _precheck_command 单独报错。
        blocks = [*e.blocks, e.pending]
    for block in blocks:
        if reason := _has_dangerous_command_prefix(block):
            return reason
    return None


def _has_delimit_change(command: str) -> bool:
    """检测行首的 ``#delimit``（含官方缩写 ``#d``；字符串内的同名字样不算）。"""
    return any(
        _split_top_level(raw_line, '"')[0].strip().lower()
        .startswith(("#delimit", "#d"))
        for raw_line in command.split("\n")
    )


# 宏混淆：`local c "shell whoami"` → 后续 `` `c' whoami `` 展开成 `shell whoami` 执行。
# 真机确认旧护栏对这类输入放行且 shell 真实执行。第一遍扫描 local/global 字面量
# 定义，标记值为危险命令的宏名；第二遍检查这些宏名是否在**行首**（命令位）被引用。
_MACRO_DEF_RE = re.compile(
    r"^\s*(?:(?:qui(?:etly)?|cap(?:ture)?|noi(?:sily)?)\s+)*"
    r"(local|global)\s+([A-Za-z_]\w*)\s+(\S.*)$",
    re.IGNORECASE | re.MULTILINE,  # 宏定义与命令位引用常跨行
)


def _flag_macro_obfuscation(command: str) -> str | None:
    """检测宏间接调用危险命令的绕过；返回原因或 None。

    只检查**命令位**引用（行首，剥前缀后以 `` `name' `` / ``$name`` 开头）——
    宏的值是危险命令、又真的被拿来当命令用，才是绕过。``display "`c'"`` 这类
    字符串内引用不触发（不构成命令执行）。
    """
    dangerous: dict[str, tuple[str, str]] = {}
    for m in _MACRO_DEF_RE.finditer(command):
        kind, name, value = m.group(1).lower(), m.group(2), m.group(3)
        bare = value.strip()
        if bare.startswith("="):  # `local c = "shell ..."` 等号形式
            bare = bare[1:].strip()
        if bare.startswith('`"') and bare.endswith("'"):  # 复合引号 `" ... "'
            bare = bare[2:-1].strip()
        else:
            bare = bare.strip('"').strip()
        if bare and _match_dangerous_prefix(bare) is not None:
            dangerous[name] = (bare, kind)
    if not dangerous:
        return None
    for raw in command.split("\n"):
        stripped = _strip_command_prefixes(raw).strip()
        for name, (val, kind) in dangerous.items():
            # local → `name'（反引号引用）；global → $name
            if kind == "local" and re.match(rf"`{re.escape(name)}'", stripped):
                return _macro_reject(name, val)
            if kind == "global" and re.match(
                rf"\$\{{{re.escape(name)}\}}|\${re.escape(name)}\b", stripped
            ):
                return _macro_reject(name, val)
    return None


def _macro_reject(name: str, val: str) -> str:
    return (
        f"错误: 命令通过宏间接调用危险命令 —— "
        f"`{name}' 展开为 '{val}'。\n"
        "宏展开后可执行主机系统/删除文件代码，已被禁止。"
    )


def _precheck_command(command: str) -> str | None:
    """自由文本命令的入口预检：危险前缀 + 分隔符变更 + 块闭合性。

    三项检查都必须在**进入执行路径之前**完成：
    - 危险前缀：见 ``_validate_command_blocks``，校验解析后的执行块
    - ``#delimit``：把命令分隔符从换行改成 ``;``，而本模块的解析器是行导向的
    - 块闭合性：未闭合的 ``{`` 或 ``end`` 送去执行会让 Stata 进入等待输入
      状态并挂死会话，看门狗的 SetBreak 也救不回

    顺序有意为之：先报危险前缀。``mata:`` 这类输入同时命中多项，此时「已被禁止」
    比「块未闭合」更贴近用户的真实问题。
    """
    if reason := _validate_command_blocks(command):
        return reason
    if reason := _flag_macro_obfuscation(command):
        return reason
    if _has_delimit_change(command):
        return (
            "错误: 不支持 `#delimit` —— 它把命令分隔符改成 `;`，而本工具按行解析命令。"
            "实测这类脚本会被切成碎块（如 `regress price weight` 与续行 `mpg ;` 变成两条"
            "独立命令，少跑一个回归元却各自「成功」）。"
            "请改用 stata_run_do_file 执行 .do 文件（由 Stata 自行解析，原生支持 #delimit），"
            "或把命令改写成默认的换行分隔形式。"
        )
    try:
        _parse_command_blocks(command)
    except UnbalancedBlockError as e:
        return f"错误: {e}"
    return None


# =============================================================================
# 路径字符串工具（纯函数，不触碰 ALLOWED_ROOTS 缓存等服务器状态）
# =============================================================================


def _expand_win_short_path(path: str) -> str:
    """展开 Windows 8.3 短文件名（如 PROGRA~1 → Program Files）。

    仅在 Windows 平台有效，非 Windows 或失败时原样返回。
    """
    try:
        import ctypes
        from ctypes import wintypes

        buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH + 1)
        ret = ctypes.windll.kernel32.GetLongPathNameW(path, buf, wintypes.MAX_PATH)
        if ret and ret <= wintypes.MAX_PATH:
            return buf.value
    except Exception:
        pass
    return path


def _canonicalize_path(path: str) -> str:
    """将路径规范化：解决相对路径、符号链接、Windows 8.3 短名、长路径前缀。

    返回经过 realpath 解析的绝对路径，统一使用正斜杠。
    对不存在的保存目标也会解析其已有的父级/叶级符号链接，避免 dangling
    symlink 绕过 ``STATA_ALLOWED_ROOTS``。
    """
    # 去除 Windows 长路径前缀
    canonical = path
    if canonical.startswith("\\\\?\\"):
        canonical = canonical[4:]
        if canonical.startswith("UNC\\"):
            canonical = "\\" + canonical[3:]

    # Windows 8.3 短名展开
    canonical = _expand_win_short_path(canonical)

    # 解析相对路径
    normalized = os.path.abspath(canonical)

    # 无条件解析 realpath。realpath 不仅解析已存在的文件，也会解析「叶子
    # 尚不存在、但父目录/最后一级是符号链接」的路径；若只在 exists(real)
    # 时采用解析结果，攻击者可用指向沙箱外的 dangling symlink 绕过白名单。
    try:
        normalized = os.path.realpath(normalized)
    except (OSError, ValueError):
        pass

    # 统一正斜杠
    return normalized.replace("\\", "/")


def _normalize_path(path: str) -> str:
    """将路径转换为 Stata 可接受的格式（正斜杠）。"""
    return os.path.normpath(os.path.abspath(path)).replace("\\", "/")


def _path_has_extension(path: str) -> bool:
    """判断文件名是否已有扩展名（忽略目录名与隐藏文件名）。"""
    return bool(os.path.splitext(os.path.basename(path))[1])


def _append_default_extension(path: str, extension: str) -> str:
    """为无扩展名的文件路径追加 Stata 该命令族的默认扩展名。"""
    if _path_has_extension(path):
        return path
    return f"{path}{extension}"


def _resource_mime(path: str) -> str:
    """按扩展名猜 MIME 类型；猜不到退回 application/octet-stream。"""
    mime, _ = mimetypes.guess_type(path)
    return mime or "application/octet-stream"


def _resource_uri(path: str) -> str:
    """构造 `stata-file://` 资源 URI（百分号编码非路径字符，保留 /）。

    客户端按此 URI 调用 resources/read 即可取回文件二进制内容。
    `quote(..., safe='/')` 让 POSIX 绝对路径（/tmp/x.csv）与 Windows 盘符路径
    （C:/data/x.png）都能直接拼进 URI；空格外编为 %20，读取端由
    `_resource_lookup` 统一 unquote。
    """
    return f"stata-file:///{quote(_normalize_path(path), safe='/')}"


# =============================================================================
# 图形/导出格式助手
# =============================================================================


def _has_unsafe_brace(cmd: str) -> bool:
    """检查 graph command 中是否存在会破坏外层 { } 复合块的右花括号。

    将命令包裹在 { } 中传给 _parse_command_blocks：
    若 cmd 包含未匹配的 }（字符串/注释外），会提前闭合外层 {，
    产生多个 block → 不安全。反之仅产生 1 个 block → 安全。

    cmd 自带未闭合的 ``{`` 时解析器会抛 UnbalancedBlockError（那种输入送去执行
    会挂死会话），同样按不安全处理。
    """
    try:
        blocks = _parse_command_blocks("{\n" + cmd + "\n}")
    except UnbalancedBlockError:
        return True
    return len(blocks) != 1


# graph export 的 width()/height() 语义按格式分三类，「矢量 vs 位图」的二分是错的。
# 依据 [G-2] graph export 的 override_options 表与各 [G-3] *_options 条目，
# 并在 Stata 19.5 MP（macOS）上逐条实测：
#   .png/.tif/.gif/.jpg —— 像素（png_options 等写作 "width of graph in pixels"）。
#     Stata 自己校验 8–16000，越界报 "must be an integer between 8 and 16,000"，
#     诊断比我们能写的更精确，故不在此预校验。
#   .svg —— svg_options 写作 width(#px|#in)，**像素与英寸都支持**，无后缀默认 px
#     （实测 width(800) → 输出头 width="800px"；width(6in) 也接受）。
#     曾被误归入英寸组：合法的 800 被丢弃，而 6 产出 6 像素的废图且导出「成功」。
#   .pdf —— 英寸（pdf_options: "width of graph in inches"），0.5–20。
#   .eps/.ps —— 官方选项表里**没有** width()/height()（ps 只有 pagewidth()，且仅
#     pagesize(custom) 时相关）。实测传任何值都是 "option width() not allowed"
#     → r(198)，错误又被复合块的 capture 吞掉，表现为导出无声失败。
#   .emf/.wmf —— `help emf_options` **不存在**，override_options 表里也没有 emf；
#     wmf 更是根本不在 graph export 的格式表中。两者不接受任何尺寸选项。
_INCH_GRAPH_EXTS = frozenset({".pdf"})
_NO_SIZE_GRAPH_EXTS = frozenset({".eps", ".ps", ".emf", ".wmf"})


def _graph_size_options(export_path: str, width: int, height: int) -> tuple[str, str]:
    """按导出格式生成 graph export 的尺寸选项，并说明被忽略的取值。

    参数被丢弃时一律回报给调用方，避免「悄悄改了参数」。

    Returns:
        (选项串, 说明文本)；无需说明时说明文本为空串。
    """
    ext = os.path.splitext(export_path)[1].lower()
    requested = [(label, v) for label, v in (("width", width), ("height", height)) if v > 0]

    if ext in _NO_SIZE_GRAPH_EXTS:
        if not requested:
            return "", ""
        dropped = ", ".join(f"{label}={v}" for label, v in requested)
        return "", (
            f"提示：{ext} 不支持 width()/height()（Stata 报 option width() not allowed），"
            f"已忽略 {dropped}，改用 Stata 默认尺寸。"
        )

    if ext not in _INCH_GRAPH_EXTS:
        # 位图与 svg：像素，原样下传，越界由 Stata 报错（信息比我们能写的更精确）
        return " ".join(f"{label}({v})" for label, v in requested), ""

    kept = [f"{label}({v})" for label, v in requested if 1 <= v <= 20]
    dropped = [f"{label}={v}" for label, v in requested if not 1 <= v <= 20]
    note = ""
    if dropped:
        note = (
            f"提示：{ext} 的 width()/height() 单位是英寸（0.5–20），"
            f"已忽略像素取值 {', '.join(dropped)}，改用 Stata 默认尺寸。"
        )
    return " ".join(kept), note


# 格式专属的 override_options，依据各 [G-3] *_options 条目并在 19.5 MP 实测：
#   quality()  仅 jpg_options 有（1–100，默认 90）；png/pdf 传了报
#              "option quality() not allowed"。.jpeg 不是官方后缀（实测
#              "translator Graph2jpeg not found"），不能当 jpg 处理。
#   mag()      仅 pdf/eps/ps_options 有（1–10000，默认 100）；png/jpg/svg 报
#              "option mag() not allowed"。
#   fontface() pdf/eps/ps/svg_options 都有；位图格式没有。
# 取值范围一律交给 Stata 校验 —— 它的诊断更精确，也不会随版本漂移。
_QUALITY_EXTS = frozenset({".jpg"})
_MAG_EXTS = frozenset({".pdf", ".eps", ".ps"})
_FONTFACE_EXTS = frozenset({".pdf", ".eps", ".ps", ".svg"})


# 导出命令对「筛选后 0 条观测」报的是 Excel 行数上限（下界是 1，故 0 条也越界），
# 与真实原因毫无关系。实测 auto 数据集：`if foreign == 1 in 1/10` 因前 10 条全为
# 国产车而选中 0 条 → "observations must be between 1 and 1048576"。
_EMPTY_SELECTION_MARKER = "observations must be between 1 and"


def _empty_selection_hint(text: str, condition: str, in_range: str) -> str:
    """把 Stata 那句谈行数上限的错误翻译成「筛选没命中」。

    仅在**确实传了筛选条件**时附加 —— 否则就成了对无关错误的臆测。
    """
    if _EMPTY_SELECTION_MARKER not in text:
        return ""
    if not (condition.strip() or in_range.strip()):
        return ""
    hint = "\n提示：筛选条件未匹配到任何观测（Stata 对空选择报的是行数上限错误）。"
    if condition.strip() and in_range.strip():
        hint += (
            "\n注意 `if` 与 `in` 叠加的语义是「**前 n 条观测里**满足条件的」，"
            "而非「满足条件的前 n 条」；先用 stata_tabulate / stata_summarize 确认规模。"
        )
    return hint


def _filter_clause(condition: str, in_range: str) -> str:
    """拼出 ``[if <cond>] [in <range>]`` 子句（含前导空格；都为空时返回空串）。

    ``[if]`` / ``[in]`` 与选项分属命令的两个语法位置，必须拼在逗号**之前**；
    拼到逗号后面 Stata 会当成未知选项报 r(198)。
    """
    parts = []
    if condition.strip():
        parts.append(f"if {condition.strip()}")
    if in_range.strip():
        parts.append(f"in {in_range.strip()}")
    return (" " + " ".join(parts)) if parts else ""


def _graph_format_options(
    export_path: str, quality: int, mag: int, fontface: str
) -> tuple[str, str]:
    """按导出格式生成 quality()/mag()/fontface()，并说明被忽略的取值。

    不适用的选项必须在此丢弃：传给 Stata 会 r(198)，而复合块的 ``capture`` 会把
    错误吞掉，表现为导出无声失败。

    Returns:
        (选项串, 说明文本)；无需说明时说明文本为空串。
    """
    ext = os.path.splitext(export_path)[1].lower()
    opts, dropped = [], []

    for label, value, allowed, rendered in (
        ("quality", quality, _QUALITY_EXTS, f"quality({quality})"),
        ("mag", mag, _MAG_EXTS, f"mag({mag})"),
        ("fontface", fontface, _FONTFACE_EXTS, f'fontface("{fontface}")'),
    ):
        if not value:
            continue
        if ext in allowed:
            opts.append(rendered)
        else:
            dropped.append(f"{label}={value}")

    note = ""
    if dropped:
        note = (
            f"提示：{ext or '该格式'} 不支持 {', '.join(dropped)}"
            f"（Stata 会报 option ... not allowed），已忽略。"
        )
    return " ".join(opts), note


def _format_size(path: str) -> str:
    """人类可读的文件大小。

    整数 KB 会把 117 字节的回归结果表显示成 "0 KB"，看起来像导出失败，
    故小文件直接用字节。
    """
    try:
        n = os.path.getsize(path)
    except OSError:
        return "大小未知"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"
