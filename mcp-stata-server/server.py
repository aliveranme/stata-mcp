#!/usr/bin/env python3
"""
Stata MCP Server — 通过 pystata 执行 Stata 命令。

使用 Stata 内置的 Python 集成 (pystata) 直接调用 Stata DLL，
支持执行 do 文件、交互式命令、包管理和数据处理。

兼容 StataNow 19 / Stata 18（MP / SE / BE 版本）。

环境变量:
    STATA_HOME: Stata 安装目录（默认 C:\\Program Files\\StataNow\\StataNow19）
    STATA_EDITION: Stata 版本 mp|se|be（默认 mp）
    STATA_ALLOWED_ROOTS: 可选，分号分隔的路径沙箱白名单（例: C:/data;D:/projects）
    STATA_ALLOW_UNC: 可选，设为 1 时允许 UNC 网络路径（默认拒绝）
"""

import atexit
import io
import logging
import os
import re
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

# =============================================================================
# 配置
# =============================================================================

STATA_HOME = os.environ.get("STATA_HOME", r"C:\Program Files\StataNow\StataNow19")
STATA_EDITION = os.environ.get("STATA_EDITION", "mp")

# 日志同时写入 stderr（避免污染 MCP stdio）和日志文件，便于故障排查
_LOG_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"))
os.makedirs(_LOG_DIR, exist_ok=True)
_LOG_FILE = os.path.join(_LOG_DIR, "stata-mcp.log")

_log_formatter = logging.Formatter("[stata-mcp] %(levelname)s: %(message)s")

# 配置 "stata-mcp" logger，避免修改 root logger 产生全局副作用
logger = logging.getLogger("stata-mcp")
logger.setLevel(logging.WARNING)
logger.propagate = False
# 清除可能已存在的处理器，避免重复输出（模块重载场景）；先关闭释放文件句柄
for _h in list(logger.handlers):
    try:
        _h.close()
    except Exception:
        pass
    logger.removeHandler(_h)

_stderr_handler = logging.StreamHandler(sys.stderr)
_stderr_handler.setFormatter(_log_formatter)
logger.addHandler(_stderr_handler)

_file_handler = RotatingFileHandler(
    _LOG_FILE, encoding="utf-8", maxBytes=5 * 1024 * 1024, backupCount=3
)
_file_handler.setFormatter(_log_formatter)
logger.addHandler(_file_handler)

# =============================================================================
# Stata 初始化
# =============================================================================

STATA_UTILITIES = os.path.join(STATA_HOME, "utilities")
if not os.path.isdir(STATA_UTILITIES):
    logger.error("Stata utilities not found at %s", STATA_UTILITIES)
    print(f"FATAL: Stata utilities directory not found: {STATA_UTILITIES}", file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, STATA_UTILITIES)
os.environ["SYSDIR_STATA"] = STATA_HOME

try:
    from pystata import config
except ImportError as e:
    logger.error("Failed to import pystata: %s", e)
    print(f"FATAL: Cannot import pystata from {STATA_UTILITIES}", file=sys.stderr)
    sys.exit(1)

try:
    config.init(STATA_EDITION, splash=False)
    # 关键: 关闭流式输出，避免额外线程干扰 MCP stdio
    config.stconfig["streamout"] = "off"
    logger.info(
        "Stata %s %s initialized at %s (streamout=off)",
        config.stversion,
        config.stedition,
        config.sthome,
    )
except SystemError as e:
    logger.error("Stata initialization failed: %s", e)
    print(f"FATAL: Failed to initialize Stata: {e}", file=sys.stderr)
    sys.exit(1)

# 二者都必须在 config.init() 之后导入（check_initialized 检查）。
# sfi 由 Stata 随 utilities 提供，用于申请 Stata 托管的临时文件（会话结束自动
# 清理），多行命令块经临时 do 文件执行时需要，见 _materialize_block。
import sfi  # noqa: E402
from pystata.core import stout  # noqa: E402

# headless 环境下主动关闭图形窗口创建，避免第三方/复杂图形挂起
try:
    with stout.RedirectOutput(stout.StataDisplay(), stout.StataError(), stecho=False):
        encoded = config.get_encode_str("set graphics off")
        config.stlib.StataSO_Execute(encoded, False)
except Exception as e:
    logger.warning("Could not set graphics off during init: %s", e)


# =============================================================================
# MCP Server
# =============================================================================

from fastmcp import FastMCP  # noqa: E402
from fastmcp.tools.base import ToolResult  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

mcp = FastMCP(
    name="StataNow 19",
    instructions=(
        "执行 Stata 命令、管理数据处理工作流、安装扩展包。"
        "可执行 do 文件、交互式命令、安装和管理 Stata 扩展包、"
        "读取 .dta 数据文件等。"
    ),
)

_stata_lock = threading.Lock()
_ping_lock = threading.Lock()  # 保护 _last_ping_time 的读写

# MCP 工具结果上限（Claude Code 默认为 25K tokens ≈ 150K 字符）
MAX_OUTPUT_CHARS = 120_000
# 分页阈值：超过此大小自动分页
PAGE_SIZE = 4_000
# Stata 返回码 3000 = "无错误但无实质输出"（如 r-class 命令）
STATA_RC_NO_OUTPUT = 3000
# 自定义返回码：StataSO_Execute 崩溃后已自动恢复，命令本身未执行，需重试。
# 区别于 999（崩溃未恢复）与 998（DLL 无响应）。视为非致命，不标记 MCP isError。
STATA_RC_RECOVERED = 997
# 命令输入最大长度
MAX_COMMAND_LENGTH = 65_536
# 最近一次完整输出的缓存（支持翻页）
_last_output = ""
# 保护 _last_output 读写的独立锁，避免翻页与命令执行串行化
_output_lock = threading.Lock()
# Ping 缓存：避免高频命令的重复心跳开销
_last_ping_time = 0.0
PING_CACHE_SECONDS = 2.0  # 2 秒内跳过重复 ping

# 输出被上限裁剪时追加的说明。带上可操作建议 —— 单纯说「已截断」会让调用方
# 反复翻页去找不存在的后半段。
_TRUNCATION_NOTICE = (
    f"\n(输出已截断：超过 {MAX_OUTPUT_CHARS} 字符上限，后续内容已丢弃。"
    "请缩小范围后重试，例如用 in_range 限定观测、改用 summarize/tabulate 汇总，"
    "或先导出到文件再查看。)"
)

# estout 依赖探测命令。提为常量以便测试断言命令串本身。
# 不能加 capture —— 那会吞掉错误使 rc 恒为 0，见 stata_export_excel 中的说明。
_ESTOUT_PROBE_CMD = "which estout"

# Stata 返回码中文释义
STATA_RC_MESSAGES = {
    0: "成功",
    1: "未指定的错误",
    2: "无效的命令或选项",
    3: "未找到指定的文件",
    4: "内存不足",
    5: "变量不存在",
    6: "系统错误",
    7: "操作被中断",
    8: "无效的语法表达式",
    9: "变量类型不匹配",
    10: "数据集中无观测值",
    20: "矩阵尺寸不匹配",
    99: "观测值不足",
    110: "变量已存在（用 replace 覆盖或改用新名）",
    111: "变量或命令未找到",
    198: "命令语法错误",
    199: "选项语法错误",
    3000: "命令执行成功，无文本输出",
    997: "Stata 崩溃后已自动恢复（命令未执行，请重试）",
    999: "Stata DLL 内部崩溃",
    998: "Stata DLL 无响应",
}

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
# 帮助主题：命令名 + 可选的多词子主题（如 "xtreg postestimation"、"estat firststage"）。
# 仅允许字母/数字/下划线/空格 —— 命令名与手册主题的全部合法字符都在此集内，
# 而 ! ; 换行 反引号 $ 引号 ( ) 等注入字符一律被拒，杜绝把第二条命令拼进 help。
_HELP_TOPIC_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_ ]{0,63}$")

# =============================================================================
# 路径沙箱 (ALLOWED_ROOTS)
# =============================================================================
# STATA_ALLOWED_ROOTS: 可选，分号分隔的允许根目录列表。
#   例: "C:/data;D:/projects/stata"
#   设置后所有文件路径（含相对路径解析后）必须落在某根之下。
#   未设置时保持向后兼容（不限制绝对路径）。
# STATA_ALLOW_UNC: 可选，设为 "1" 后允许 UNC 网络路径（默认拒绝，仅对沙箱模式生效）。

_STATA_ALLOWED_ROOTS_ENV = os.environ.get("STATA_ALLOWED_ROOTS", "")
_STATA_ALLOW_UNC = os.environ.get("STATA_ALLOW_UNC", "") == "1"

# 字典序排列的允许根目录（realpath 解析后），缓存避免每次重新解析
_ALLOWED_ROOTS_CACHE: tuple[str, ...] | None = None


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
    对不存在的路径（如 save 新文件），回退为 normpath + abspath。
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

    # 尝试 realpath 解析符号链接；若文件不存在则回退
    try:
        real = os.path.realpath(normalized)
        if os.path.exists(real):
            normalized = real
    except (OSError, ValueError):
        pass

    # 统一正斜杠
    return normalized.replace("\\", "/")


def _init_allowed_roots() -> tuple[str, ...]:
    """从环境变量解析允许的根目录列表（懒加载）。"""
    global _ALLOWED_ROOTS_CACHE
    if _ALLOWED_ROOTS_CACHE is not None:
        return _ALLOWED_ROOTS_CACHE
    raw = os.environ.get("STATA_ALLOWED_ROOTS", "")
    if not raw.strip():
        _ALLOWED_ROOTS_CACHE = ()
        return _ALLOWED_ROOTS_CACHE
    roots: list[str] = []
    for part in raw.split(";"):
        part = part.strip().strip("\"'").strip()
        if not part:
            continue
        canonical = _canonicalize_path(part)
        # 确保根目录以 / 结尾，便于前缀匹配
        if not canonical.endswith("/"):
            canonical += "/"
        roots.append(canonical)
    roots.sort(key=len, reverse=True)  # 长前缀优先匹配
    _ALLOWED_ROOTS_CACHE = tuple(roots)
    return _ALLOWED_ROOTS_CACHE


def _is_path_allowed(path: str) -> bool:
    """检查规范化后的绝对路径是否在允许的根目录下。

    若 ALLOWED_ROOTS 未配置则默认允许（向后兼容）；
    若已配置则路径必须落在任一允许根下。
    """
    roots = _init_allowed_roots()
    if not roots:
        return True  # 未配置沙箱
    canonical = _canonicalize_path(path)
    # 确保路径以 / 结尾，避免前缀误匹配（如 /data 匹配 /data2）
    check = canonical if canonical.endswith("/") else canonical + "/"
    return any(check.startswith(root) for root in roots)


# 危险字符：换行、回车、空字节、分号（可能分割命令）
_INJECTABLE_CHARS = {"\n", "\r", "\x00", ";"}
# varlist 中额外需要注意的 shell/Stata 元字符
_VARLIST_FORBIDDEN_CHARS = {"\n", "\r", "\x00", ";", "!", "|", "&", "`", "$"}
# 危险：stata_run 中可能导致主机命令执行或 Python 代码执行的显著前缀
_DANGEROUS_COMMAND_PREFIXES = ("!", "shell", "winexec", "python:", "python(")


def _has_dangerous_command_prefix(cmd: str) -> str | None:
    """检查命令是否包含明显的 shell/python 执行入口；返回原因或 None。

    该检查仅作为最后一层护栏，不能替代操作系统级沙箱。它阻止最常见的
    '!' shell out 和 'python:' 代码注入；复杂的 Stata 宏/ado 绕行仍可能
    绕过，因此仍需保持最小权限运行 MCP Server。
    """
    for raw_line in cmd.split("\n"):
        line = raw_line.strip()
        if not line or line.startswith("*") or line.startswith("//"):
            continue
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
        # unlink() / fopen() 可直接读写文件，而本函数是逐行**行首**匹配，
        # 对 mata 块内的代码完全无效 —— 实测 `mata:` + `_stata("display 12345")`
        # 可原样穿过本护栏并成功执行。
        if lowered == "mata" or lowered.startswith("mata ") or lowered.startswith("mata:"):
            return (
                f"错误: 命令 '{line[:60]}' 尝试进入 Mata，已被禁止 —— "
                "Mata 可经 _stata() 执行任意 Stata 命令并直接读写文件。"
                "如确需 Mata 编程，请在 Stata 中直接操作。"
            )
    return None


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


def _precheck_command(command: str) -> str | None:
    """自由文本命令的入口预检：危险前缀 + 块闭合性。返回错误文本或 None。

    两项检查都必须在**进入执行路径之前**完成：
    - 危险前缀：见 ``_validate_command_blocks``，校验解析后的执行块
    - 块闭合性：未闭合的 ``{`` 或 ``end`` 送去执行会让 Stata 进入等待输入
      状态并挂死会话，看门狗的 SetBreak 也救不回

    顺序有意为之：先报危险前缀。``mata:`` 这类输入同时命中两项，此时「已被禁止」
    比「块未闭合」更贴近用户的真实问题。
    """
    if reason := _validate_command_blocks(command):
        return reason
    try:
        _parse_command_blocks(command)
    except UnbalancedBlockError as e:
        return f"错误: {e}"
    return None


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


def _check_abs_path_safety(abs_path: str) -> str | None:
    """对一个已规范化的绝对路径做权威安全校验。

    校验 UNC 与 ALLOWED_ROOTS 沙箱。绝对路径已词法折叠（无残留 ..），
    因此沙箱前缀匹配天然防止越界 —— 落在沙箱外的路径会因前缀不匹配被拒。
    返回错误文本或 None。

    该函数不依赖任何工作目录，是路径校验的权威层。
    """
    # 拒绝 UNC 路径（除非 STATA_ALLOW_UNC=1）。兼顾 \\ 与 // 两种形式。
    norm = abs_path.replace("/", "\\")
    if norm.startswith("\\\\") and not _STATA_ALLOW_UNC:
        return "错误: 不允许 UNC 网络路径"
    # ALLOWED_ROOTS 沙箱检查
    if not _is_path_allowed(abs_path):
        roots = _init_allowed_roots()
        return (
            f"错误: 路径 '{abs_path}' 不在允许目录下。"
            f"已配置的允许根目录: {'; '.join(roots)}。"
            "如确需访问此路径，请更新 STATA_ALLOWED_ROOTS 环境变量。"
        )
    return None


def _check_path_chars(path: str) -> str | None:
    """路径字符级预检：拒绝空路径与非法控制/分隔字符。

    不依赖任何工作目录，不解析相对路径，是 _validate_path（Python-cwd 预检）
    与 _resolve_stata_path_locked（Stata-cwd 权威校验）共用的第一道字符关口。
    """
    if not path or not path.strip():
        return "错误: 路径为空"
    if "\x00" in path or '"' in path or ";" in path or "\n" in path or "\r" in path:
        return "错误: 路径包含非法字符"
    return None


def _validate_path(path: str) -> str | None:
    """校验路径安全性：拒绝空字节、双引号、分号、换行以及越界路径穿越。

    这是工具入口的快速预检（在进入 _stata_lock 之前），基于 Python 进程
    工作目录解析相对路径。权威校验在 _resolve_stata_path_locked 中基于
    Stata 实际工作目录进行 —— 二者可能不同，故此处仅作早期拦截。

    当 STATA_ALLOWED_ROOTS 环境变量配置时，所有路径必须落在允许根目录下。
    """
    if err := _check_path_chars(path):
        return err
    normalized = os.path.normpath(os.path.abspath(path))
    # 拒绝 UNC 路径（除非 STATA_ALLOW_UNC=1）
    if normalized.startswith("\\\\") and not _STATA_ALLOW_UNC:
        return "错误: 不允许 UNC 网络路径"
    # 相对路径限制在当前工作目录内，防止 .. 越界（快速预检）
    if not os.path.isabs(path):
        try:
            rel = os.path.relpath(normalized, os.getcwd())
            if rel.startswith(".."):
                return "错误: 相对路径不能超出当前工作目录"
        except ValueError:
            return "错误: 路径无效"
    # ALLOWED_ROOTS 沙箱检查（基于 Python cwd 解析，权威校验在锁内补充）
    if err := _check_abs_path_safety(normalized.replace("\\", "/")):
        return err
    return None


def _paginate(text: str, page: int, page_size: int = PAGE_SIZE) -> str:
    """将文本分页，返回指定页及导航信息。

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
    footer = f"\n── 第 {page}/{total_pages} 页"
    if page < total_pages:
        footer += f" — 使用 stata_more(page={page + 1}) 翻下页"
    if page > 1:
        footer += f" — stata_more(page={page - 1}) 翻上页"
    footer += " — stata_more(page=0) 显示全部"

    return header + chunk + footer


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
    """
    head = line.strip().split()
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
        """扫描单行，返回 (有效内容, 是否有续行, 花括号深度变化, 是否仍在块注释中, 续行前是否有空格)。"""
        # Stata 的 * 注释必须位于第 1 列（不允许前导空白）
        if not in_block_comment and line.startswith("*"):
            # 完整行注释；花括号均不计算
            return "", False, 0, False, False

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

    blocks = []
    buffer = []
    brace_depth = 0
    in_block_comment = False
    in_continuation = False
    cont_space_before = False
    in_end_block = False

    for raw_line in cmd.split("\n"):
        line = raw_line.strip("\r")
        if (
            not line.strip()
            and brace_depth == 0
            and not buffer
            and not in_block_comment
            and not in_continuation
        ):
            continue

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


def _drain_output(min_wait: float = 0.1, quiet_gap: float = 0.02) -> str:
    """排空输出缓冲，返回残留内容。

    使用指数退避轮询：初始 1ms，最大 20ms；在检测到输出后重置回 1ms。
    安静窗口用于确认输出已结束，避免固定高频轮询浪费 CPU。

    优化：若轮询期间从未见过任何输出（缓冲本就为空），3ms 后即快速退出，
    无需等满 min_wait/quiet_gap。常见的小输出场景下省去 ~12ms 空转。
    有残留时行为不变（仍走 quiet_gap 确认输出已结束）。

    参数可调：min_wait=最小等待秒，quiet_gap=安静判定秒。

    用于执行前清理和 SetBreak 后的错误恢复。
    """
    parts = io.StringIO()
    t_start = time.time()
    last_nonempty = time.time()
    seen_output = False
    sleep_ms = 1

    while time.time() - t_start < min_wait:
        out = config.get_output()
        if out:
            parts.write(out)
            seen_output = True
            last_nonempty = time.time()
            sleep_ms = 1
        if seen_output:
            if time.time() - last_nonempty > quiet_gap:
                break
        elif time.time() - t_start > 0.003:
            # 从未见过输出：缓冲本就为空，无需继续等待
            break
        time.sleep(sleep_ms / 1000.0)
        sleep_ms = min(sleep_ms * 2, 20)

    return parts.getvalue()


def _set_break() -> None:
    """安全调用 Stata 中断，用于超时恢复。"""
    try:
        sb = config.stlib.StataSO_SetBreak
        if sb:
            sb()
    except Exception as e:
        logger.warning("StataSO_SetBreak failed: %s", e)


def _ping_stata() -> bool:
    """快速心跳：检测 Stata DLL 是否存活。

    通过 _execute_single 执行无害命令 "display 42"，自带超时看门狗。
    成功时更新 _last_ping_time 缓存。
    若失败则尝试排空缓冲 + SetBreak 恢复一次。

    Returns:
        True = 存活, False = 无响应。
    """
    global _last_ping_time

    for attempt in range(2):  # 首次 + 一次恢复重试
        try:
            rc, out = _execute_single("display 42", timeout=10)
            if rc in (0, STATA_RC_NO_OUTPUT) and "42" in out:
                with _ping_lock:
                    _last_ping_time = time.time()
                return True
        except Exception:
            pass

        if attempt == 0:
            logger.warning("Stata ping failed, attempting recovery...")
            _drain_output()
            _set_break()
            time.sleep(0.1)

    with _ping_lock:
        _last_ping_time = 0.0
    return False


def _execute_safe(cmd: str, timeout: int = 60) -> tuple:
    """安全执行 Stata 命令，包含预检 + 崩溃恢复。

    **预检优化**：2 秒内对同一会话的连续调用跳过重复 ping。
    流程：
      1. ping 预检（缓存有效则跳过）
      2. 若存活 → _execute_single() 正常执行
      3. 若 _execute_single 返回崩溃码(999) → 尝试恢复
      4. 若 ping 失败 → 直接返回错误信息

    Args:
        cmd: Stata 命令字符串。
        timeout: 超时秒数（默认 60）。

    Returns:
        (return_code, output_text)
    """
    # --- 预检（带缓存）---
    now = time.time()
    with _ping_lock:
        ping_age = now - _last_ping_time
        ping_expired = ping_age >= PING_CACHE_SECONDS
    if ping_expired:
        if not _ping_stata():
            logger.error("Stata 无响应，无法执行命令: %s", cmd[:80])
            return 998, (
                "[错误] Stata DLL 无响应。这可能由以下原因导致：\n"
                "  1. 上一个命令导致 Stata DLL 崩溃\n"
                "  2. headless 环境中图形操作过载\n"
                "  3. Stata 会话已损坏\n\n"
                "建议: 重启 MCP Server（退出并重新打开 Claude Code）\n"
            )
    else:
        logger.debug("Skipped ping (cached %.1fs ago)", ping_age)

    # --- 执行 ---
    rc, out = _execute_single(cmd, timeout)

    # --- 崩溃检测与恢复 ---
    if rc == 999:
        logger.warning("StataSO_Execute 崩溃, 尝试恢复会话...")
        try:
            _drain_output()
            _set_break()
            time.sleep(0.2)

            # 再次 ping 确认恢复
            if _ping_stata():
                # 恢复成功：Stata 存活但本命令未执行，用 997 标记「需重试」而非 999。
                # 这样 _run_stata_command 不会将其误报为致命的「内部崩溃」错误。
                rc = STATA_RC_RECOVERED
                out += "\n(Stata 已自动恢复，请重试命令)"
            else:
                rc = 998
                out += "\n(Stata 崩溃且无法自动恢复，需要重启 MCP Server)"
        except Exception as e:
            logger.exception("Stata 崩溃恢复失败: %s", e)
            rc = 998
            out += "\n(Stata 崩溃且无法自动恢复，需要重启 MCP Server)"

    return rc, out


def _describe_empty_result() -> str:
    """命令毫无输出时，给出可操作的解释而非笼统的「成功」。

    内存中没有数据集时，Stata 执行 ``summarize`` / ``list`` / ``tabulate``
    既不报错也不输出，笼统回一句「执行成功，无文本输出」会让调用方去排查命令
    本身，而真实原因只是还没载入数据。这里用 ``c(N)`` 把这种情况单独讲清楚。

    仅在确无输出时才触发（约 12ms），不影响正常路径。
    调用者必须已持有 ``_stata_lock``。
    """
    try:
        rc, out = _execute_single("display c(N)", timeout=10)
    except Exception:  # noqa: BLE001 - 探测失败不应影响主命令的结果
        logger.exception("空输出原因探测失败")
        return "(命令执行成功，无文本输出)"
    if rc in (0, STATA_RC_NO_OUTPUT) and out.strip() == "0":
        return (
            "(无输出：当前内存中没有数据集。"
            '请先用 stata_use_dataset("路径.dta") 载入数据，'
            '或 stata_run("sysuse auto, clear") 载入示例数据。)'
        )
    return "(命令执行成功，无文本输出)"


def _materialize_block(cmd: str) -> str:
    """把多行命令块落到 Stata 临时 do 文件，返回实际要执行的单条命令。

    ``StataSO_Execute`` 是**单条命令**接口，不是脚本接口：换行不被视为命令
    分隔符，而是被当作同一条命令的续写。实测（Stata 19.5 MP）后果分三级：

    - ``display 1\\ndisplay 2`` → 只执行第一条，第二条成为参数 → r(198)
    - ``capture noisily {...}`` → "code follows on the same line as open brace"
    - ``if _N > 0 {...}`` / ``program define ... end`` → Stata 进入等待输入
      状态，**整个会话挂死，看门狗的 SetBreak 也无法恢复**

    因此多行块必须整体交给 Stata 解析。官方 ``pystata.stata.run`` 对多行输入
    采用同样策略：写入临时 do 文件后 ``include`` 执行。用 ``include`` 而非
    ``do``，是为了让块内定义的局部宏对后续命令可见 —— 语义上贴近「在命令行
    逐条敲」，符合 MCP 会话的预期。

    单行命令原样返回，继续走 ``StataSO_Execute`` 快路径：实测单行约 12ms，
    include 约 257ms，20 倍差距，不能一律走临时文件。

    Args:
        cmd: 单个命令块（可能多行）。

    Returns:
        ``(要执行的单条命令, 执行后需删除的临时文件路径或 None)``。

    Raises:
        OSError: 临时文件创建或写入失败。
    """
    if "\n" not in cmd.strip():
        return cmd, None
    tmpf = sfi.SFIToolkit.getTempFile()
    with open(tmpf, "w", encoding="utf-8") as f:
        f.write(cmd if cmd.endswith("\n") else cmd + "\n")
    return f'include "{tmpf}"', tmpf


def _cleanup_temp_block(path: str | None) -> None:
    """删除 ``_materialize_block`` 写出的临时 do 文件。

    sfi 的临时文件本会在 Stata 会话结束时统一清理，但 MCP server 是长驻进程：
    一个活跃分析会话每执行一个多行块就留下一个文件（实测 50 个块 → 50 个文件），
    进程崩溃重启时还会残留。命令执行完文件已被 ``include`` 读完，立即删除即可，
    残留数恒为 0。
    """
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        logger.debug("临时 do 文件已不存在或无法删除: %s", path)


def _execute_single(cmd: str, timeout: int = 60) -> tuple:
    """执行单条 Stata 命令，返回 (return_code, output_text)。

    使用 RedirectOutput 防止 Stata 输出泄漏到 MCP stdio 通道。
    内置超时保护：命令执行超过 timeout 秒时调用 StataSO_SetBreak 中断。

    **输出收集优化**：自适应轮询 + 智能清尾。
    - 首次 300ms 快轮询：1ms 间隔，连续 3 次空转退出
    - 然后 50ms 短 drain：仅在小输出时执行
    - 仅在输出量 ≥ 10K 时执行完整 drain（100ms）

    Args:
        cmd: Stata 命令字符串。
        timeout: 超时秒数（默认 60）。

    Returns:
        (return_code, output_text)
    """
    # 多行块无法直接喂给单命令接口，先落到临时 do 文件（详见 _materialize_block）
    try:
        exec_cmd, tmp_block = _materialize_block(cmd)
    except OSError as e:
        logger.exception("无法为多行命令块创建临时 do 文件: %s", cmd[:80])
        return 1, f"错误: 无法创建临时 do 文件执行多行命令块: {e}"

    # 执行前排空残留缓冲（最短 drain）
    _drain_output(min_wait=0.05, quiet_gap=0.01)

    # 超时看门狗（防止 StataSO_Execute 挂起导致 MCP 通信阻塞）
    exec_done = threading.Event()
    did_break = False

    def _timeout_watchdog():
        nonlocal did_break
        if not exec_done.wait(timeout=timeout):
            # 二次检查，避免命令恰好在超时临界点完成时误发 break
            if exec_done.is_set():
                return
            logger.warning("Stata command timed out (>%ss), issuing break: %s", timeout, cmd[:80])
            _set_break()
            did_break = True

    watch = threading.Thread(target=_timeout_watchdog, daemon=True)
    watch.start()

    try:
        with stout.RedirectOutput(stout.StataDisplay(), stout.StataError(), stecho=False):
            encoded = config.get_encode_str(exec_cmd)
            rc = config.stlib.StataSO_Execute(encoded, False)
    except Exception as e:
        logger.exception("StataSO_Execute crashed on: %s", cmd[:80])
        exec_done.set()
        return 999, f"StataSO_Execute 崩溃: {e}"
    finally:
        # include 已把文件读完，此处删除；放 finally 保证崩溃路径也不残留。
        _cleanup_temp_block(tmp_block)

    exec_done.set()

    # 仅在看门狗触发 break 后排空错误输出
    if did_break:
        time.sleep(0.1)
        _drain_output(min_wait=0.05)

    # --- 自适应输出收集 ---
    out_buf = io.StringIO()
    total_len = 0
    empty_count = 0
    clean_exit = False  # 阶段 1 是否以「连续 3 次空转」正常退出

    # 阶段 1: 快轮询（最多 300 次，连续 3 次空转退出，指数退避）
    # 优化：取到输出时立即复取（不 sleep），仅空转时退避等待。
    sleep_ms = 1
    attempts = 0
    while attempts < 300:
        out = config.get_output()
        attempts += 1
        if out:
            # 必须在写入时按剩余空间裁剪：Stata 可能一次性吐出远超上限的文本
            # （实测 19980 obs 的 list 单次返回 1,270,888 字符）。先整块写入再
            # 判断总长，只能停止继续收集，拦不住已经进入缓冲的部分，上限形同虚设。
            room = MAX_OUTPUT_CHARS - total_len
            if len(out) >= room:
                out_buf.write(out[:room])
                out_buf.write(_TRUNCATION_NOTICE)
                total_len = MAX_OUTPUT_CHARS
                break
            out_buf.write(out)
            total_len += len(out)
            empty_count = 0
            sleep_ms = 1
            continue  # 立即复取，不 sleep
        empty_count += 1
        if empty_count >= 3:
            clean_exit = True
            break
        time.sleep(sleep_ms / 1000.0)
        sleep_ms = min(sleep_ms * 2, 20)

    # 阶段 2: 智能清尾
    # - clean_exit 的小输出：阶段 1 已确认输出结束，仅做超短 drain（5ms）兜底
    #   延迟二次输出，省去原 50ms 空转。
    # - 未干净退出或大输出：保留原 drain 策略确保完整收集。
    if total_len < MAX_OUTPUT_CHARS:
        if clean_exit and total_len < 10_000:
            tail = _drain_output(min_wait=0.005, quiet_gap=0.002)
        elif total_len < 10_000:
            tail = _drain_output(min_wait=0.05, quiet_gap=0.01)
        else:
            tail = _drain_output(min_wait=0.1, quiet_gap=0.015)
        if tail:
            room = MAX_OUTPUT_CHARS - total_len
            if len(tail) >= room:
                out_buf.write(tail[:room])
                out_buf.write(_TRUNCATION_NOTICE)
                total_len = MAX_OUTPUT_CHARS
            else:
                out_buf.write(tail)
                total_len += len(tail)

    # 看门狗中断后 Stata 返回的是通用错误码（实测 rc=1「未指定的错误」），
    # 单看返回码会让调用方去排查命令语法，故显式点明超时与可行的下一步。
    if did_break:
        out_buf.write(
            f"\n(命令执行超过 {timeout}s 上限已被中断。"
            "如属正常的长耗时任务，请显式传入更大的 timeout；"
            "若命令可能在 headless 环境挂起，请改用更轻量的写法。)"
        )

    return rc, out_buf.getvalue()


def _format_error(rc: int, block: str, out: str) -> str:
    """格式化 Stata 错误信息，包含返回码释义。"""
    msg = STATA_RC_MESSAGES.get(rc, f"未知返回码({rc})")
    prefix = f"[返回码: {rc}] {msg}"
    snippet = block[:60]
    if out.strip():
        return f"{prefix} — {snippet}\n{out.strip()}"
    return f"{prefix} — {snippet}"


def _make_error_result(message: str, rc: int | None = None) -> ToolResult:
    """将错误文本包装为 MCP ToolResult(is_error=True)。

    FastMCP 默认将工具返回的 str 序列化为 isError=false 的成功结果。
    本函数显式标记错误状态，使 MCP 客户端能程序化区分错误与正常输出。

    Args:
        message: 错误文本（保持与 error 相同的中文文案）。
        rc: 可选的 Stata 返回码，附在 meta 中供调用方参考。

    Returns:
        ToolResult，FastMCP 会将其序列化为 CallToolResult(isError=true)。
    """
    return ToolResult(content=message, is_error=True)


# 用于识别错误文本的通用前缀列表（由 _validate_* 及 _format_error 系列产生）
_ERROR_PREFIXES = ("错误：", "错误:", "[错误]", "[返回码:", "(无有效命令)")


def _result_or_error(value: str | ToolResult) -> str | ToolResult:
    """将已知错误前缀字符串统一包装为 ToolResult(is_error=True)。

    允许工具函数保留现有 return err_str 风格，仅在最后边界处
    自动检测错误前缀并提升 MCP 错误语义。不会修改纯成功文本。
    """
    if isinstance(value, ToolResult):
        return value
    if isinstance(value, str) and any(value.startswith(p) for p in _ERROR_PREFIXES):
        return _make_error_result(value)
    return value


def _run_stata_command(
    cmd: str,
    page: int = 1,
    timeout: int = 60,
    require_file: str | None = None,
) -> str | ToolResult:
    """执行 Stata 命令，支持分页浏览。

    多行命令按 \\n 拆分后逐条执行。
    支援 `///` 续行符和 `{ }` 复合块（自动合并为单次 StataSO_Execute 调用）。
    当输出超过 PAGE_SIZE 时自动缓存完整输出并返回首页。
    所有执行经过 _execute_safe（预检 + 超时 + 崩溃恢复）。

    Args:
        cmd: Stata 命令字符串（多命令用 \\n 分隔）。
        page: 页码（1-based），0 = 全部，仅对单命令有效。
        timeout: 每条命令的超时秒数（默认 60）。
        require_file: 若提供，在获取锁后用 Stata 实际工作目录解析为绝对路径，
            经沙箱权威校验后检查文件是否存在；不存在或越界沙箱则直接返回错误，
            不会访问 Stata DLL 执行 cmd。命令中嵌入的 Python-cwd 路径会被
            替换为该 Stata 绝对路径，确保「校验路径 == 执行路径」，
            消除 Python cwd vs Stata cwd 不一致导致的沙箱绕过。

    Returns:
        Stata 输出文本（可能包含分页导航）。
    """
    global _last_output

    # 输入验证（返回错误前已缓存的交互由调用方处理，此处直接返回错误结果）
    if not cmd or not cmd.strip():
        return _make_error_result("(无有效命令)")
    if len(cmd) > MAX_COMMAND_LENGTH:
        return _make_error_result(
            f"错误: 命令过长（{len(cmd)} 字符），上限 {MAX_COMMAND_LENGTH} 字符"
        )

    with _stata_lock:
        # 锁内路径解析：用 Stata cwd 解析 require_file 为绝对路径并经沙箱权威校验，
        # 再把命令里嵌入的 Python-cwd 路径替换为 Stata 绝对路径。
        # 校验路径即执行路径，根除 Python cwd vs Stata cwd 不一致的沙箱绕过。
        if require_file:
            stata_abs, err = _resolve_stata_path_locked(require_file)
            if err:
                return _make_error_result(err)
            # 工具构造命令时用 _normalize_path(require_file)（Python-cwd 绝对路径）嵌入，
            # 此处替换为 Stata-cwd 绝对路径，使 Stata 实际执行用解析后的路径。
            py_abs = _normalize_path(require_file)
            if py_abs != stata_abs:
                cmd = cmd.replace(py_abs, stata_abs)
            if not os.path.isfile(stata_abs):
                return _make_error_result(f"错误: 文件不存在 — {stata_abs}")

        # 使用新的解析器：正确处理 /// 续行和 { } 复合块
        try:
            blocks = _parse_command_blocks(cmd)
        except UnbalancedBlockError as e:
            # 未闭合的块若送去执行会挂死会话，在此拦下并给出可操作提示
            return _make_error_result(f"错误: {e}")

        if not blocks:
            return _make_error_result("(无有效命令)")

        all_buf = io.StringIO()
        hwritten = False
        had_error = False
        for block in blocks:
            try:
                rc, out = _execute_safe(block, timeout)

                # RC=998: Stata DLL dead, abort chain
                if rc == 998:
                    if hwritten:
                        all_buf.write("\n")
                    all_buf.write(out)
                    hwritten = True
                    had_error = True
                    break

                # STATA_RC_RECOVERED (997) = 崩溃已恢复，当前命令未执行需重试。
                # 非致命：输出恢复提示但不标记 had_error / isError，不显示「内部崩溃」。
                # 中止后续块（break 而非 continue）：当前块未执行，后续块若依赖它会
                # 在陈旧状态上运行；让用户整体重试整条命令链更安全。
                if rc == STATA_RC_RECOVERED:
                    if hwritten:
                        all_buf.write("\n")
                    all_buf.write(out.strip())
                    hwritten = True
                    break

                # STATA_RC_NO_OUTPUT (3000) = 无错误但无实质输出
                if rc != 0 and rc != STATA_RC_NO_OUTPUT:
                    if hwritten:
                        all_buf.write("\n")
                    all_buf.write(_format_error(rc, block, out))
                    hwritten = True
                    had_error = True
                elif out.strip():
                    if hwritten:
                        all_buf.write("\n")
                    all_buf.write(out.strip())
                    hwritten = True

            except SystemError as e:
                if hwritten:
                    all_buf.write("\n")
                all_buf.write(f"Stata 系统错误 ({block[:40]}): {e}")
                hwritten = True
                had_error = True
            except Exception:
                logger.exception("Error executing: %s", block[:200])
                if hwritten:
                    all_buf.write("\n")
                all_buf.write(f"执行错误 ({block[:40]}): {sys.exc_info()[1]}")
                hwritten = True
                had_error = True

        full = all_buf.getvalue() if hwritten else _describe_empty_result()
        # 聚合层同样要收口：_execute_single 的上限只作用于**单个块**，N 个大输出
        # 块拼起来就是 N × 120K（实测 3 条 list 拼出 360,263 字符）。这里截断后
        # _last_output 缓存、错误分支与 stata_more(page=0) 才真正受 120K 约束。
        # 仍先执行完所有块再截断：块的副作用（如 save/export）必须照常发生，
        # 不能因为输出超限就跳过后续命令。
        if len(full) > MAX_OUTPUT_CHARS:
            full = full[:MAX_OUTPUT_CHARS] + _TRUNCATION_NOTICE
        with _output_lock:
            _last_output = full

        # 若任何 block 出错，返回 isError=true
        if had_error:
            return _make_error_result(full)

        # 自动分页：仅当是单条命令且输出超过阈值
        if len(blocks) == 1 and len(full) > PAGE_SIZE:
            return _paginate(full, page)
        elif len(blocks) > 1 and len(full) > PAGE_SIZE * 3:
            # 多命令输出也分页
            return _paginate(full, page)

        return full


def _normalize_path(path: str) -> str:
    """将路径转换为 Stata 可接受的格式（正斜杠）。"""
    return os.path.normpath(os.path.abspath(path)).replace("\\", "/")


def _get_stata_cwd_locked() -> str:
    """在 _stata_lock 保护下查询 Stata 当前工作目录。

    调用者必须已经持有 _stata_lock，否则直接调用本函数会在线程安全上
    违反 Stata DLL 的单线程约束。
    """
    try:
        with stout.RedirectOutput(stout.StataDisplay(), stout.StataError(), stecho=False):
            encoded = config.get_encode_str("display c(pwd)")
            rc = config.stlib.StataSO_Execute(encoded, False)
        if rc not in (0, STATA_RC_NO_OUTPUT):
            return ""
        parts = []
        for _ in range(50):
            out = config.get_output()
            if out:
                parts.append(out)
            elif parts:
                break
            time.sleep(0.001)
        lines = [ln.strip() for ln in "".join(parts).splitlines() if ln.strip()]
        return lines[-1] if lines else ""
    except Exception:
        return ""


def _resolve_stata_path_locked(filepath: str) -> tuple[str, str | None]:
    """在 _stata_lock 保护下，将路径解析为 Stata 实际访问的绝对路径。

    相对路径用 **Stata 当前工作目录** 解析（与后续 Stata 执行的基目录一致），
    若无法获取 Stata cwd 则回退到 Python 进程 cwd。对解析后的绝对路径执行
    _check_abs_path_safety 权威沙箱校验，确保「校验路径 == 执行路径」，
    消除 Python cwd 与 Stata cwd 不一致导致的沙箱绕过。

    调用者必须已经持有 _stata_lock。

    Returns:
        (stata_abs_path, None) 或 ("", error_text)。
    """
    # 字符级预检：仅拦截非法字符，不做沙箱/相对路径检查（沙箱权威校验
    # 在下方对 Stata-cwd 解析后的绝对路径执行，避免 Python cwd 预检误拦）。
    if err := _check_path_chars(filepath):
        return "", err
    if os.path.isabs(filepath):
        abs_path = os.path.normpath(filepath).replace("\\", "/")
    else:
        cwd = _get_stata_cwd_locked()
        base = cwd if cwd else os.getcwd()
        abs_path = os.path.normpath(os.path.join(base, filepath)).replace("\\", "/")
    # 对 Stata 实际访问的绝对路径做权威沙箱校验
    if err := _check_abs_path_safety(abs_path):
        return "", err
    return abs_path, None


# =============================================================================


# =============================================================================
# 生命周期
# =============================================================================


@atexit.register
def _shutdown_stata() -> None:
    """优雅关闭 Stata 会话。"""
    try:
        # 尝试获取锁，避免与正在执行的命令并发访问 DLL。
        if _stata_lock.acquire(timeout=5):
            try:
                if config.is_stata_initialized():
                    config.shutdown()
                    logger.info("Stata shut down cleanly")
            finally:
                _stata_lock.release()
            return

        # 若 5 秒内无法获取锁，尝试打断当前命令后再试一次
        logger.warning("Stata shutdown: _stata_lock held by active command, issuing break...")
        try:
            _set_break()
        except Exception:
            pass
        time.sleep(1.5)
        if _stata_lock.acquire(timeout=3):
            try:
                if config.is_stata_initialized():
                    config.shutdown()
                    logger.info("Stata shut down cleanly after break")
            finally:
                _stata_lock.release()
            return

        logger.error("Stata shutdown failed: unable to acquire _stata_lock after break")
    except Exception:
        logger.exception("Error during Stata shutdown")


# =============================================================================
# MCP 工具 — 核心执行
# =============================================================================


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def stata_run(command: str, page: int = 1, timeout: int = 60) -> str | ToolResult:
    """执行一条或多条 Stata 命令并返回输出。

    这是最核心的工具，可以执行任意 Stata 命令。
    支持多行命令，每行一条命令。支持数据加载、统计分析、
    回归、图形生成、数据管理等各种 Stata 操作。

    当输出过长时自动分页。使用 stata_more 工具翻页浏览。

    内置安全机制：
    - 执行前自动检测 Stata DLL 存活（ping）
    - 若 DLL 无响应，返回明确错误信息而非崩溃
    - 若命令超时，自动中断返回而非挂起

    使用示例：
    - 单条命令: "summarize mpg"
    - 多条命令: "sysuse auto, clear\\nsummarize mpg\\ntabulate foreign"

    Args:
        command: Stata 命令，多条命令用 \\n 分隔。
        page: 页码（1-based），仅对单条命令有效。默认 1。
        timeout: 命令超时秒数（默认 60，最长 1800）。

    Returns:
        Stata 输出文本（可能包含分页导航）。
    """
    # 限定时长范围；拒绝可能破坏 MCP stdio  transport 的空字节与显著危险前缀
    safe_timeout = max(10, min(timeout, 1800))
    if "\x00" in command:
        return _make_error_result("错误: command 包含空字节")
    # 入口预检：危险前缀（校验解析后的执行块）+ 块闭合性（未闭合会挂死会话）
    if reason := _precheck_command(command):
        return _make_error_result(reason)
    return _run_stata_command(command, page, timeout=safe_timeout)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def stata_run_do_file(filepath: str, timeout: int = 300) -> str | ToolResult:
    """执行一个 Stata .do 文件并返回全部输出。

    .do 文件是 Stata 的批处理脚本。此工具会执行指定路径的 .do 文件。

    注意：do 文件由 Stata 自行解析，**不经过** ``stata_run`` 的危险命令前缀
    护栏。只执行你信任的 do 文件。

    Args:
        filepath: .do 文件的绝对路径。
        timeout: 超时秒数（默认 300，范围 10–1800）。do 文件往往是最长跑的
            任务，跑批量建模或大数据清洗时请显式调大。

    Returns:
        do 文件执行过程中的全部 Stata 输出。
    """
    safe_timeout = max(10, min(timeout, 1800))
    return _run_stata_command(
        f'do "{_normalize_path(filepath)}"', require_file=filepath, timeout=safe_timeout
    )


# =============================================================================
# MCP 工具 — 数据管理 (destructiveHint=True)
# =============================================================================


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def stata_use_dataset(filepath: str, clear: bool = True) -> str | ToolResult:
    """加载 Stata 数据集 (.dta 文件) 到内存中。

    加载后可使用 stata_describe、stata_summarize 等工具查看数据。

    Args:
        filepath: .dta 文件的绝对路径。
        clear: 是否先清除内存中的已有数据（默认 True）。

    Returns:
        数据集加载确认信息及变量列表。
    """
    normalized = _normalize_path(filepath)
    suffix = ", clear" if clear else ""
    return _run_stata_command(f'use "{normalized}"{suffix}', require_file=filepath)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def stata_save_dataset(filepath: str, replace: bool = False) -> str | ToolResult:
    """将当前内存中的数据集保存为 .dta 文件。

    Args:
        filepath: 保存路径（建议使用 .dta 扩展名）。
        replace: 是否覆盖已有文件（默认 False）。

    Returns:
        保存确认信息。
    """
    if err := _validate_path(filepath):
        return _result_or_error(err)
    normalized = _normalize_path(filepath)
    suffix = ", replace" if replace else ""
    return _run_stata_command(f'save "{normalized}"{suffix}')


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def stata_set_cwd(path: str) -> str | ToolResult:
    """更改 Stata 的工作目录。

    Args:
        path: 新的工作目录路径。

    Returns:
        当前工作目录确认信息。
    """
    if err := _validate_path(path):
        return _result_or_error(err)
    return _run_stata_command(f'cd "{_normalize_path(path)}"')


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def stata_generate(
    newvar: str,
    expression: str,
    condition: str = "",
) -> str | ToolResult:
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
    if err := _validate_identifier(newvar, "newvar", required=True):
        return _result_or_error(err)
    if not expression.strip():
        return _make_error_result("错误: 请提供赋值表达式")
    if err := _validate_no_injection(expression, "expression"):
        return _result_or_error(err)
    if err := _validate_no_injection(condition, "condition"):
        return _result_or_error(err)
    cmd = f"generate {newvar} = {expression.strip()}"
    if condition.strip():
        cmd += f" if {condition.strip()}"
    return _run_stata_command(cmd)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def stata_egen(
    newvar: str,
    fcn: str,
    by: str = "",
    condition: str = "",
) -> str | ToolResult:
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
    if err := _validate_identifier(newvar, "newvar", required=True):
        return _result_or_error(err)
    if not fcn.strip():
        return _make_error_result("错误: 请提供 egen 函数，如 mean(price)")
    if err := _validate_no_injection(fcn, "fcn"):
        return _result_or_error(err)
    if err := _validate_varlist(by, "by"):
        return _result_or_error(err)
    if err := _validate_no_injection(condition, "condition"):
        return _result_or_error(err)
    prefix = f"bysort {by.strip()}: " if by.strip() else ""
    cmd = f"{prefix}egen {newvar} = {fcn.strip()}"
    if condition.strip():
        cmd += f" if {condition.strip()}"
    return _run_stata_command(cmd)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def stata_predict(
    newvar: str,
    options: str = "",
    condition: str = "",
) -> str | ToolResult:
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
    if err := _validate_identifier(newvar, "newvar", required=True):
        return _result_or_error(err)
    if err := _validate_no_injection(options, "options"):
        return _result_or_error(err)
    if err := _validate_no_injection(condition, "condition"):
        return _result_or_error(err)
    cmd = f"predict {newvar}"
    if condition.strip():
        cmd += f" if {condition.strip()}"
    if options.strip():
        cmd += f", {options.strip()}"
    return _run_stata_command(cmd)


# =============================================================================
# MCP 工具 — 数据探索 (readOnlyHint=True)
# =============================================================================


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_describe(varlist: str = "", simple: bool = False) -> str | ToolResult:
    """描述当前数据集的变量信息。

    显示变量名、存储类型、显示格式、变量标签和值标签。
    使用 simple=True 可获得更精简的输出。

    Args:
        varlist: 要描述的变量（空格分隔），留空 = 全部变量。
        simple: 是否使用精简模式（默认 False）。

    Returns:
        变量描述信息表。
    """
    if err := _validate_varlist(varlist, "varlist"):
        return _result_or_error(err)
    if simple:
        cmd = "describe, simple"
    elif varlist.strip():
        cmd = f"describe {varlist}"
    else:
        cmd = "describe"
    return _run_stata_command(cmd)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_summarize(
    varlist: str = "",
    detail: bool = False,
    condition: str = "",
) -> str | ToolResult:
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
    if err := _validate_varlist(varlist, "varlist"):
        return _result_or_error(err)
    if err := _validate_no_injection(condition, "condition"):
        return _result_or_error(err)
    cmd = f"summarize {varlist}".strip()
    if condition.strip():
        cmd += f" if {condition.strip()}"
    if detail:
        cmd += ", detail"
    return _run_stata_command(cmd)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_list(
    varlist: str = "",
    n: int = 10,
    in_range: str = "",
    condition: str = "",
) -> str | ToolResult:
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

    Returns:
        数据表格。
    """
    if err := _validate_varlist(varlist, "varlist"):
        return _result_or_error(err)
    if err := _validate_no_injection(condition, "condition"):
        return _result_or_error(err)
    if err := _validate_no_injection(in_range, "in_range"):
        return _result_or_error(err)
    if n < 0:
        return _make_error_result("错误: n 不能为负数")
    cmd = "list"
    if varlist.strip():
        cmd += f" {varlist}"
    if condition.strip():
        cmd += f" if {condition.strip()}"
    if in_range.strip():
        cmd += f" in {in_range}"
    elif n > 0:
        cmd += f" in 1/{n}"
    return _run_stata_command(cmd)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_codebook(
    varlist: str = "",
    compact: bool = False,
    condition: str = "",
) -> str | ToolResult:
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
    if err := _validate_varlist(varlist, "varlist"):
        return _result_or_error(err)
    if err := _validate_no_injection(condition, "condition"):
        return _result_or_error(err)
    cmd = f"codebook {varlist}".strip()
    if condition.strip():
        cmd += f" if {condition.strip()}"
    if compact:
        cmd += ", compact"
    return _run_stata_command(cmd)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_tabulate(
    varname: str,
    byvar: str = "",
    chi2: bool = False,
    condition: str = "",
) -> str | ToolResult:
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
        return _make_error_result("错误：请提供至少一个变量名。")
    if err := _validate_identifier(varname, "varname", required=True):
        return _result_or_error(err)
    if err := _validate_identifier(byvar, "byvar"):
        return _result_or_error(err)
    if err := _validate_no_injection(condition, "condition"):
        return _result_or_error(err)
    cmd = f"tabulate {varname}"
    if byvar.strip():
        cmd += f" {byvar}"
    if condition.strip():
        cmd += f" if {condition.strip()}"
    if byvar.strip() and chi2:
        cmd += ", chi2"
    return _run_stata_command(cmd)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_display(expression: str) -> str | ToolResult:
    """计算并显示 Stata 表达式的结果。

    可用于简单计算、宏展开、返回值查看。
    适合查看 r(mean)、e(N)、e(r2) 等存储结果。

    Args:
        expression: Stata 表达式，如 "2+2"、"r(mean)"、"e(r2)"。

    Returns:
        表达式计算结果。
    """
    if err := _validate_no_injection(expression, "expression"):
        return _result_or_error(err)
    return _run_stata_command(f"display {expression}")


# =============================================================================
# MCP 工具 — 统计分析 (readOnlyHint=True)
# =============================================================================


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_regress(
    depvar: str,
    indepvars: str,
    options: str = "",
    condition: str = "",
) -> str | ToolResult:
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
    if err := _validate_identifier(depvar, "depvar", required=True):
        return _result_or_error(err)
    if err := _validate_varlist(indepvars, "indepvars"):
        return _result_or_error(err)
    if err := _validate_no_injection(condition, "condition"):
        return _result_or_error(err)
    if err := _validate_no_injection(options, "options"):
        return _result_or_error(err)
    cmd = f"regress {depvar} {indepvars}"
    if condition.strip():
        cmd += f" if {condition.strip()}"
    if options.strip():
        cmd += f", {options}"
    return _run_stata_command(cmd)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_logistic(
    depvar: str,
    indepvars: str,
    options: str = "",
    condition: str = "",
) -> str | ToolResult:
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
    if err := _validate_identifier(depvar, "depvar", required=True):
        return _result_or_error(err)
    if err := _validate_varlist(indepvars, "indepvars"):
        return _result_or_error(err)
    if err := _validate_no_injection(condition, "condition"):
        return _result_or_error(err)
    if err := _validate_no_injection(options, "options"):
        return _result_or_error(err)
    cmd = f"logistic {depvar} {indepvars}"
    if condition.strip():
        cmd += f" if {condition.strip()}"
    if options.strip():
        cmd += f", {options}"
    return _run_stata_command(cmd)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_ttest(
    varname: str,
    byvar: str = "",
    options: str = "",
    condition: str = "",
) -> str | ToolResult:
    """运行单样本或独立样本 t 检验。

    ``varname`` 只接受单个变量名，因此**不支持配对 t 检验**（其语法为
    ``ttest var1 == var2``）。需要配对检验时请直接用
    ``stata_run("ttest before == after")``。

    Args:
        varname: 要检验的变量名（单个）。
        byvar: 分组变量（可选）。给出时做独立样本 t 检验，否则做单样本检验。
        options: 额外选项，如 "unequal"、"level(90)"。
        condition: if 条件子句（可选）。例："!missing(price)".

    Returns:
        t 检验结果表。
    """
    if err := _validate_identifier(varname, "varname", required=True):
        return _result_or_error(err)
    if err := _validate_identifier(byvar, "byvar"):
        return _result_or_error(err)
    if err := _validate_no_injection(condition, "condition"):
        return _result_or_error(err)
    if err := _validate_no_injection(options, "options"):
        return _result_or_error(err)
    if byvar.strip():
        cmd = f"ttest {varname}"
        if condition.strip():
            cmd += f" if {condition.strip()}"
        cmd += f", by({byvar})"
        if options.strip():
            cmd += f" {options}"
    else:
        cmd = f"ttest {varname}"
        if condition.strip():
            cmd += f" if {condition.strip()}"
        if options.strip():
            cmd += f", {options}"
    return _run_stata_command(cmd)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_probit(
    depvar: str,
    indepvars: str,
    marginal_effects: bool = False,
    options: str = "",
    condition: str = "",
) -> str | ToolResult:
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
    if err := _validate_identifier(depvar, "depvar", required=True):
        return _result_or_error(err)
    if err := _validate_varlist(indepvars, "indepvars"):
        return _result_or_error(err)
    if err := _validate_no_injection(condition, "condition"):
        return _result_or_error(err)
    if err := _validate_no_injection(options, "options"):
        return _result_or_error(err)
    cmd = f"probit {depvar} {indepvars}"
    if condition.strip():
        cmd += f" if {condition.strip()}"
    if options.strip():
        cmd += f", {options}"
    if marginal_effects:
        cmd += "\nmargins, dydx(*)"
    return _run_stata_command(cmd)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_poisson(
    depvar: str,
    indepvars: str,
    irr: bool = False,
    options: str = "",
    condition: str = "",
) -> str | ToolResult:
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
    if err := _validate_identifier(depvar, "depvar", required=True):
        return _result_or_error(err)
    if err := _validate_varlist(indepvars, "indepvars"):
        return _result_or_error(err)
    if err := _validate_no_injection(condition, "condition"):
        return _result_or_error(err)
    if err := _validate_no_injection(options, "options"):
        return _result_or_error(err)
    cmd = f"poisson {depvar} {indepvars}"
    if condition.strip():
        cmd += f" if {condition.strip()}"
    opt_parts = [p for p in (("irr" if irr else ""), options.strip()) if p]
    if opt_parts:
        cmd += f", {' '.join(opt_parts)}"
    return _run_stata_command(cmd)


# 面板估计量白名单：作为 xtreg 的选项拼接，用正向白名单杜绝注入
_XTREG_EFFECTS = {"fe", "re", "be", "mle", "pa"}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_xtreg(
    depvar: str,
    indepvars: str,
    effects: str = "fe",
    options: str = "",
    condition: str = "",
) -> str | ToolResult:
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
    if err := _validate_identifier(depvar, "depvar", required=True):
        return _result_or_error(err)
    if err := _validate_varlist(indepvars, "indepvars"):
        return _result_or_error(err)
    if err := _validate_no_injection(condition, "condition"):
        return _result_or_error(err)
    if err := _validate_no_injection(options, "options"):
        return _result_or_error(err)
    eff = effects.strip().lower()
    if eff not in _XTREG_EFFECTS:
        return _make_error_result(
            f"错误: effects 只能是 {', '.join(sorted(_XTREG_EFFECTS))} 之一，收到 '{effects}'"
        )
    cmd = f"xtreg {depvar} {indepvars}"
    if condition.strip():
        cmd += f" if {condition.strip()}"
    opt_parts = [p for p in (eff, options.strip()) if p]
    cmd += f", {' '.join(opt_parts)}"
    return _run_stata_command(cmd)


# IV 估计量白名单
_IVREGRESS_ESTIMATORS = {"2sls", "liml", "gmm"}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_ivregress(
    depvar: str,
    endogenous: str,
    instruments: str,
    exogenous: str = "",
    estimator: str = "2sls",
    options: str = "",
    condition: str = "",
) -> str | ToolResult:
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
    if err := _validate_identifier(depvar, "depvar", required=True):
        return _result_or_error(err)
    if err := _validate_varlist(endogenous, "endogenous"):
        return _result_or_error(err)
    if not endogenous.strip():
        return _make_error_result("错误: 至少需要一个内生变量")
    if err := _validate_varlist(instruments, "instruments"):
        return _result_or_error(err)
    if not instruments.strip():
        return _make_error_result("错误: 至少需要一个工具变量")
    if err := _validate_varlist(exogenous, "exogenous"):
        return _result_or_error(err)
    if err := _validate_no_injection(condition, "condition"):
        return _result_or_error(err)
    if err := _validate_no_injection(options, "options"):
        return _result_or_error(err)
    est = estimator.strip().lower()
    if est not in _IVREGRESS_ESTIMATORS:
        return _make_error_result(
            f"错误: estimator 只能是 {', '.join(sorted(_IVREGRESS_ESTIMATORS))} 之一，收到 '{estimator}'"
        )
    exog = f" {exogenous.strip()}" if exogenous.strip() else ""
    cmd = f"ivregress {est} {depvar}{exog} ({endogenous.strip()} = {instruments.strip()})"
    if condition.strip():
        cmd += f" if {condition.strip()}"
    if options.strip():
        cmd += f", {options}"
    return _run_stata_command(cmd)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_correlate(
    varlist: str = "",
    pairwise: bool = False,
    options: str = "",
    condition: str = "",
) -> str | ToolResult:
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
    if err := _validate_varlist(varlist, "varlist"):
        return _result_or_error(err)
    if err := _validate_no_injection(condition, "condition"):
        return _result_or_error(err)
    if err := _validate_no_injection(options, "options"):
        return _result_or_error(err)
    base = "pwcorr" if pairwise else "correlate"
    cmd = base
    if varlist.strip():
        cmd += f" {varlist}"
    if condition.strip():
        cmd += f" if {condition.strip()}"
    if options.strip():
        cmd += f", {options}"
    return _run_stata_command(cmd)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_margins(
    marginlist: str = "",
    dydx: str = "",
    at: str = "",
    options: str = "",
) -> str | ToolResult:
    """估计边际效应 / 预测边际（margins，后估计命令）。

    **前提**：先运行过一个估计命令（regress/logit/probit 等）。probit/logit 的
    系数不可直接解读，``margins, dydx(*)`` 给出平均边际效应。

    Args:
        marginlist: 因子变量的边际（如 "foreign"、"i.rep78"），可留空。
        dydx: 求哪些变量的边际效应，如 "price"、"*"（全部）。
        at: 在何处求值，如 "(mean) _all"、"age=(20 40 60)"。
        options: 额外选项，如 "atmeans"、"vce(unconditional)"。

    Returns:
        边际效应表。
    """
    if err := _validate_varlist(marginlist, "marginlist"):
        return _result_or_error(err)
    if err := _validate_no_injection(dydx, "dydx"):
        return _result_or_error(err)
    if err := _validate_no_injection(at, "at"):
        return _result_or_error(err)
    if err := _validate_no_injection(options, "options"):
        return _result_or_error(err)
    cmd = "margins"
    if marginlist.strip():
        cmd += f" {marginlist.strip()}"
    opt_parts = []
    if dydx.strip():
        opt_parts.append(f"dydx({dydx.strip()})")
    if at.strip():
        opt_parts.append(f"at({at.strip()})")
    if options.strip():
        opt_parts.append(options.strip())
    if opt_parts:
        cmd += f", {' '.join(opt_parts)}"
    return _run_stata_command(cmd)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_test(spec: str) -> str | ToolResult:
    """对上一个估计结果做 Wald 检验（test，后估计命令）。

    **前提**：先运行过一个估计命令。

    Args:
        spec: 检验设定。例：
            - "weight mpg"        联合显著性：weight=0 且 mpg=0
            - "weight = mpg"      系数相等
            - "weight = 0.5"      系数等于某值

    Returns:
        Wald 检验结果（F 或 chi2 统计量与 p 值）。
    """
    if not spec.strip():
        return _make_error_result("错误: 请提供检验设定，如 'weight mpg' 或 'weight = mpg'")
    if err := _validate_no_injection(spec, "spec"):
        return _result_or_error(err)
    return _run_stata_command(f"test {spec.strip()}")


# =============================================================================
# MCP 工具 — 图形 (readOnlyHint=True)
# =============================================================================

GRAPH_SCHEMES = {
    "s2color": "Stata 默认彩色",
    "s2mono": "黑白/灰度",
    "s2manual": "Stata 手册风格",
    "economist": "The Economist 杂志风格（需安装）",
    "cleanplots": "简洁出版风格（需安装）",
    "plottig": "Tufte/Edward 风格（需安装）",
}


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


# 矢量图格式：graph export 的 width()/height() 以英寸计（0.5–20），
# 位图格式则以像素计。把像素值传给矢量格式会直接 r(198)。
_VECTOR_GRAPH_EXTS = frozenset({".pdf", ".eps", ".ps", ".svg", ".emf", ".wmf"})


def _graph_size_options(export_path: str, width: int, height: int) -> tuple[str, str]:
    """按导出格式生成 graph export 的尺寸选项，并说明被忽略的取值。

    实测：对 .pdf 传 ``width(800)`` 会失败并输出
    "width() must be a number between 0.5 and 20" —— 因为矢量格式的单位是英寸。
    故矢量格式下超出英寸范围的取值一律丢弃，并把这一决定回报给调用方，
    避免「悄悄改了参数」。

    Returns:
        (选项串, 说明文本)；无需说明时说明文本为空串。
    """
    ext = os.path.splitext(export_path)[1].lower()
    if ext not in _VECTOR_GRAPH_EXTS:
        opts = [f"width({width})"] if width > 0 else []
        if height > 0:
            opts.append(f"height({height})")
        return " ".join(opts), ""

    kept, dropped = [], []
    for label, value in (("width", width), ("height", height)):
        if value <= 0:
            continue
        if 1 <= value <= 20:
            kept.append(f"{label}({value})")
        else:
            dropped.append(f"{label}={value}")
    note = ""
    if dropped:
        note = (
            f"提示：{ext} 为矢量格式，width()/height() 单位是英寸（0.5–20），"
            f"已忽略像素取值 {', '.join(dropped)}，改用 Stata 默认尺寸。"
        )
    return " ".join(kept), note


def _mtime_ns(path: str) -> int | None:
    """文件的纳秒级修改时间；不存在或无法读取时返回 None。

    不能直接用 ``os.stat``：调用点在 ``os.path.isfile`` 之后，两者之间文件
    可能已被删除或替换。
    """
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def _file_written_since(path: str, before_ns: int | None) -> bool:
    """判断文件是否在本次调用中被真正写入（新建或覆盖）。

    ``before_ns`` 为调用前的 ``st_mtime_ns``，文件当时不存在则为 None。
    仅检查「文件存在」不足以判定成功：replace=False 且目标已存在时 Stata 会
    拒绝写入，文件却依然在原处。
    """
    if not os.path.isfile(path):
        return False
    if before_ns is None:
        return True
    now_ns = _mtime_ns(path)
    return now_ns is None or now_ns != before_ns


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


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def stata_graph(
    command: str,
    scheme: str = "s2color",
    export: str = "",
    width: int = 800,
    height: int = 0,
    replace: bool = False,
) -> str | ToolResult:
    """生成 Stata 图形并可选导出为文件。

    自动在命令前设置图形方案（scheme）。
    当指定 export 时,使用 { } 复合块将 graph + export 包装在单次
    StataSO_Execute 调用中,避免图形窗口在 headless 环境中丢失。

    支持两种导出方式（推荐使用第一种）：
    1. export 参数 — 自动生成 { } 复合块，无需临时文件
    2. 或在 command 中手动写 capture noisily { ... }

    Args:
        command: 图形命令(scatter mpg weight, histogram price 等)。
        scheme: 图形方案(默认 's2color')。常用方案:
                s2color(默认彩色), s2mono(灰度), s2manual(手册风格)。
        export: 导出图形文件路径（留空不导出）。支持 .png/.pdf/.svg/.emf/.wmf 等格式；
                Stata 按扩展名自动推断格式。例:"C:/output/scatter.png"。
        width: 导出图片宽度(像素,默认 800)。
        height: 导出图片高度(像素,默认 0 表示不指定)。
        replace: 是否覆盖已有文件(默认 False)。

    Returns:
        图形生成确认信息。
    """
    try:
        if "\x00" in command or "\n" in command or "\r" in command:
            return _make_error_result("错误: command 包含非法控制字符")
        # command 是自由文本，会被原样拼进要执行的命令串（导出模式下还会进入
        # 临时 do 文件），因此必须与 stata_run 走同一层护栏 —— 实测
        # stata_graph(command='!touch /tmp/x') 曾能真实创建文件。
        # 同样要校验解析后的块：`sh/*x*/ell …` 在原始文本里不含 shell 一词。
        if reason := _precheck_command(command):
            return _make_error_result(reason)
        if err := _validate_scheme_name(scheme):
            return _result_or_error(err)
        # 负值会被原样拼成 width(-100) 交给 Stata；实测虽因图形命令先失败而未暴露，
        # 但语义上无意义，应在入口拒绝而不是依赖下游偶然报错。
        for label, value in (("width", width), ("height", height)):
            if value < 0:
                return _make_error_result(f"错误: {label} 不能为负数（{value}）")
        if export:
            if err := _validate_path(export):
                return _result_or_error(err)
            if _has_unsafe_brace(command):
                return _make_error_result(
                    "错误: graph command 中包含会破坏复合块的 '}'，"
                    "请避免在 command 中使用未转义的右花括号（字符串内除外）"
                )

        if not export:
            return _run_stata_command(f"set scheme {scheme}\n{command}", timeout=120)

        # 导出模式：使用 { } 复合块确保 graph + export 原子执行
        export_path = _normalize_path(export)
        replace_opt = "replace" if replace else ""
        size_opts, size_note = _graph_size_options(export_path, width, height)

        # 复合块内的错误被 capture 吞掉（rc 恒为 0），无法据此判断成败；
        # 改以「文件是否被这次调用新写入」为准，故先记录调用前的状态。
        # 只看文件存在与否不够：replace=False 且目标已存在时 Stata 会拒绝写入，
        # 而文件依旧在，会被误判成功。
        before_ns = _mtime_ns(export_path) if os.path.isfile(export_path) else None

        compound = (
            f"capture noisily {{\n"
            f"    set graphics off\n"
            f"    set scheme {scheme}\n"
            f"    {command}\n"
            f'    graph export "{export_path}", {replace_opt} {size_opts}\n'
            f"}}\n"
            f"capture noisily graph drop _all"
        )

        result = _run_stata_command(compound, timeout=120)

        # 若 _run_stata_command 已标记错误，直接透传，不追加成功提示
        if isinstance(result, ToolResult):
            return result

        # 以文件是否被本次调用写入为准，而非 rc —— capture 已把块内错误吞掉。
        if not _file_written_since(export_path, before_ns):
            hint = ""
            if before_ns is not None and not replace:
                hint = "\n提示：目标文件已存在且 replace=False，如需覆盖请传 replace=True。"
            return _make_error_result(
                f"错误: 图形导出失败，未生成文件 {export_path}{hint}\n{result.strip()}"
            )

        result += f"\n(图形已导出: {export_path}, {_format_size(export_path)})"
        if size_note:
            result += f"\n{size_note}"
        return result

    except Exception as e:
        return _make_error_result(f"图形生成失败: {type(e).__name__}: {e}")


# =============================================================================
# MCP 工具 — Excel 导出 (destructiveHint=True)
# =============================================================================


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def stata_export_excel(
    filepath: str,
    varlist: str = "",
    sheet: str = "Sheet1",
    replace: bool = False,
    results: bool = False,
) -> str | ToolResult:
    """将当前数据集导出为 Excel (.xlsx) 文件，或将回归结果导出为 CSV。

    使用 Stata 的 export excel 命令导出数据。
    当 results=True 时，使用 esttab 导出回归结果表；esttab 不支持 xlsx
    与 sheet() 选项，因此强制输出为 CSV（如原路径为 .xlsx，会自动改
    为 .csv 并提示）。

    Args:
        filepath: 导出路径（数据导出建议 .xlsx；回归结果导出会改为 .csv）。
        varlist: 要导出的变量列表（空格分隔），留空 = 全部变量。
        sheet: Excel 工作表名（默认 "Sheet1"，仅用于数据导出）。
        replace: 是否覆盖已有文件（默认 False）。
        results: 若为 True，将当前存储的回归结果导出为 CSV 表格而非原始数据。

    Returns:
        导出确认信息。
    """
    if err := _validate_path(filepath):
        return _result_or_error(err)
    if err := _validate_varlist(varlist, "varlist"):
        return _result_or_error(err)
    if err := _validate_sheet_name(sheet):
        return _result_or_error(err)

    export_path = _normalize_path(filepath)
    replace_opt = "replace" if replace else ""
    firstrow_opt = "firstrow(variables)"

    if results:
        # esttab 不支持 xlsx/sheet，统一输出 CSV
        base, ext = os.path.splitext(export_path)
        if ext.lower() != ".csv":
            export_path = base + ".csv"
            if ext.lower() == ".xlsx":
                changed_msg = (
                    f"提示：回归结果导出不支持 .xlsx/sheet()，已自动改用 CSV 路径：{export_path}\n"
                )
            else:
                changed_msg = f"提示：回归结果已导出为 CSV：{export_path}\n"
        else:
            changed_msg = ""

        # 前置探测 estout 是否已安装：缺失则直接报错，引导用户用
        # stata_install_package("estout") 手动安装。绝不在此内嵌 ssc install ——
        # headless 环境下 SSC 网络请求会阻塞 StataSO_Execute，看门狗的 SetBreak
        # 无法干净中断网络 I/O，会损坏 DLL 状态导致后续调用全部卡死。
        #
        # 必须用裸 which，不能加 capture：capture 的语义就是吞掉命令自身的错误、
        # 只写入 _rc，实测（Stata 19.5 MP）`capture which <pkg>` 在已装与未装
        # 两种情况下一律返回 rc=0，无法区分。裸 which 已装返回 0、未装返回 111。
        #
        # 锁内执行：Stata DLL 非线程安全，且 _execute_safe 会 drain 输出缓冲，
        # 不加锁会抢走并发命令的输出。_ping_stata 不持 _stata_lock，无重入风险。
        # 探测与下方 esttab 分属两段临界区（Lock 不可重入，不能跨 _run_stata_command）。
        with _stata_lock:
            probe_rc, probe_out = _execute_safe(_ESTOUT_PROBE_CMD, timeout=20)

        if probe_rc == 998:
            # DLL 无响应：透传原始诊断（含「重启 MCP Server」指引）。误报为
            # 「未安装」会让用户去装包，而错过真正需要的恢复步骤。
            return _make_error_result(probe_out)
        if probe_rc == STATA_RC_RECOVERED:
            # 崩溃已恢复、探测命令未执行：按 997 契约返回非致命提示，不标 isError。
            return probe_out.strip()
        if probe_rc not in (0, STATA_RC_NO_OUTPUT):
            return _result_or_error(
                "错误: 未安装 estout（esttab 所依赖），无法导出回归结果。\n"
                "请执行这一条安装命令（联网，会阻塞几秒到十几秒）：\n"
                '    stata_install_package("estout", source="ssc", timeout=120)\n'
                "装好后重试本次导出即可，无需改动其他步骤。"
            )

        cmd = (
            f'esttab using "{export_path}", csv {replace_opt} '
            f"plain nogaps nomtitles nonumber"
        )
    else:
        changed_msg = ""
        # 导出数据集为 Excel
        if varlist.strip():
            cmd = (
                f'export excel {varlist} using "{export_path}", '
                f'{replace_opt} {firstrow_opt} sheet("{sheet}")'
            )
        else:
            cmd = (
                f'export excel using "{export_path}", {replace_opt} {firstrow_opt} sheet("{sheet}")'
            )

    # 导出成败以「文件是否被这次调用写入」为准，不能只看文件是否存在：
    # 上次运行留下的同名文件会把失败伪装成成功。实测 rc=997（崩溃已恢复、
    # 命令未执行）时，旧文件仍在，原实现回报「已导出 28 B」。
    before_ns = _mtime_ns(export_path) if os.path.isfile(export_path) else None

    result = _run_stata_command(cmd, timeout=120)

    # 若 _run_stata_command 已标记错误，直接透传
    if isinstance(result, ToolResult):
        return result

    if not _file_written_since(export_path, before_ns):
        hint = ""
        if before_ns is not None and not replace:
            hint = "\n提示：目标文件已存在且 replace=False，如需覆盖请传 replace=True。"
        return _make_error_result(
            f"错误: 导出失败，未写入文件 {export_path}{hint}\n{changed_msg}{result.strip()}"
        )

    return f"{changed_msg}已导出 {_format_size(export_path)} -> {export_path}\n{result}"


# =============================================================================
# MCP 工具 — 包管理 (destructiveHint=True)
# =============================================================================


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def stata_install_package(
    package: str, source: str = "ssc", replace: bool = False, timeout: int = 300
) -> str | ToolResult:
    """安装 Stata 扩展包（联网）。

    从 ssc 或完整 from() URL 安装 Stata 包。支持 replace 解决版本冲突。

    **这是唯一的联网安装入口，请单独调用、装完再继续原任务**：`ssc install`
    是网络阻塞调用（实测同一包 3–13s 波动，慢网络更久），执行期间独占串行锁、
    冻结整个 server。不要把 `ssc install` 内嵌进 `stata_run` 的分析步骤里。

    ``timeout`` 是真实兜底：安装超过它时看门狗会**干净中断**（实测超时的
    `ssc install` 被 break 后会话健康、包不残留半装状态），返回超时提示而非
    卡死。下限受 `stata_run` 约束为 10s；慢网络下装大包建议 120–300s。

    Args:
        package: 包名称（如 "outreg2"、"estout"、"ivreg2"）。
        source: 安装源 — "ssc"（默认）或完整的 from() URL。
                例："https://fmwww.bc.edu/RePEc/bocode/o"
        replace: 是否强制替换已有文件（解决版本冲突，默认 False）。
        timeout: 安装超时秒数（默认 300）。超时则中断并提示，不会卡死会话。

    Returns:
        安装过程输出。
    """
    if err := _validate_identifier(package, "package"):
        return _result_or_error(err)
    if err := _validate_install_source(source):
        return _result_or_error(err)
    replace_opt = ", replace" if replace else ""
    src_lower = source.lower().strip()
    if src_lower == "ssc":
        cmd = f"ssc install {package}{replace_opt}"
    else:
        cmd = f"net install {package}{replace_opt}, from({source.strip()})"
    return _run_stata_command(cmd, timeout=timeout)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def stata_uninstall_package(package: str) -> str | ToolResult:
    """卸载一个已安装的 Stata 扩展包（删除其 ado 文件）。

    与 ``stata_install_package`` 对称，补全包的安装/卸载生命周期。执行
    ``ado uninstall <package>``，这是**纯本地**操作（只删文件，不联网），
    实测约 20ms，不存在 SSC 网络请求卡死 DLL 的风险。

    包未安装时 Stata 返回 r(111) ``package not found``。不确定包名时先用
    ``stata_list_packages`` 查已装清单。

    Args:
        package: 要卸载的包名（须与 ``stata_list_packages`` 列出的名称一致）。

    Returns:
        卸载确认信息。
    """
    if err := _validate_identifier(package, "package", required=True):
        return _result_or_error(err)
    return _run_stata_command(f"ado uninstall {package}")


# 包详情来源白名单：本地已装 vs 联网查 SSC
_DESCRIBE_SOURCES = {"installed", "ssc"}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_describe_package(package: str, source: str = "installed") -> str | ToolResult:
    """查看某个扩展包的详情（作者、功能、包含的文件）。

    两种来源：
    - ``source="installed"``（默认）：``ado describe <package>``，**本地**读取
      已安装包的信息，实测约 12ms，无网络风险。包未安装则报错。
    - ``source="ssc"``：``ssc describe <package>``，**联网**查询 SSC 存档，可在
      安装**前**了解一个包（实测约 1–7s）。网络不可达时会等到超时。

    安装决策流程：``stata_find_package`` 搜索 → ``stata_describe_package(pkg,
    source="ssc")`` 看详情 → ``stata_install_package`` 安装。

    Args:
        package: 包名。
        source: "installed"（本地已装，默认）或 "ssc"（联网查 SSC）。

    Returns:
        包详情文本。
    """
    if err := _validate_identifier(package, "package", required=True):
        return _result_or_error(err)
    src = source.strip().lower()
    if src not in _DESCRIBE_SOURCES:
        return _make_error_result(
            f"错误: source 只能是 {', '.join(sorted(_DESCRIBE_SOURCES))} 之一，收到 '{source}'"
        )
    if src == "ssc":
        return _run_stata_command(f"ssc describe {package}", timeout=120)
    return _run_stata_command(f"ado describe {package}")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_find_package(keyword: str) -> str | ToolResult:
    """搜索可安装的 Stata 扩展包（联网）。

    使用 ``net search``，覆盖 SSC 与 Stata Journal 等 net 资源，返回包名、
    来源 URL 与简介；拿到包名后可交给 ``stata_install_package`` 安装。

    注意：本工具会访问 www.stata.com（实测约 1 秒）。若网络不可达，命令会等到
    超时为止。仅需查看某个已知包的详情时，用 ``stata_run("ssc describe <包名>")``
    更快；仅需搜索本机已安装的帮助文件时，用 ``stata_run("search <词>, local")``。

    Args:
        keyword: 搜索关键词（如 "panel"、"binscatter"、"iv"）。

    Returns:
        匹配的包列表及简要描述。
    """
    if err := _validate_no_injection(keyword, "keyword"):
        return _result_or_error(err)
    if not keyword.strip():
        return _make_error_result("错误：请提供搜索关键词。")
    return _run_stata_command(f"net search {keyword.strip()}", timeout=120)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_list_packages() -> str | ToolResult:
    """列出当前已安装的所有 Stata 扩展包（包名 + 一句简介）。

    用 ``ado dir`` 而非 ``ado describe``：后者会把每个包的完整文档全文吐出来，
    实测本机 49516 字符 / 13 页，而 ``ado dir`` 只要 4330 字符就给出同样的包
    清单。需要某个包的详情时再用 ``stata_run("ado describe <包名>")``。

    Returns:
        已安装包列表。
    """
    return _run_stata_command("ado dir", timeout=120)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_help(command: str, page: int = 1) -> str | ToolResult:
    """查询任意 Stata 命令的官方帮助文档（语法、选项、示例）。

    ``help`` 在 headless 环境下把 SMCL 帮助渲染为纯文本返回（实测可用，不会
    卡在图形查看器），因此本工具**覆盖全部内置命令**（3500+）以及任何已安装的
    外置命令 —— 需要某条命令的权威语法时，先用它查，而不是凭记忆拼命令。

    支持多词主题：
    - 命令：``stata_help("xtreg")``、``stata_help("reghdfe")``
    - 后估计：``stata_help("regress postestimation")``
    - 子命令：``stata_help("estat firststage")``

    帮助文档常常很长，超过阈值会自动分页；用 ``page`` 翻页，或随后调用
    ``stata_more(page=N)``。找不到命令时返回 Stata 的「help for X not found」
    提示（不报错），可改用 ``stata_find_package`` 联网搜索可安装的包。

    Args:
        command: 命令名或帮助主题（可含空格分隔的子主题）。
        page: 分页页码（默认第 1 页）。

    Returns:
        该命令的帮助文本（可能分页）。
    """
    topic = command.strip()
    if not topic:
        return _make_error_result("错误：请提供要查询的命令名。")
    if not _HELP_TOPIC_RE.match(topic):
        return _make_error_result(
            "错误: 命令名只能包含字母、数字、下划线和空格（用于子主题，"
            "如 'xtreg postestimation'）。含其他字符的帮助请用 stata_run 查询。"
        )
    return _run_stata_command(f"help {topic}", page=page, timeout=30)


# =============================================================================
# MCP 工具 — 会话 (readOnlyHint=True)
# =============================================================================


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_more(page: int = 0, page_size: int = 0) -> str | ToolResult:
    """翻页浏览上一条 Stata 命令的完整输出。

    当 stata_run 等工具返回的输出过长时，完整内容被缓存，
    可使用此工具按页浏览。

    Args:
        page: 页码（1-based），0 = 显示全部。默认 0。
        page_size: 每页字符数，0 = 使用默认值 (4000)。默认 0。

    Returns:
        指定页的输出内容及导航信息。
    """
    with _output_lock:
        cached = _last_output
    if not cached:
        return "(没有缓存的输出，请先执行 Stata 命令)"
    ps = page_size if page_size > 0 else PAGE_SIZE
    return _paginate(cached, page, ps)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_status() -> str | ToolResult:
    """获取当前 Stata 会话状态。

    显示当前加载的数据集、变量数量、观测数量、工作目录和内存使用情况。
    变量清单请用 ``stata_describe``，此处只给概览。

    Returns:
        会话状态摘要。
    """
    # 查工作目录必须用 display c(pwd)，不能用裸 cd —— Stata 的 cd 不带参数会
    # **切换**到 home 目录（同 Unix shell）并把新目录打印出来，看着像查询实为修改。
    # 曾因此让本工具在 readOnlyHint=True 的情况下悄悄重置用户 set_cwd 的结果，
    # 使后续相对路径全部指向 home。
    return _run_stata_command("describe, short\ndisplay c(pwd)\nmemory")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_ping() -> str | ToolResult:
    """心跳检测 — 快速测试 Stata MCP Server 是否存活。

    执行一个极简的 Stata 命令(display 42)并返回。
    DLL 已崩溃或无响应时返回 MCP 错误结果（isError=true）并附带诊断输出。

    Returns:
        存活时返回 "pong | Stata <版本> | alive"；异常时返回错误结果。
    """
    global _last_ping_time
    try:
        with _stata_lock:
            rc, result = _execute_single("display 42")
        version = getattr(config, "stversion", "?")
        edition = getattr(config, "stedition", "?")
        if rc in (0, STATA_RC_NO_OUTPUT) and "42" in result:
            # 回写 ping 缓存，使紧接着的 _execute_safe 跳过重复心跳。
            # 必须有上面的 global 声明，否则赋的是函数局部变量，这项优化从未生效。
            with _ping_lock:
                _last_ping_time = time.time()
            return f"pong | Stata {version} {edition} | alive"
        # 探测失败即 DLL 不可用：必须标记 isError，否则调用方看到以 "pong" 开头的
        # 普通字符串会以为一切正常，而 degraded 只藏在末尾。同时把 _execute_single
        # 的原始输出带出去 —— 那里才有崩溃/超时的线索。
        with _ping_lock:
            _last_ping_time = 0.0
        return _make_error_result(
            f"Stata 心跳失败（degraded）| Stata {version} {edition} | 返回码 {rc}\n"
            f"探测输出: {result.strip()[:300] or '(无输出)'}\n"
            "建议: 若持续失败请重启 MCP Server。"
        )
    except Exception as e:
        with _ping_lock:
            _last_ping_time = 0.0
        return _make_error_result(f"Stata 心跳失败: {type(e).__name__}: {e}")


# =============================================================================
# 入口
# =============================================================================

if __name__ == "__main__":
    mcp.run(transport="stdio")
