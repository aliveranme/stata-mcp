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
import shlex
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
# 返回码释义。**每一条都在 Stata 19.5 MP 上真机触发核对过**，不要凭印象增补 ——
# 这段文本由 _format_error 拼在 Stata 自己的报错**之前**，是 Agent 首先读到的
# 一行，给错方向比不给更糟。旧表大半是错的：rc 9 被标成「变量类型不匹配」（真值
# 是 assert 失败 assertion is false，而 type mismatch 其实是 rc **109**），
# rc 4 标成「内存不足」（真值是数据未保存），rc 5 标成「变量不存在」（真值是
# not sorted），rc 199 标成「选项语法错误」（真值是命令不存在）。
# 未收录的返回码退化为「未知返回码(N)」，其后紧跟 Stata 原文，不会丢信息。
STATA_RC_MESSAGES = {
    0: "成功",
    1: "已中断（Break）—— 看门狗超时会走这里",
    2: "网络连接超时",
    3: "当前没有载入数据集",
    4: "内存中的数据集有未保存的修改（加 clear 选项或先 save）",
    5: "数据未按要求排序（先 sort）",
    7: "变量类型不符合命令要求（如需要字符串却是数值）",
    9: "assert 断言不成立（数据不满足断言条件）",
    100: "缺少必需的选项或变量（如 ttest 需要 by()）",
    109: "类型不匹配（type mismatch）",
    110: "变量已存在（用 replace 覆盖或改用新名）",
    111: "变量或命令未找到",
    133: "未知函数",
    198: "语法无效或选项不被允许",
    199: "命令不存在（拼写有误或包未安装）",
    301: "找不到已存储的估计结果（先运行估计命令）",
    459: "变量不能唯一识别观测（isid/duplicates 校验失败）",
    601: "文件不存在",
    602: "文件已存在（加 replace）",
    603: "文件无法打开",
    900: "无法再添加变量（已达变量数上限）",
    2000: "没有观测值（筛选条件未匹配到任何观测）",
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
# STATA_ALLOW_UNC: 可选，设为 "1" 后允许 UNC 网络路径。默认拒绝，且拒绝是**无条件**的
#   —— 不依赖 STATA_ALLOWED_ROOTS 是否配置。

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
    """检测行首的 ``#delimit``（字符串内的同名字样不算）。"""
    return any(
        _split_top_level(raw_line, '"')[0].strip().lower().startswith("#delimit")
        for raw_line in command.split("\n")
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


# generate/egen 的 [type] 位置：官方允许的存储类型（str# / strL 亦合法）。
# 用白名单而非黑名单 —— 该值直接拼进命令的关键字位置，不容许任何自由文本。
_STORAGE_TYPE_RE = re.compile(r"^(byte|int|long|float|double|str[0-9]{1,4}|strL)$")


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

    **输出收集优化**：指数退避快轮询 + 三档智能清尾。
    - 阶段 1 快轮询：间隔 1ms 起、每次翻倍封顶 20ms，连续 3 次空转即干净退出
    - 阶段 2 清尾按情形分三档（见下方实现）：
      干净退出的小输出 5ms | 未干净退出的小输出 50ms | ≥10K 的大输出 100ms

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
    # 把「确认命令未完成」与「发出 break」合成一个原子步骤。裸的二次确认
    # （`if exec_done.is_set(): return` 后紧跟 _set_break()）留有窗口：主线程可能
    # 恰在两句之间完成。晚到的 break 不会被任何代码消费，而是被**下一次**
    # StataSO_Execute 吃掉，表现为一条无关命令的 rc=1「已中断」。
    break_guard = threading.Lock()
    did_break = False

    def _timeout_watchdog():
        nonlocal did_break
        if exec_done.wait(timeout=timeout):
            return
        with break_guard:
            # 锁内二次确认：主线程置位 exec_done 同样要拿这把锁，于是「确认未
            # 完成」与「发出 break」之间不可能再插入命令的完成。
            if exec_done.is_set():
                return
            logger.warning("Stata command timed out (>%ss), issuing break: %s", timeout, cmd[:80])
            # 先置位再 break：主线程要拿到锁才能往下走，因此不会读到
            # 「break 已发出但 did_break 仍为 False」的中间态 —— 那会让它既不清
            # break 残渣，也不追加超时说明，调用方只看到一个通用 rc=1。
            did_break = True
            _set_break()

    watch = threading.Thread(target=_timeout_watchdog, daemon=True)
    watch.start()

    try:
        with stout.RedirectOutput(stout.StataDisplay(), stout.StataError(), stecho=False):
            encoded = config.get_encode_str(exec_cmd)
            rc = config.stlib.StataSO_Execute(encoded, False)
            # 命令一返回就立即置位，不能等到 RedirectOutput.__exit__ 与临时文件
            # 清理之后 —— 那段（多行块时含一次磁盘 unlink）足够让看门狗超时。
            with break_guard:
                exec_done.set()
    except Exception as e:
        logger.exception("StataSO_Execute crashed on: %s", cmd[:80])
        with break_guard:
            exec_done.set()
        return 999, f"StataSO_Execute 崩溃: {e}"
    finally:
        # include 已把文件读完，此处删除；放 finally 保证崩溃路径也不残留。
        _cleanup_temp_block(tmp_block)

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
        for index, block in enumerate(blocks):
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
                    # 首错即停 —— 与 Stata 自身的 do 文件语义一致。此前只有
                    # 997/998 会中止，而 r(601)/r(198) 与看门狗超时（break 后
                    # rc=1）都继续执行剩余块。这两类比 997 常见几个数量级，
                    # 且后果更重：`use` 失败后的 `collapse` 会在**上一个**内存
                    # 数据集上聚合，`save … , replace` 把错误数据落盘 —— 整体
                    # 虽标 isError，磁盘破坏已不可逆。
                    # 想继续执行的用户用 Stata 原生的 `capture`（它让 rc=0）。
                    remaining = len(blocks) - index - 1
                    if remaining > 0:
                        all_buf.write(
                            f"\n[已跳过后续 {remaining} 条命令]"
                            " 出错后中止，避免在陈旧/半截状态上继续执行。"
                            " 如需忽略该错误继续，请在该命令前加 Stata 的 capture 前缀。"
                        )
                    break
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
    - 拦截行首的危险前缀（``!``、``shell``、``winexec``、``python``、``mata``）

    **路径沙箱不覆盖本工具**：``STATA_ALLOWED_ROOTS`` 只校验其他工具的**路径
    参数**（如 ``stata_use_dataset(filepath=…)``）。本工具接受自由文本命令，
    其中的路径不做沙箱校验 —— 实测配置白名单后 ``stata_use_dataset`` 会拒绝
    越界路径，而 ``stata_run('use "越界路径"')`` 照常执行。
    这是刻意的边界而非遗漏：路径可能出现在 ``use`` / ``save`` / ``import`` /
    ``export`` / ``graph export`` / ``log using`` / ``merge … using`` /
    ``include`` 等任意位置，还能由宏在运行时拼出，做部分校验只会给出虚假的
    安全感。需要强制隔离时，请在操作系统层面限制本进程可访问的目录。

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


# 匹配 do 文件里的 `ssc install <pkg> [, options]`，允许 qui/cap/noi 前缀组合。
# install 只匹配全写（99% 写法），缩写未命中则退回原样内联执行（安全兜底）。
_SSC_INSTALL_RE = re.compile(
    r"^\s*(?:(?:qui(?:etly)?|cap(?:ture)?|noi(?:sily)?)\s+)*"
    r"ssc\s+install\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:,\s*(?P<opts>.*))?$",
    re.IGNORECASE,
)


def _extract_ssc_installs(do_text: str) -> tuple[str, list[tuple[str, bool]]]:
    """从 do 文本中拆出 `ssc install` 行，供执行前单独安装。

    返回 ``(cleaned_text, installs)``：
    - ``cleaned_text``：安装行**改成注释**（保留行号，便于错误定位），其余原样。
    - ``installs``：``[(package, replace)]``，按包名去重（保留首次出现的 replace 标志）。

    只处理**行首**的 `ssc install`（含 qui/cap/noi 前缀）；`{ }` 块或循环内的
    安装仍随块内联执行 —— 它是**有条件**执行的（``if _rc != 0 { ssc install foo }``
    是常见写法），提到脚本之前预装等于把它变成无条件安装，改变了 do 文件的语义。
    此前 docstring 声称如此，代码却无块跟踪；块深度即在此维护。
    ``install`` 只认全写。
    """
    installs: list[tuple[str, bool]] = []
    seen: set[str] = set()
    out_lines: list[str] = []
    brace_depth = 0
    for raw in do_text.split("\n"):
        m = _SSC_INSTALL_RE.match(raw) if brace_depth == 0 else None
        # 粗粒度的花括号计数：这里只需知道「是否在块内」，宁可保守 ——
        # 数错的后果是不拆分（退回内联执行，安全兜底），而非误拆。
        brace_depth = max(0, brace_depth + raw.count("{") - raw.count("}"))
        if m:
            pkg = m.group(1)
            opts = (m.group("opts") or "")
            replace = bool(re.search(r"\breplace\b", opts, re.IGNORECASE))
            if pkg not in seen:
                seen.add(pkg)
                installs.append((pkg, replace))
            out_lines.append(f"* [stata-mcp] 已移出单独安装: {raw.strip()}")
        else:
            out_lines.append(raw)
    return "\n".join(out_lines), installs


def _prepare_ssc_installs(installs: list[tuple[str, bool]], timeout: int) -> list[str]:
    """执行前逐个处理拆出的 ssc install：已装且无 replace 则跳过，缺失才装。

    每次安装走独立的联网调用（带 timeout，超时可被看门狗干净中断）。
    调用者不得持有 ``_stata_lock`` —— 本函数内部经 ``_run_stata_command`` 自行抢锁。

    Returns:
        面向用户的处理报告行列表。
    """
    report: list[str] = []
    for pkg, replace in installs:
        if not replace:
            # 锁内执行：Stata DLL 非线程安全，且 _execute_safe 会 drain 输出缓冲，
            # 不加锁会抢走并发命令的输出（与 estout 探测同处理）。锁在此 with
            # 结束时释放，下面的 _run_stata_command 自行重新抢锁 —— _stata_lock
            # 是不可重入的 threading.Lock，不能嵌套持有。
            with _stata_lock:
                probe_rc, _ = _execute_safe(f"which {pkg}", timeout=10)
            if probe_rc in (STATA_RC_RECOVERED, 998):
                # DLL 已死/刚恢复：探测结果不可信。当成「未安装」会对每个包各发
                # 一次联网 ssc install，把一个已经失败的会话拖更久。
                report.append(
                    f"  · {pkg}: 探测失败（Stata 无响应，返回码 {probe_rc}），已中止后续安装"
                )
                break
            if probe_rc in (0, STATA_RC_NO_OUTPUT):
                report.append(f"  · {pkg}: 已安装，跳过")
                continue
        replace_opt = ", replace" if replace else ""
        res = _run_stata_command(f"ssc install {pkg}{replace_opt}", timeout=timeout)
        ok = not isinstance(res, ToolResult)
        report.append(f"  · {pkg}: {'已安装' if ok else '安装失败（详见下方输出）'}")
        if not ok:
            report.append("    " + _result_text_inline(res))
    return report


def _result_text(value) -> str:
    """提取 str / ToolResult 的文本，保留换行。"""
    if isinstance(value, ToolResult):
        try:
            return value.content[0].text.strip()
        except (AttributeError, IndexError):
            return str(value)
    return str(value).strip()


def _result_text_inline(value) -> str:
    """提取 str / ToolResult 的文本并单行化，**仅**用于并入单行的报告条目。

    不要用它包装完整的命令输出：换行变 " | " 会把 Stata 的错误上下文、表格与
    行号提示压成一条巨型单行。需要保留格式时用 ``_result_text``。
    """
    return _result_text(value).replace("\n", " | ")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def stata_run_do_file(filepath: str, timeout: int = 300) -> str | ToolResult:
    """执行一个 Stata .do 文件并返回全部输出。

    .do 文件是 Stata 的批处理脚本。此工具会执行指定路径的 .do 文件。

    **执行前自动拆出 `ssc install`**：do 文件常在开头写 `ssc install foo`，内联
    执行会让整段脚本卡在网络请求上。本工具先扫描并把这些行移出：已安装的包直接
    跳过（不重复联网），缺失的包各自单独安装（带 timeout，超时可干净中断），
    然后运行**去掉安装行**的脚本主体（安装行改成注释，行号不变）。文件里没有
    `ssc install` 时，脚本原样执行、行为完全不变。

    注意：do 文件由 Stata 自行解析，**不经过** ``stata_run`` 的危险命令前缀
    护栏。只执行你信任的 do 文件。

    Args:
        filepath: .do 文件的绝对路径。
        timeout: 超时秒数（默认 300，范围 10–1800）。既是脚本主体的超时，也是
            每个拆出的包安装的超时。跑批量建模或大数据清洗时请显式调大。

    Returns:
        （若有）包安装报告 + do 文件执行的全部输出。
    """
    if err := _validate_path(filepath):
        return _result_or_error(err)
    safe_timeout = max(10, min(timeout, 1800))
    normalized = _normalize_path(filepath)

    # 前置拆分：读文件（best-effort）→ 拆出 ssc install → 单独预装 → 跑清理后脚本。
    # 读不到文件（如 Stata cwd 与 Python cwd 不一致的相对路径）时不做拆分，
    # 退回原样执行 —— 由下方 require_file 的锁内权威解析报「文件不存在」或正常运行。
    # UnicodeDecodeError 必须一并兜住：它继承自 ValueError 而非 OSError，
    # 而中文 Windows 的 Stata do 编辑器默认就不是 UTF-8（GBK/Big5 的 do 文件
    # 在本项目用户群体里很常见）。漏掉它会让 Python 栈异常穿透整个工具 ——
    # 而这类文件交给 Stata 自己执行本来完全正常。
    try:
        with open(normalized, encoding="utf-8") as f:
            do_text = f.read()
    except (OSError, UnicodeDecodeError):
        return _run_stata_command(
            f'do "{normalized}"', require_file=filepath, timeout=safe_timeout
        )

    cleaned, installs = _extract_ssc_installs(do_text)
    if not installs:
        # 无 ssc install：原样执行，走标准 require_file 权威路径，行为不变。
        return _run_stata_command(
            f'do "{normalized}"', require_file=filepath, timeout=safe_timeout
        )

    report = _prepare_ssc_installs(installs, timeout=safe_timeout)

    # 清理后的脚本写入 Stata 临时 do 文件执行（临时文件由 Stata 会话末清理，
    # 且 _cleanup 不适用于 do；此处即用即删）。
    try:
        tmpf = sfi.SFIToolkit.getTempFile()
        with open(tmpf, "w", encoding="utf-8") as f:
            f.write(cleaned if cleaned.endswith("\n") else cleaned + "\n")
    except OSError as e:
        return _make_error_result(f"错误: 无法写入清理后的临时 do 文件: {e}")

    try:
        result = _run_stata_command(f'do "{tmpf}"', timeout=safe_timeout)
    finally:
        _cleanup_temp_block(tmpf)

    header = "已在执行前处理 do 文件中的 ssc install：\n" + "\n".join(report) + "\n" + "-" * 40 + "\n"
    if isinstance(result, ToolResult):
        # 保留原始换行：_result_text_inline 是为并入安装**报告行**设计的，套在
        # 可达 120K 字符的 do 文件完整输出上会把 Stata 的错误上下文、表格、行号
        # 提示压成一条巨型单行 —— 同一个 do 文件只要不含 ssc install 就走原路径、
        # 格式完好，一行 ssc install 不该改变错误报告的可读性。
        return _make_error_result(header + _result_text(result))
    return header + result


# =============================================================================
# MCP 工具 — 数据管理 (destructiveHint=True)
# =============================================================================


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def stata_use_dataset(
    filepath: str,
    clear: bool = True,
    varlist: str = "",
    condition: str = "",
    in_range: str = "",
    options: str = "",
) -> str | ToolResult:
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

    Returns:
        数据集加载确认信息及变量列表。
    """
    if err := _validate_varlist(varlist, "varlist"):
        return _result_or_error(err)
    if err := _validate_filter_expr(condition, "condition"):
        return _result_or_error(err)
    if err := _validate_filter_expr(in_range, "in_range"):
        return _result_or_error(err)
    if err := _validate_no_injection(options, "options"):
        return _result_or_error(err)
    normalized = _normalize_path(filepath)
    # 指定 varlist 时官方语法要求写成 `use <varlist> using "file"`。
    if varlist.strip():
        cmd = f'use {varlist.strip()} using "{normalized}"'
    else:
        cmd = f'use "{normalized}"'
    cmd += _filter_clause(condition, in_range)
    opts = " ".join(p for p in ("clear" if clear else "", options.strip()) if p)
    if opts:
        cmd += f", {opts}"
    return _run_stata_command(cmd, require_file=filepath)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def stata_save_dataset(
    filepath: str, replace: bool = False, options: str = ""
) -> str | ToolResult:
    """将当前内存中的数据集保存为 .dta 文件。

    Args:
        filepath: 保存路径（建议使用 .dta 扩展名）。
        replace: 是否覆盖已有文件（默认 False）。

    Returns:
        保存确认信息。
    """
    if err := _validate_path(filepath):
        return _result_or_error(err)
    if err := _validate_no_injection(options, "options"):
        return _result_or_error(err)
    normalized = _normalize_path(filepath)
    opts = " ".join(p for p in ("replace" if replace else "", options.strip()) if p)
    suffix = f", {opts}" if opts else ""
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
    in_range: str = "",
    vartype: str = "",
    options: str = "",
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
    if err := _validate_filter_expr(condition, "condition"):
        return _result_or_error(err)
    if err := _validate_filter_expr(in_range, "in_range"):
        return _result_or_error(err)
    if err := _validate_storage_type(vartype):
        return _result_or_error(err)
    if err := _validate_no_injection(options, "options"):
        return _result_or_error(err)
    type_part = f"{vartype.strip()} " if vartype.strip() else ""
    cmd = f"generate {type_part}{newvar} = {expression.strip()}"
    cmd += _filter_clause(condition, in_range)
    if options.strip():
        cmd += f", {options.strip()}"
    return _run_stata_command(cmd)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def stata_egen(
    newvar: str,
    fcn: str,
    by: str = "",
    condition: str = "",
    in_range: str = "",
    vartype: str = "",
    options: str = "",
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
    if err := _validate_filter_expr(condition, "condition"):
        return _result_or_error(err)
    if err := _validate_filter_expr(in_range, "in_range"):
        return _result_or_error(err)
    if err := _validate_storage_type(vartype):
        return _result_or_error(err)
    if err := _validate_no_injection(options, "options"):
        return _result_or_error(err)
    prefix = f"bysort {by.strip()}: " if by.strip() else ""
    type_part = f"{vartype.strip()} " if vartype.strip() else ""
    cmd = f"{prefix}egen {type_part}{newvar} = {fcn.strip()}"
    cmd += _filter_clause(condition, in_range)
    if options.strip():
        cmd += f", {options.strip()}"
    return _run_stata_command(cmd)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def stata_predict(
    newvar: str,
    options: str = "",
    condition: str = "",
    in_range: str = "",
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
    if err := _validate_filter_expr(condition, "condition"):
        return _result_or_error(err)
    if err := _validate_filter_expr(in_range, "in_range"):
        return _result_or_error(err)
    cmd = f"predict {newvar}"
    cmd += _filter_clause(condition, in_range)
    if options.strip():
        cmd += f", {options.strip()}"
    return _run_stata_command(cmd)


# =============================================================================
# MCP 工具 — 数据探索 (readOnlyHint=True)
# =============================================================================


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_describe(
    varlist: str = "", simple: bool = False, options: str = ""
) -> str | ToolResult:
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
    if err := _validate_no_injection(options, "options"):
        return _result_or_error(err)
    cmd = f"describe {varlist}".strip()
    opts = " ".join(p for p in ("simple" if simple else "", options.strip()) if p)
    if opts:
        cmd += f", {opts}"
    return _run_stata_command(cmd)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_summarize(
    varlist: str = "",
    detail: bool = False,
    condition: str = "",
    in_range: str = "",
    options: str = "",
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
    if err := _validate_filter_expr(condition, "condition"):
        return _result_or_error(err)
    if err := _validate_filter_expr(in_range, "in_range"):
        return _result_or_error(err)
    if err := _validate_no_injection(options, "options"):
        return _result_or_error(err)
    cmd = f"summarize {varlist}".strip()
    cmd += _filter_clause(condition, in_range)
    opts = " ".join(p for p in ("detail" if detail else "", options.strip()) if p)
    if opts:
        cmd += f", {opts}"
    return _run_stata_command(cmd)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_list(
    varlist: str = "",
    n: int = 10,
    in_range: str = "",
    condition: str = "",
    options: str = "",
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
        options: 额外的官方选项，如 "noobs clean"、"separator(0)"、"abbreviate(12)"。

    Returns:
        数据表格。
    """
    if err := _validate_varlist(varlist, "varlist"):
        return _result_or_error(err)
    if err := _validate_filter_expr(condition, "condition"):
        return _result_or_error(err)
    if err := _validate_filter_expr(in_range, "in_range"):
        return _result_or_error(err)
    if err := _validate_no_injection(options, "options"):
        return _result_or_error(err)
    if n < 0:
        return _make_error_result("错误: n 不能为负数")
    cmd = "list"
    if varlist.strip():
        cmd += f" {varlist}"
    # in 子句由下面的 in_range/n 逻辑负责，故这里只交 condition 给 _filter_clause，
    # 否则会拼出 `list … in 1/20 in 1/20`。
    cmd += _filter_clause(condition, "")
    if in_range.strip():
        cmd += f" in {in_range.strip()}"
    elif n > 0:
        cmd += f" in 1/{n}"
    if options.strip():
        cmd += f", {options.strip()}"
    return _run_stata_command(cmd)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_codebook(
    varlist: str = "",
    compact: bool = False,
    condition: str = "",
    in_range: str = "",
    options: str = "",
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
    if err := _validate_filter_expr(condition, "condition"):
        return _result_or_error(err)
    if err := _validate_filter_expr(in_range, "in_range"):
        return _result_or_error(err)
    if err := _validate_no_injection(options, "options"):
        return _result_or_error(err)
    cmd = f"codebook {varlist}".strip()
    cmd += _filter_clause(condition, in_range)
    opts = " ".join(p for p in ("compact" if compact else "", options.strip()) if p)
    if opts:
        cmd += f", {opts}"
    return _run_stata_command(cmd)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_tabulate(
    varname: str,
    byvar: str = "",
    chi2: bool = False,
    condition: str = "",
    in_range: str = "",
    options: str = "",
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
    if err := _validate_filter_expr(condition, "condition"):
        return _result_or_error(err)
    if err := _validate_filter_expr(in_range, "in_range"):
        return _result_or_error(err)
    if err := _validate_no_injection(options, "options"):
        return _result_or_error(err)
    cmd = f"tabulate {varname}"
    if byvar.strip():
        cmd += f" {byvar}"
    cmd += _filter_clause(condition, in_range)
    # chi2 是 twoway 专属选项，单变量表传了会 r(198)
    opts = " ".join(
        p for p in ("chi2" if (byvar.strip() and chi2) else "", options.strip()) if p
    )
    if opts:
        cmd += f", {opts}"
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
    in_range: str = "",
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
    if err := _validate_filter_expr(condition, "condition"):
        return _result_or_error(err)
    if err := _validate_filter_expr(in_range, "in_range"):
        return _result_or_error(err)
    if err := _validate_no_injection(options, "options"):
        return _result_or_error(err)
    cmd = f"regress {depvar} {indepvars}"
    cmd += _filter_clause(condition, in_range)
    if options.strip():
        cmd += f", {options}"
    return _run_stata_command(cmd)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_logistic(
    depvar: str,
    indepvars: str,
    options: str = "",
    condition: str = "",
    in_range: str = "",
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
    if err := _validate_filter_expr(condition, "condition"):
        return _result_or_error(err)
    if err := _validate_filter_expr(in_range, "in_range"):
        return _result_or_error(err)
    if err := _validate_no_injection(options, "options"):
        return _result_or_error(err)
    cmd = f"logistic {depvar} {indepvars}"
    cmd += _filter_clause(condition, in_range)
    if options.strip():
        cmd += f", {options}"
    return _run_stata_command(cmd)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_ttest(
    varname: str,
    byvar: str = "",
    compare_to: str = "",
    options: str = "",
    condition: str = "",
    in_range: str = "",
) -> str | ToolResult:
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
    if err := _validate_identifier(varname, "varname", required=True):
        return _result_or_error(err)
    if err := _validate_identifier(byvar, "byvar"):
        return _result_or_error(err)
    if err := _validate_no_injection(compare_to, "compare_to"):
        return _result_or_error(err)
    if err := _validate_filter_expr(condition, "condition"):
        return _result_or_error(err)
    if err := _validate_filter_expr(in_range, "in_range"):
        return _result_or_error(err)
    if err := _validate_no_injection(options, "options"):
        return _result_or_error(err)

    if byvar.strip() and compare_to.strip():
        return _make_error_result(
            "错误: byvar 与 compare_to 互斥 —— 前者是按组两样本检验"
            "（ttest v, by(g)），后者是单样本/配对检验（ttest v == x）。"
        )
    if not byvar.strip() and not compare_to.strip():
        # 裸 `ttest v` 会 r(100)；与其把非法命令发给 Stata，不如说明该给什么。
        return _make_error_result(
            "错误: 必须给出 byvar 或 compare_to 之一（裸 `ttest 变量` 不是合法命令）。\n"
            '  · 单样本检验均值是否等于某值 → compare_to="5000"\n'
            '  · 按组比较两样本         → byvar="foreign"\n'
            '  · 配对/非配对比较两变量   → compare_to="另一变量"'
            "（非配对再加 options=\"unpaired\"）"
        )

    lhs = f"{varname} == {compare_to.strip()}" if compare_to.strip() else varname
    cmd = f"ttest {lhs}"
    cmd += _filter_clause(condition, in_range)
    opts = " ".join(
        p for p in (f"by({byvar.strip()})" if byvar.strip() else "", options.strip()) if p
    )
    if opts:
        cmd += f", {opts}"
    return _run_stata_command(cmd)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_probit(
    depvar: str,
    indepvars: str,
    marginal_effects: bool = False,
    options: str = "",
    condition: str = "",
    in_range: str = "",
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
    if err := _validate_filter_expr(condition, "condition"):
        return _result_or_error(err)
    if err := _validate_filter_expr(in_range, "in_range"):
        return _result_or_error(err)
    if err := _validate_no_injection(options, "options"):
        return _result_or_error(err)
    cmd = f"probit {depvar} {indepvars}"
    cmd += _filter_clause(condition, in_range)
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
    in_range: str = "",
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
    if err := _validate_filter_expr(condition, "condition"):
        return _result_or_error(err)
    if err := _validate_filter_expr(in_range, "in_range"):
        return _result_or_error(err)
    if err := _validate_no_injection(options, "options"):
        return _result_or_error(err)
    cmd = f"poisson {depvar} {indepvars}"
    cmd += _filter_clause(condition, in_range)
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
    in_range: str = "",
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
    if err := _validate_filter_expr(condition, "condition"):
        return _result_or_error(err)
    if err := _validate_filter_expr(in_range, "in_range"):
        return _result_or_error(err)
    if err := _validate_no_injection(options, "options"):
        return _result_or_error(err)
    eff = effects.strip().lower()
    if eff not in _XTREG_EFFECTS:
        return _make_error_result(
            f"错误: effects 只能是 {', '.join(sorted(_XTREG_EFFECTS))} 之一，收到 '{effects}'"
        )
    cmd = f"xtreg {depvar} {indepvars}"
    cmd += _filter_clause(condition, in_range)
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
    in_range: str = "",
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
    if err := _validate_filter_expr(condition, "condition"):
        return _result_or_error(err)
    if err := _validate_filter_expr(in_range, "in_range"):
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
    cmd += _filter_clause(condition, in_range)
    if options.strip():
        cmd += f", {options}"
    return _run_stata_command(cmd)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_correlate(
    varlist: str = "",
    pairwise: bool = False,
    options: str = "",
    condition: str = "",
    in_range: str = "",
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
    if err := _validate_filter_expr(condition, "condition"):
        return _result_or_error(err)
    if err := _validate_filter_expr(in_range, "in_range"):
        return _result_or_error(err)
    if err := _validate_no_injection(options, "options"):
        return _result_or_error(err)
    base = "pwcorr" if pairwise else "correlate"
    cmd = base
    if varlist.strip():
        cmd += f" {varlist}"
    cmd += _filter_clause(condition, in_range)
    if options.strip():
        cmd += f", {options}"
    return _run_stata_command(cmd)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_margins(
    marginlist: str = "",
    dydx: str = "",
    at: str = "",
    options: str = "",
    condition: str = "",
    in_range: str = "",
) -> str | ToolResult:
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
    if err := _validate_filter_expr(condition, "condition"):
        return _result_or_error(err)
    if err := _validate_filter_expr(in_range, "in_range"):
        return _result_or_error(err)
    cmd = "margins"
    if marginlist.strip():
        cmd += f" {marginlist.strip()}"
    cmd += _filter_clause(condition, in_range)
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
def stata_test(spec: str, options: str = "") -> str | ToolResult:
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

    Returns:
        Wald 检验结果（F 或 chi2 统计量与 p 值）。
    """
    if not spec.strip():
        return _make_error_result("错误: 请提供检验设定，如 'weight mpg' 或 'weight = mpg'")
    if err := _validate_no_injection(spec, "spec"):
        return _result_or_error(err)
    if err := _validate_no_injection(options, "options"):
        return _result_or_error(err)
    cmd = f"test {spec.strip()}"
    if options.strip():
        cmd += f", {options.strip()}"
    return _run_stata_command(cmd)


# =============================================================================
# MCP 工具 — 图形 (readOnlyHint=True)
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
    scheme: str = "",
    export: str = "",
    width: int = 800,
    height: int = 0,
    replace: bool = False,
    quality: int = 0,
    mag: int = 0,
    fontface: str = "",
) -> str | ToolResult:
    """生成 Stata 图形并可选导出为文件。

    当指定 export 时,使用 { } 复合块将 graph + export 包装在单次
    StataSO_Execute 调用中,避免图形窗口在 headless 环境中丢失。

    **不适用于目标格式的选项会被丢弃并在返回信息中说明** —— 传给 Stata 会
    r(198)，而复合块的 capture 会吞掉错误，表现为导出无声失败。

    官方支持的后缀（[G-2] graph export）：ps eps svg emf pdf png tif gif jpg。
    可用性依运行环境而变：emf 仅 Windows、gif 仅 Mac GUI、tif 不支持 console 模式；
    本 MCP 以 headless console 运行，实测仅 png/jpg/pdf/svg/eps/ps 可用。

    Args:
        command: 图形命令(scatter mpg weight, histogram price 等)。
        scheme: 图形方案。**留空（默认）= 不改变当前 scheme**，沿用 Stata 或用户
                的设置（Stata 19 默认为 stcolor）。传值才会 `set scheme`，且不会
                在调用后还原。用 stata_scheme() 可列出全部可用方案。
        export: 导出图形文件路径（留空不导出）；Stata 按扩展名推断格式。
        width: 导出宽度（默认 800）。单位随格式而变：.png/.jpg/.tif/.gif 与 .svg
               是**像素**（位图 8–16000）；.pdf 是**英寸**（0.5–20）；
               .eps/.ps/.emf/.wmf **不支持**尺寸选项。
        height: 导出高度，单位同 width（默认 0 表示不指定）。
        replace: 是否覆盖已有文件(默认 False)。
        quality: JPEG 压缩质量 1–100（默认 0 = 不指定，Stata 默认 90）。**仅 .jpg**。
        mag: 缩放百分比 1–10000（默认 0 = 不指定，Stata 默认 100）。
             **仅 .pdf/.eps/.ps**。
        fontface: 默认字体名（默认空 = 不指定）。**仅 .pdf/.eps/.ps/.svg**。

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
        if scheme and (err := _validate_scheme_name(scheme)):
            return _result_or_error(err)
        if fontface and (err := _validate_fontface(fontface)):
            return _result_or_error(err)
        # 负值会被原样拼成 width(-100) 交给 Stata；实测虽因图形命令先失败而未暴露，
        # 但语义上无意义，应在入口拒绝而不是依赖下游偶然报错。
        for label, value in (
            ("width", width),
            ("height", height),
            ("quality", quality),
            ("mag", mag),
        ):
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

        # scheme 留空时不发 `set scheme` —— 那会把用户当前的主题（Stata 19 默认
        # 是 stcolor）悄悄改掉且不还原，是覆盖而非设定。
        scheme_line = f"set scheme {scheme}\n" if scheme else ""

        if not export:
            return _run_stata_command(f"{scheme_line}{command}", timeout=120)

        # 导出模式：使用 { } 复合块确保 graph + export 原子执行
        export_path = _normalize_path(export)
        replace_opt = "replace" if replace else ""
        size_opts, size_note = _graph_size_options(export_path, width, height)
        fmt_opts, fmt_note = _graph_format_options(export_path, quality, mag, fontface)
        export_opts = " ".join(p for p in (replace_opt, size_opts, fmt_opts) if p)

        # 复合块内的错误被 capture 吞掉（rc 恒为 0），无法据此判断成败；
        # 改以「文件是否被这次调用新写入」为准，故先记录调用前的状态。
        # 只看文件存在与否不够：replace=False 且目标已存在时 Stata 会拒绝写入，
        # 而文件依旧在，会被误判成功。
        before_ns = _mtime_ns(export_path) if os.path.isfile(export_path) else None

        compound = (
            f"capture noisily {{\n"
            f"    set graphics off\n"
            f"{'    ' + scheme_line if scheme_line else ''}"
            f"    {command}\n"
            f'    graph export "{export_path}", {export_opts}\n'
            f"}}\n"
            # 只清匿名图，不能 `graph drop _all`：具名图正是「我要在后续命令里
            # 引用它」的显式表达，而 _all 会把它们一起摧毁 —— combine 出一张图
            # 导出后，再换个布局导出第二张就会发现源图已经没了。
            # 真机确认（Stata 19.5 MP）：匿名图名为 `Graph`（`graph combine` 的
            # 结果同样叫 `Graph`），`graph drop Graph` 只删它、具名图存活。
            # 匿名图不会累积 —— 每次绘图都覆盖同名的那一个。
            f"capture noisily graph drop Graph"
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
        for note in (size_note, fmt_note):
            if note:
                result += f"\n{note}"
        return result

    except Exception as e:
        return _make_error_result(f"图形生成失败: {type(e).__name__}: {e}")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))
def stata_scheme(
    action: str = "list", scheme: str = "", permanently: bool = False
) -> str | ToolResult:
    """查询或设置 Stata 图形主题（scheme）。

    scheme 决定配色、字体、坐标轴与图例的整体外观。Stata 19 的默认是 ``stcolor``
    （实测 ``c(scheme)``），本机内置 26 个方案；``ssc install`` 的第三方方案
    （cleanplots、plottig、schemepack 等）装好后也会出现在列表里。

    Args:
        action: ``list``（默认，列出全部可用方案）/ ``get``（当前方案）/
                ``set``（切换方案）。
        scheme: 方案名，仅 action="set" 时必填。
        permanently: 是否写入 Stata 配置、跨会话保留（默认 False，仅本会话生效）。

    Returns:
        方案清单、当前方案名，或设置确认。
    """
    if action not in ("list", "get", "set"):
        return _make_error_result(
            f'错误: action 只能是 "list" / "get" / "set"（收到 {action!r}）'
        )

    if action == "list":
        # 官方查询命令；ssc 没有对应子命令，`graph query, schemes` 是唯一入口。
        return _run_stata_command("graph query, schemes")

    if action == "get":
        # 不能用裸 `set scheme` 查询 —— 那是设置命令，不带参数时行为不同。
        return _run_stata_command("display c(scheme)")

    if not scheme.strip():
        # 空值会拼出裸 `set scheme`，改变命令语义而非报错。
        return _make_error_result('错误: action="set" 时必须提供 scheme 名')
    if err := _validate_scheme_name(scheme):
        return _result_or_error(err)

    suffix = ", permanently" if permanently else ""
    return _run_stata_command(f"set scheme {scheme.strip()}{suffix}")


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
    sheet_mode: str = "",
    cell: str = "",
    firstrow: str = "variables",
    nolabel: bool = False,
    condition: str = "",
    in_range: str = "",
    options: str = "",
) -> str | ToolResult:
    """将当前数据集导出为 Excel (.xlsx/.xls) 文件，或将回归结果导出为 CSV。

    使用 Stata 的 export excel 命令导出数据。
    当 results=True 时，使用 esttab 导出回归结果表；esttab 不支持 xlsx
    与 sheet() 选项，因此强制输出为 CSV（如原路径为 .xlsx，会自动改
    为 .csv 并提示）。

    Args:
        filepath: 导出路径（数据导出建议 .xlsx；回归结果导出会改为 .csv）。
        varlist: 要导出的变量列表（空格分隔），留空 = 全部变量。
                 仅用于数据导出；results=True 时 esttab 按已存储的估计结果出表，
                 该参数会被忽略。
        sheet: Excel 工作表名（默认 "Sheet1"，仅用于数据导出）。
        replace: 是否覆盖已有**文件**（默认 False）。
        results: 若为 True，将当前存储的回归结果导出为 CSV 表格而非原始数据。
        sheet_mode: 目标**工作表**已存在时的处理 —— "modify"（保留其他表，改写本表）
                    或 "replace"（清空本表重写）。留空则沿用 Stata 默认：工作表
                    已存在时报 r(602)。注意这与 ``replace``（针对整个文件）不同。
        cell: 起始单元格（左上角），如 "B3"。留空 = 从 A1 开始。
        firstrow: 首行内容 —— "variables"（默认，变量名）、"varlabels"（变量标签）
                  或 "none"（不写首行）。
        nolabel: 导出数值本身而非值标签（默认 False）。
        condition: if 条件子句（可选），如 "foreign == 1"。
        in_range: 观测范围（可选），如 "1/100"。
        options: 其余官方选项的自由文本逃生舱，如
                 ``'keepcellfmt missing("NA") datestring("%td")'``。

    Returns:
        导出确认信息。
    """
    if err := _validate_path(filepath):
        return _result_or_error(err)
    if err := _validate_varlist(varlist, "varlist"):
        return _result_or_error(err)
    if err := _validate_sheet_name(sheet):
        return _result_or_error(err)
    if err := _validate_filter_expr(condition, "condition"):
        return _result_or_error(err)
    if err := _validate_filter_expr(in_range, "in_range"):
        return _result_or_error(err)
    if err := _validate_no_injection(options, "options"):
        return _result_or_error(err)
    if err := _validate_no_injection(cell, "cell"):
        return _result_or_error(err)
    if sheet_mode and sheet_mode not in ("modify", "replace"):
        return _make_error_result(
            f'错误: sheet_mode 只能是 "modify" 或 "replace"（收到 {sheet_mode!r}）'
        )
    if sheet_mode and replace and not results:
        # 实测 Stata：invalid syntax; option sheet(...,replace) may not be combined
        # with option replace。二者语义冲突 —— 文件级 replace 重建整个文件，
        # 不可能有工作表冲突；sheet_mode 则是针对已存在文件里的某张表。
        return _make_error_result(
            "错误: sheet_mode 与 replace 不能同时使用（Stata 会 r(198)）。\n"
            "  · 想重建整个文件 → 只传 replace=True\n"
            f'  · 想保留文件、改写其中一张表 → 只传 sheet_mode="{sheet_mode}"'
        )
    if firstrow not in ("variables", "varlabels", "none"):
        return _make_error_result(
            f'错误: firstrow 只能是 "variables" / "varlabels" / "none"（收到 {firstrow!r}）'
        )

    export_path = _normalize_path(filepath)
    replace_opt = "replace" if replace else ""
    firstrow_opt = "" if firstrow == "none" else f"firstrow({firstrow})"

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
        # stata_install_package("estout") 手动安装。不在此内嵌 ssc install ——
        # 但原因不是「损坏 DLL」：那条结论已被实测推翻（Stata 19.5 MP，多场景
        # 复现无一崩溃，超时也能被 SetBreak 干净中断、包不残留半装状态）。
        # 真正的问题是它整段独占 _stata_lock：同一个包耗时在 3–13 秒间波动，
        # 慢网络更久，内嵌进分析步骤会意外冻结整个 server。故包安装独立成工具，
        # 由用户控制时机、timeout 参数真实兜底。
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
        # 导出数据集为 Excel。[if] [in] 属于命令的另一个语法位置，必须在逗号之前。
        sheet_opt = f'sheet("{sheet}", {sheet_mode})' if sheet_mode else f'sheet("{sheet}")'
        opts = " ".join(
            p
            for p in (
                replace_opt,
                firstrow_opt,
                sheet_opt,
                f"cell({cell.strip()})" if cell.strip() else "",
                "nolabel" if nolabel else "",
                options.strip(),
            )
            if p
        )
        cmd = "export excel"
        if varlist.strip():
            cmd += f" {varlist.strip()}"
        cmd += f' using "{export_path}"'
        cmd += _filter_clause(condition, in_range)
        cmd += f", {opts}"

    # 导出成败以「文件是否被这次调用写入」为准，不能只看文件是否存在：
    # 上次运行留下的同名文件会把失败伪装成成功。实测 rc=997（崩溃已恢复、
    # 命令未执行）时，旧文件仍在，原实现回报「已导出 28 B」。
    before_ns = _mtime_ns(export_path) if os.path.isfile(export_path) else None

    result = _run_stata_command(cmd, timeout=120)

    # 若 _run_stata_command 已标记错误，透传；空选择这类误导性诊断补一句解释。
    if isinstance(result, ToolResult):
        raw = result.content[0].text if result.content else ""
        if hint := _empty_selection_hint(raw, condition, in_range):
            return _make_error_result(raw + hint)
        return result

    if not _file_written_since(export_path, before_ns):
        hint = ""
        if before_ns is not None and not replace:
            hint = "\n提示：目标文件已存在且 replace=False，如需覆盖请传 replace=True。"
        return _make_error_result(
            f"错误: 导出失败，未写入文件 {export_path}{hint}\n{changed_msg}{result.strip()}"
        )

    return f"{changed_msg}已导出 {_format_size(export_path)} -> {export_path}\n{result}"


# etable 支持的导出格式，逐一在 Stata 19.5 MP 上实测过：
# .csv 与 .rtf 报 r(198) 且不产出文件，其余九种均 rc=0 且文件真实写出。
# 必须在入口拦下不支持的格式 —— etable 会先把表格正常打印出来再报错，
# r(198) 淹没在表格输出里，用户很容易以为导出成功了。
_ETABLE_EXPORT_EXTS = frozenset(
    {".docx", ".xlsx", ".xls", ".html", ".pdf", ".tex", ".md", ".txt", ".smcl"}
)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def stata_etable(
    estimates: str = "",
    export: str = "",
    replace: bool = False,
    stars: bool = False,
    stats: str = "",
    title: str = "",
    options: str = "",
) -> str | ToolResult:
    """生成回归结果表并可导出（官方 ``etable``，Stata 17+）。

    这是「把回归表交出去」的官方路径：**不需要任何第三方包**，且能直接产出
    Word/Excel/PDF/LaTeX。对照 ``stata_export_excel(results=True)`` —— 那条路
    依赖第三方 ``estout``，且只能产出 CSV。

    典型用法：跑完多个模型各自 ``stata_estimates(action="store", name="m1")``，
    再用 ``estimates="m1 m2 m3"`` 并排成表导出。不传 ``estimates`` 时用当前
    活跃的估计结果。

    Args:
        estimates: 已存储的估计结果名（空格分隔）。留空则用当前活跃估计。
        export: 导出路径。支持 .docx / .xlsx / .xls / .html / .pdf / .tex /
            .md / .txt / .smcl（实测 .csv 与 .rtf 会 r(198)）。留空只打印。
        replace: 覆盖已存在的文件（默认 False）。
        stars: 显示显著性星号并附星号说明（``showstars showstarsnote``）。
        stats: 附加的模型统计量，空格分隔，如 "N r2 r2_a aic"
            （官方语法是每个各写一个 ``mstat()``，此处自动展开）。
        title: 表标题。
        options: 其余官方选项，如 "column(dvlabel)"、"cstat(_r_b, nformat(%9.3f))"。

    Returns:
        表格文本；导出时附确认信息。
    """
    if err := _validate_varlist(estimates, "estimates"):
        return _result_or_error(err)
    if err := _validate_varlist(stats, "stats"):
        return _result_or_error(err)
    if err := _validate_sheet_name(title):
        return _result_or_error(err.replace("工作表名", "title"))
    if err := _validate_no_injection(options, "options"):
        return _result_or_error(err)

    export_path = ""
    if export:
        if err := _validate_path(export):
            return _result_or_error(err)
        ext = os.path.splitext(export)[1].lower()
        if ext not in _ETABLE_EXPORT_EXTS:
            return _make_error_result(
                f"错误: etable 不支持导出为 {ext or '<无扩展名>'}"
                f"（实测 .csv 与 .rtf 会 r(198)）。可用: "
                f"{', '.join(sorted(_ETABLE_EXPORT_EXTS))}"
            )
        export_path = _normalize_path(export)

    opts = []
    if estimates.strip():
        opts.append(f"estimates({estimates.strip()})")
    if stars:
        opts.append("showstars showstarsnote")
    # 官方语法是每个统计量各写一个 mstat()，不是 mstat(N r2)
    opts.extend(f"mstat({s})" for s in stats.split())
    if title.strip():
        opts.append(f'title("{title.strip()}")')
    if export_path:
        replace_opt = ", replace" if replace else ""
        opts.append(f'export("{export_path}"{replace_opt})')
    if options.strip():
        opts.append(options.strip())

    cmd = "etable" + (f", {' '.join(opts)}" if opts else "")

    before_ns = (
        _mtime_ns(export_path) if export_path and os.path.isfile(export_path) else None
    )
    result = _run_stata_command(cmd)
    if isinstance(result, ToolResult) or not export_path:
        return result

    # 以文件是否被本次调用写入为准：etable 会先打印表格再报导出错误，
    # 只看输出很容易把失败当成功（与 stata_graph 同一判定思路）。
    if not _file_written_since(export_path, before_ns):
        hint = "" if replace else "\n提示：目标文件已存在时需传 replace=True。"
        return _make_error_result(f"错误: 表格未能写入 {export_path}{hint}\n{result}")
    return f"已导出 {_format_size(export_path)} -> {export_path}\n{result}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def stata_export_delimited(
    filepath: str,
    varlist: str = "",
    delimiter: str = "",
    novarnames: bool = False,
    nolabel: bool = False,
    datafmt: bool = False,
    quote: bool = False,
    replace: bool = False,
    condition: str = "",
    in_range: str = "",
    options: str = "",
) -> str | ToolResult:
    """将当前数据集导出为分隔文本文件（CSV / TSV / 自定义分隔符）。

    对应官方的 ``export delimited``。相比 Excel，它无依赖、体积小、任何工具都能读，
    是跨程序交换数据的首选；文件名不带扩展名时 Stata 默认按 ``.csv`` 处理。

    Args:
        filepath: 导出路径（如 "out/data.csv"）。
        varlist: 要导出的变量列表（空格分隔），留空 = 全部变量。
        delimiter: 分隔符 —— 留空 = 逗号（Stata 默认）；``"tab"`` 用制表符；
                   或单个字符如 ``";"``、``"|"``。
        novarnames: 不写变量名首行（默认 False，即写）。
        nolabel: 导出数值本身而非值标签（默认 False）。
        datafmt: 按变量的显示格式导出（默认 False）。
        quote: 字符串一律用双引号包裹（默认 False，仅必要时包裹）。
        replace: 是否覆盖已有文件（默认 False）。
        condition: if 条件子句（可选）。
        in_range: 观测范围（可选），如 "1/100"。
        options: 其余官方选项的自由文本逃生舱。

    Returns:
        导出确认信息。
    """
    if err := _validate_path(filepath):
        return _result_or_error(err)
    if err := _validate_varlist(varlist, "varlist"):
        return _result_or_error(err)
    for value, label in (
        (condition, "condition"),
        (in_range, "in_range"),
        (options, "options"),
    ):
        if err := _validate_no_injection(value, label):
            return _result_or_error(err)

    # delimiter 拼进 delimiter("<c>")，双引号会提前闭合；tab 是关键字不加引号。
    # 实测 Stata 对 delimiter("tab") 与 delimiter(tab) 一视同仁（都产出制表符，
    # 不会把 "tab" 当三字符分隔符），此处取官方文档的无引号写法。
    delim_opt = ""
    if delimiter:
        if delimiter == "tab":
            delim_opt = "delimiter(tab)"
        elif len(delimiter) != 1:
            return _make_error_result(
                f'错误: delimiter 只能是单个字符或关键字 "tab"（收到 {delimiter!r}）'
            )
        elif delimiter in ('"', "`", "$", "\\"):
            return _make_error_result(f"错误: delimiter 不能是 {delimiter!r}")
        else:
            delim_opt = f'delimiter("{delimiter}")'

    export_path = _normalize_path(filepath)
    opts = " ".join(
        p
        for p in (
            delim_opt,
            "novarnames" if novarnames else "",
            "nolabel" if nolabel else "",
            "datafmt" if datafmt else "",
            "quote" if quote else "",
            "replace" if replace else "",
            options.strip(),
        )
        if p
    )

    cmd = "export delimited"
    if varlist.strip():
        cmd += f" {varlist.strip()}"
    cmd += f' using "{export_path}"'
    cmd += _filter_clause(condition, in_range)
    if opts:
        cmd += f", {opts}"

    # 与 stata_graph / stata_export_excel 一致：以文件是否被本次调用写入为准。
    # 只判断「文件存在」会把上次留下的同名文件当成本次成功。
    before_ns = _mtime_ns(export_path) if os.path.isfile(export_path) else None

    result = _run_stata_command(cmd, timeout=120)
    if isinstance(result, ToolResult):
        raw = result.content[0].text if result.content else ""
        if hint := _empty_selection_hint(raw, condition, in_range):
            return _make_error_result(raw + hint)
        return result

    if not _file_written_since(export_path, before_ns):
        hint = ""
        if before_ns is not None and not replace:
            hint = "\n提示：目标文件已存在且 replace=False，如需覆盖请传 replace=True。"
        return _make_error_result(
            f"错误: 导出失败，未写入文件 {export_path}{hint}\n{result.strip()}"
        )

    return f"已导出 {_format_size(export_path)} -> {export_path}\n{result}"


_FRAME_ACTIONS = frozenset(
    {"dir", "current", "create", "change", "drop", "copy", "rename"}
)
_FRAME_NEED_NAME = frozenset({"create", "change", "drop", "copy", "rename"})
_FRAME_NEED_NEWNAME = frozenset({"copy", "rename"})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def stata_frame(
    action: str = "dir", name: str = "", newname: str = ""
) -> str | ToolResult:
    """管理数据 frame —— 在内存中同时持有多个数据集（Stata 16+）。

    合并前把两份数据各放一个 frame、或一边建模一边保留原始数据时用得上。
    当前有哪些 frame 也可从 ``stata_status`` 看到。

    Args:
        action: ``dir``（默认，列出全部）/ ``current``（当前 frame 名）/
            ``create`` / ``change`` / ``drop``（均需 name）/
            ``copy`` / ``rename``（需 name 与 newname）。
        name: 目标 frame 名。
        newname: copy / rename 的新名字。

    Returns:
        frame 清单或操作确认。
    """
    if action not in _FRAME_ACTIONS:
        return _make_error_result(
            f"错误: action 只能是 {sorted(_FRAME_ACTIONS)}（收到 {action!r}）"
        )
    if err := _validate_identifier(name, "name"):
        return _result_or_error(err)
    if err := _validate_identifier(newname, "newname"):
        return _result_or_error(err)
    if action in _FRAME_NEED_NAME and not name.strip():
        return _make_error_result(f'错误: action="{action}" 必须提供 name')
    if action in _FRAME_NEED_NEWNAME and not newname.strip():
        return _make_error_result(f'错误: action="{action}" 必须提供 newname')

    if action == "dir":
        return _run_stata_command("frames dir")
    if action == "current":
        # `frame pwf` = print working frame，与 c(frame) 等价但更自解释。
        return _run_stata_command("frame pwf")
    if action in _FRAME_NEED_NEWNAME:
        return _run_stata_command(f"frame {action} {name.strip()} {newname.strip()}")
    return _run_stata_command(f"frame {action} {name.strip()}")


# 数据校验：各自是独立命令，但都在回答「数据对不对」，故合成一个工具。
_VERIFY_CHECKS = frozenset({"count", "assert", "duplicates", "isid", "missing"})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_verify(
    check: str = "count",
    varlist: str = "",
    expression: str = "",
    condition: str = "",
    in_range: str = "",
    options: str = "",
) -> str | ToolResult:
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

    Returns:
        检查结果；``assert`` 不成立时以错误结果返回。
    """
    if check not in _VERIFY_CHECKS:
        return _make_error_result(
            f"错误: check 只能是 {sorted(_VERIFY_CHECKS)}（收到 {check!r}）"
        )
    if err := _validate_varlist(varlist, "varlist"):
        return _result_or_error(err)
    for value, label in ((expression, "expression"), (condition, "condition"),
                         (in_range, "in_range"), (options, "options")):
        if err := _validate_no_injection(value, label):
            return _result_or_error(err)

    if check == "count":
        return _run_stata_command("count" + _filter_clause(condition, in_range))

    if check == "assert":
        if not expression.strip():
            return _make_error_result('错误: check="assert" 必须提供 expression')
        cmd = f"assert {expression.strip()}" + _filter_clause(condition, in_range)
        if options.strip():
            cmd += f", {options.strip()}"
        return _run_stata_command(cmd)

    if check == "duplicates":
        # duplicates 的第一个词是子命令而非选项，故从 options 取，缺省 report。
        sub = options.strip() or "report"
        # 本工具标 readOnlyHint=True，而 `drop` 删除观测、`tag()` 创建变量 ——
        # 都是「修改」而非「校验」。遵循 MCP 注解的客户端会对只读工具跳过确认，
        # 放行等于静默改数据。挡在门外，比给一个「除非传某个选项否则只读」的
        # 工具更安全；真要改数据走 stata_run，那里的注解是诚实的。
        if re.match(r"^(drop|tag)\b", sub, re.IGNORECASE):
            return _make_error_result(
                f"错误: stata_verify 是只读工具，不执行会修改数据的 `duplicates {sub}`"
                "（drop 删除观测、tag() 创建变量）。"
                f'请改用 stata_run("duplicates {sub}")，那里会按非只读工具处理。'
            )
        cmd = f"duplicates {sub}"
        if varlist.strip():
            cmd += f" {varlist.strip()}"
        cmd += _filter_clause(condition, in_range)
        return _run_stata_command(cmd)

    if check == "isid":
        if not varlist.strip():
            return _make_error_result(
                '错误: check="isid" 必须提供 varlist（要检验唯一性的变量）'
            )
        cmd = f"isid {varlist.strip()}"
        if options.strip():
            cmd += f", {options.strip()}"
        return _run_stata_command(cmd)

    cmd = "misstable summarize"
    if varlist.strip():
        cmd += f" {varlist.strip()}"
    cmd += _filter_clause(condition, in_range)
    if options.strip():
        cmd += f", {options.strip()}"
    return _run_stata_command(cmd)


# merge 的匹配基数（[D] merge）。m:m 官方明确不推荐，但仍是合法形式。
_MERGE_KINDS = ("1:1", "m:1", "1:m", "m:m")


def _split_using_paths(using: str, single: bool = False) -> tuple[list[str], str | None]:
    """把 ``using`` 参数解析成逐个校验、规范化后的路径列表。

    每个路径都要过 ``_validate_path`` —— append 可以一次接多个文件，
    只校验第一个等于给后面的留了口子。

    含空格的路径在真实系统上是常态（``/Users/x/My Drive/…``、
    ``C:/Program Files/…``），而按空白无脑切分会把它们劈成两半，报出的错还与
    真实原因无关。两条出路：

    - ``single=True``（``merge`` 只接一个文件）：整个字符串就是一个路径，
      不切分，天然支持空格。
    - ``single=False``（``append`` 可接多个）：按**双引号感知**切分，
      用户用 ``"a b.dta" "c.dta"`` 表达含空格的路径；引号在校验前剥掉
      （``_validate_path`` 会拒绝 ``"``）。

    Returns:
        (规范化路径列表, 错误文本)；成功时错误为 None。
    """
    if single:
        raw = [using.strip()] if using.strip() else []
    else:
        try:
            raw = [t for t in shlex.split(using, posix=False) if t]
        except ValueError:
            return [], "错误: using 路径含未闭合的引号"
        raw = [t[1:-1] if len(t) >= 2 and t[0] == t[-1] == '"' else t for t in raw]
    if not raw:
        return [], "错误: 请提供至少一个 .dta 文件路径"
    out = []
    for p in raw:
        if err := _validate_path(p):
            return [], err
        out.append(_normalize_path(p))
    return out, None


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def stata_merge(
    kind: str,
    keyvars: str,
    using: str,
    keepusing: str = "",
    condition: str = "",
    in_range: str = "",
    options: str = "",
) -> str | ToolResult:
    """横向合并数据集（``merge``）。

    官方语法：``merge 1:1|m:1|1:m|m:m varlist using filename [, options]``。
    合并结果记在 ``_merge`` 变量里（1=仅主数据、2=仅使用数据、3=两边都有），
    合并后用 ``stata_tabulate("_merge")`` 检查匹配情况。

    Args:
        kind: 匹配基数 —— "1:1" / "m:1" / "1:m" / "m:m"（官方不推荐 m:m）。
        keyvars: 匹配键变量（空格分隔）；按观测号合并时传 "_n"。
        using: 被合并的 .dta 文件路径。
        keepusing: 只从使用数据中带入这些变量（留空 = 全部）。
        condition: if 条件子句（可选）。
        in_range: 观测范围（可选）。
        options: 官方选项，如 "nogenerate"、"keep(match)"、"assert(match)"、
            "update replace"、"force"、"noreport"。

    Returns:
        合并结果的匹配汇总表。
    """
    if kind not in _MERGE_KINDS:
        return _make_error_result(
            f"错误: kind 只能是 {list(_MERGE_KINDS)}（收到 {kind!r}）"
        )
    if not keyvars.strip():
        return _make_error_result('错误: 请提供匹配键变量（按观测号合并传 "_n"）')
    if err := _validate_varlist(keyvars, "keyvars"):
        return _result_or_error(err)
    if err := _validate_varlist(keepusing, "keepusing"):
        return _result_or_error(err)
    for value, label in ((condition, "condition"), (in_range, "in_range"),
                         (options, "options")):
        if err := _validate_no_injection(value, label):
            return _result_or_error(err)
    paths, err = _split_using_paths(using, single=True)
    if err:
        return _result_or_error(err)

    cmd = f'merge {kind} {keyvars.strip()} using "{paths[0]}"'
    cmd += _filter_clause(condition, in_range)
    opts = " ".join(
        p for p in (
            f"keepusing({keepusing.strip()})" if keepusing.strip() else "",
            options.strip(),
        ) if p
    )
    if opts:
        cmd += f", {opts}"
    return _run_stata_command(cmd, timeout=120, require_file=paths[0])


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def stata_append(using: str, options: str = "") -> str | ToolResult:
    """纵向追加数据集（``append``）。

    官方语法：``append using filename [filename …] [, options]`` —— 可一次
    接多个文件。变量按名字对齐，缺的补缺失值。

    Args:
        using: 一个或多个 .dta 文件路径（空格分隔）。
        options: 官方选项，如 "generate(src)"（标记来源）、"keep(varlist)"、
            "nolabel"、"nonotes"、"force"（允许字符/数值类型不一致）。

    Returns:
        追加确认信息。
    """
    if err := _validate_no_injection(options, "options"):
        return _result_or_error(err)
    paths, err = _split_using_paths(using)
    if err:
        return _result_or_error(err)

    cmd = "append using " + " ".join(f'"{p}"' for p in paths)
    if options.strip():
        cmd += f", {options.strip()}"
    return _run_stata_command(cmd, timeout=120, require_file=paths[0])


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def stata_reshape(
    direction: str, stub: str, i: str, j: str = "", options: str = ""
) -> str | ToolResult:
    """长宽表互转（``reshape``）。

    官方语法：``reshape long|wide stub, i(i) j(j)``。
    面板分析（``xtreg`` 等）要求**长表**：每个个体-时点一行。

    ``long``：宽转长，把 ``inc1980 inc1981`` 合成 ``inc`` 加一列 ``year``。
    ``wide``：长转宽，反向操作。

    Args:
        direction: "long" 或 "wide"。
        stub: 变量名前缀，如 "inc"（对应 inc1980、inc1981 …）。可给多个。
        i: 个体标识变量（转换前后都唯一标识一行/一组）。
        j: 区分列的变量 —— long 方向是**新建**的，wide 方向是**已存在**的。
        options: 官方选项，如 "string"（j 是字符串）、"atwl(_)"。

    Returns:
        转换前后的形态汇总。
    """
    if direction not in ("long", "wide"):
        return _make_error_result(
            f'错误: direction 只能是 "long" 或 "wide"（收到 {direction!r}）'
        )
    if not stub.strip():
        return _make_error_result('错误: 请提供变量名前缀 stub，如 "inc"')
    if not i.strip():
        return _make_error_result("错误: 请提供个体标识变量 i")
    if err := _validate_varlist(stub, "stub"):
        return _result_or_error(err)
    if err := _validate_varlist(i, "i"):
        return _result_or_error(err)
    if err := _validate_varlist(j, "j"):
        return _result_or_error(err)
    if err := _validate_no_injection(options, "options"):
        return _result_or_error(err)

    cmd = f"reshape {direction} {stub.strip()}, i({i.strip()})"
    if j.strip():
        cmd += f" j({j.strip()})"
    if options.strip():
        cmd += f" {options.strip()}"
    return _run_stata_command(cmd, timeout=120)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def stata_collapse(
    clist: str,
    by: str = "",
    condition: str = "",
    in_range: str = "",
    options: str = "",
) -> str | ToolResult:
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

    Returns:
        聚合确认信息。
    """
    if not clist.strip():
        return _make_error_result(
            '错误: 请提供聚合表达式 clist，如 "(mean) price (sd) mpg"'
        )
    if err := _validate_varlist(by, "by"):
        return _result_or_error(err)
    for value, label in ((clist, "clist"), (condition, "condition"),
                         (in_range, "in_range"), (options, "options")):
        if err := _validate_no_injection(value, label):
            return _result_or_error(err)

    cmd = f"collapse {clist.strip()}"
    cmd += _filter_clause(condition, in_range)
    opts = " ".join(
        p for p in (f"by({by.strip()})" if by.strip() else "", options.strip()) if p
    )
    if opts:
        cmd += f", {opts}"
    return _run_stata_command(cmd, timeout=120)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_return_list(kind: str = "r") -> str | ToolResult:
    """一次列出全部返回值（``return`` / ``ereturn`` / ``creturn list``）。

    比 ``stata_display("r(mean)")`` 逐个取高效得多 —— Agent 通常先看有哪些值
    再决定取哪个。

    Args:
        kind: ``r``（默认，``r()``：summarize/tabulate 等一般命令的返回值）、
            ``e``（``e()``：估计命令的结果，如 e(N)、e(r2)、e(b)）、
            ``c``（``c()``：系统常量与设置，如 c(pwd)、c(N)、c(scheme)）。

    Returns:
        返回值清单（名称 = 值）。
    """
    prefix = {"r": "return", "e": "ereturn", "c": "creturn"}.get(kind)
    if not prefix:
        return _make_error_result(
            f'错误: kind 只能是 "r" / "e" / "c"（收到 {kind!r}）'
        )
    return _run_stata_command(f"{prefix} list")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_estat(subcommand: str, options: str = "") -> str | ToolResult:
    """运行后估计诊断（``estat``）。

    **前提**：先运行过一个估计命令（可用 ``stata_status`` 确认「当前活跃」）。
    可用子命令随模型而变，用 ``stata_help("<估计命令> postestimation")`` 查全。

    常用：``vif``（方差膨胀因子）、``hettest``（Breusch–Pagan 异方差）、
    ``ovtest``（Ramsey RESET 遗漏变量）、``ic``（AIC/BIC）、``summarize``
    （估计样本的描述统计）、``firststage``（IV 第一阶段）、``imtest``。

    Args:
        subcommand: estat 子命令名，如 "vif"、"hettest"。
        options: 该子命令的官方选项，如 "rhs iid"、"all"。

    Returns:
        诊断结果表。
    """
    if not subcommand.strip():
        return _make_error_result('错误: 请提供 estat 子命令，如 "vif"、"hettest"')
    if err := _validate_identifier(subcommand, "subcommand", required=True):
        return _result_or_error(err)
    if err := _validate_no_injection(options, "options"):
        return _result_or_error(err)
    cmd = f"estat {subcommand.strip()}"
    if options.strip():
        cmd += f", {options.strip()}"
    return _run_stata_command(cmd)


# estimates 的子命令。store/restore/save/use/drop 需要名字；dir/clear/table/stats
# 不需要（table/stats 的名字可选，留空即用当前活跃估计）。
_ESTIMATES_ACTIONS = frozenset(
    {"store", "restore", "table", "stats", "dir", "drop", "clear", "describe", "replay"}
)
_ESTIMATES_NEED_NAME = frozenset({"store", "restore", "drop"})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def stata_estimates(
    action: str = "dir", name: str = "", options: str = ""
) -> str | ToolResult:
    """管理已存储的估计结果（``estimates``）。

    典型用法：跑完多个模型后逐个 ``store``，再用 ``table`` 并排比较。
    当前已存了哪些可用 ``stata_status`` 或 ``action="dir"`` 查看。

    Args:
        action: ``store`` / ``restore`` / ``drop``（均需 name）、
            ``table`` / ``stats`` / ``describe`` / ``replay``（name 可选）、
            ``dir`` / ``clear``（无需 name）。
        name: 估计结果名；``table`` / ``stats`` 可给多个（空格分隔）。
        options: 官方选项，如 "star stats(N r2)"（table）、"aic bic"（stats）。

    Returns:
        操作确认或比较表。
    """
    if action not in _ESTIMATES_ACTIONS:
        return _make_error_result(
            f"错误: action 只能是 {sorted(_ESTIMATES_ACTIONS)}（收到 {action!r}）"
        )
    if err := _validate_varlist(name, "name"):
        return _result_or_error(err)
    if err := _validate_no_injection(options, "options"):
        return _result_or_error(err)
    if action in _ESTIMATES_NEED_NAME and not name.strip():
        return _make_error_result(f'错误: action="{action}" 必须提供 name')

    cmd = f"estimates {action}"
    if name.strip():
        cmd += f" {name.strip()}"
    if options.strip():
        cmd += f", {options.strip()}"
    return _run_stata_command(cmd)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def stata_use_example(
    name: str = "", source: str = "sysuse", clear: bool = True, action: str = "load"
) -> str | ToolResult:
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

    Returns:
        加载确认与数据集概览。
    """
    if source not in ("sysuse", "webuse"):
        return _make_error_result(
            f'错误: source 只能是 "sysuse" 或 "webuse"（收到 {source!r}）'
        )
    if action not in ("load", "list"):
        return _make_error_result(
            f'错误: action 只能是 "load" 或 "list"（收到 {action!r}）'
        )
    if action == "list":
        # webuse 没有 dir 子命令，列表一律走本地 sysuse dir。
        return _run_stata_command("sysuse dir")
    if not name.strip():
        return _make_error_result('错误: 请提供数据集名，如 name="auto"')
    if err := _validate_identifier(name, "name", required=True):
        return _result_or_error(err)

    cmd = f"{source} {name.strip()}"
    if clear:
        cmd += ", clear"
    # webuse 要联网取数，给足超时。
    return _run_stata_command(cmd, timeout=120 if source == "webuse" else 60)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def stata_xtset(
    panelvar: str = "",
    timevar: str = "",
    action: str = "set",
    options: str = "",
) -> str | ToolResult:
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
        return _make_error_result(
            f'错误: action 只能是 "set" / "show" / "clear"（收到 {action!r}）'
        )
    if action == "show":
        # 裸 xtset 是查询，但未设定时报 r(459)；capture noisily 既不中断
        # 命令链，又保留 "panel variable not set" 这句有用的诊断。
        # 实测 xtset 对纯时序数据也照报 "Time variable: …"，无需再发 tsset。
        return _run_stata_command("capture noisily xtset")
    if action == "clear":
        return _run_stata_command("xtset, clear")

    if err := _validate_identifier(panelvar, "panelvar"):
        return _result_or_error(err)
    if err := _validate_identifier(timevar, "timevar"):
        return _result_or_error(err)
    if err := _validate_no_injection(options, "options"):
        return _result_or_error(err)
    if not panelvar.strip() and not timevar.strip():
        return _make_error_result(
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
    return _run_stata_command(cmd)


# 扩展名 → 官方 import 子命令（依据 [D] import 的方法表）。
# .dta 不在此列 —— 它走 `use`，不属于 import 命令族。
_IMPORT_FORMAT_BY_EXT = {
    ".xlsx": "excel", ".xls": "excel",
    ".csv": "delimited", ".tsv": "delimited", ".txt": "delimited", ".dat": "delimited",
    ".sas7bdat": "sas",
    ".sav": "spss", ".zsav": "spss",
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
# case() 除 parquet 外各格式都有。
_IMPORT_CASE_FORMATS = frozenset({"excel", "delimited", "sas", "spss", "dbase"})
# [if] [in] 只有 sas / spss 有（`import sas [namelist] [if] [in] using file`）。
_IMPORT_FILTER_FORMATS = frozenset({"sas", "spss"})
# varlist 位置的**语义随格式而变**，不能统一映射：
#   sas/spss 的 namelist 与 parquet 的 columnlist 是「只导入这些列」（筛选）；
#   excel/delimited 的 extvarlist 却是「给导入的列命名」（重命名）。
# 把重命名当筛选用会静默导入错的数据，故只对筛选语义的三种格式放行。
_IMPORT_SELECT_FORMATS = frozenset({"sas", "spss", "parquet"})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
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
) -> str | ToolResult:
    """导入非 .dta 格式的数据文件（与 stata_export_* 对称）。

    覆盖官方 ``import`` 命令族：excel / delimited / sas / spss / dbase / parquet。
    ``.dta`` 请用 ``stata_use_dataset``（它属于 ``use``，不是 import）。

    **不适用于目标格式的选项会被丢弃并说明** —— 实测传错会 r(198)：
    ``firstrow`` / ``sheet`` / ``cellrange`` 仅 excel；``delimiter`` /
    ``varnames`` / ``encoding`` 仅 delimited；``[namelist] [if] [in]`` 仅 sas。

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
        encoding: 文件编码，如 "utf-8"、"gbk"。**仅 delimited**。
        case: 变量名大小写 —— preserve / lower / upper。excel 与 delimited 均支持。
        varlist: 只导入这些变量。**仅 sas**。
        condition: if 条件子句。**仅 sas**。
        in_range: 观测范围。**仅 sas**。
        options: 其余官方选项的自由文本逃生舱。

    Returns:
        导入确认信息（含变量与观测数概览）。
    """
    if err := _validate_path(filepath):
        return _result_or_error(err)
    if err := _validate_varlist(varlist, "varlist"):
        return _result_or_error(err)
    for value, label in ((condition, "condition"), (in_range, "in_range")):
        if err := _validate_filter_expr(value, label):
            return _result_or_error(err)
    if err := _validate_no_injection(options, "options"):
        return _result_or_error(err)
    # 这几项都被拼进 `opt("<值>")` 或 `opt(<值>)`，校验强度必须与
    # stata_export_excel 的 sheet 对称 —— 后者走 _validate_sheet_name 明确拒绝
    # `"`，而此处曾混在 _validate_no_injection 那批里（只拒换行/回车/空字节/分号），
    # 于是同一个值在两个工具里下场完全相反：`S1") cellrange(A1:A1) //` 能提前
    # 闭合引号并注入任意 import 选项。
    if err := _validate_sheet_name(sheet):
        return _result_or_error(err.replace("工作表名", "sheet"))
    for value, label, pattern in (
        (cellrange, "cellrange", _IMPORT_CELLRANGE_RE),
        (varnames, "varnames", _IMPORT_VARNAMES_RE),
    ):
        if value.strip() and not pattern.match(value.strip()):
            return _make_error_result(
                f"错误: {label} 格式非法（收到 {value!r}）—— 它被原样拼进 "
                f"{label}(...)，含 ) 或引号即可逃逸出括号注入其他选项"
            )
    if encoding and any(ch in encoding for ch in ('"', "(", ")")):
        return _make_error_result(
            '错误: encoding 不能包含 " 或括号（它被拼进 encoding("...")）'
        )
    if case and case not in ("preserve", "lower", "upper"):
        return _make_error_result(
            f'错误: case 只能是 "preserve" / "lower" / "upper"（收到 {case!r}）'
        )

    ext = os.path.splitext(filepath)[1].lower()
    if format in ("", "auto"):
        if ext == ".dta":
            return _make_error_result(
                "错误: .dta 不属于 import 命令族，请改用 "
                'stata_use_dataset("路径")（底层是 `use`）。'
            )
        fmt = _IMPORT_FORMAT_BY_EXT.get(ext)
        if not fmt:
            return _make_error_result(
                f"错误: 无法从扩展名 {ext or '(无)'} 推断导入格式。"
                f"请显式指定 format={sorted(_IMPORT_FORMATS)}，"
                "或用 stata_run 执行官方 import 命令。"
            )
    elif format not in _IMPORT_FORMATS:
        return _make_error_result(
            f"错误: format 只能是 {sorted(_IMPORT_FORMATS)}（收到 {format!r}）"
        )
    else:
        fmt = format

    import_path = _normalize_path(filepath)
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
        if fmt not in _IMPORT_DELIMITED_ONLY:
            dropped.append("delimiter")
        elif delimiter == "tab":
            opts.append("delimiters(tab)")
        elif len(delimiter) != 1 or delimiter in ('"', "`", "$", "\\"):
            return _make_error_result(
                f'错误: delimiter 只能是单个字符或关键字 "tab"（收到 {delimiter!r}）'
            )
        else:
            opts.append(f'delimiters("{delimiter}")')
    _take(varnames, _IMPORT_DELIMITED_ONLY, "varnames", f"varnames({varnames.strip()})")
    _take(encoding, _IMPORT_DELIMITED_ONLY, "encoding", f'encoding("{encoding.strip()}")')
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
        cmd += _filter_clause(condition, in_range)
    else:
        for value, label in ((condition, "condition"), (in_range, "in_range")):
            if value.strip():
                dropped.append(label)
    cmd += f' using "{import_path}"'
    if opts:
        cmd += f", {' '.join(opts)}"

    result = _run_stata_command(cmd, timeout=120, require_file=filepath)
    if isinstance(result, ToolResult):
        return result
    if dropped:
        result += (
            f"\n提示：{fmt} 格式不支持 {', '.join(dropped)}"
            "（Stata 会报 option ... not allowed），已忽略。"
        )
    return result + extra_note


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
    # 与 stata_run / stata_run_do_file 一致地钳制 —— 此前完全未钳制：timeout=1
    # 会架起 1 秒看门狗（而 ssc install 实测需 3–13 秒，必然被 break），
    # timeout=10**6 则突破 docstring 与 CLAUDE.md 所述的 1800 秒上限。
    return _run_stata_command(cmd, timeout=max(10, min(timeout, 1800)))


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
def stata_find_package(
    keyword: str,
    scope: str = "",
    match_any: bool = False,
    exclude_sj: bool = False,
    error_if_none: bool = False,
    options: str = "",
) -> str | ToolResult:
    """搜索可安装的 Stata 扩展包（联网）。

    使用 ``net search``，覆盖 SSC 与 Stata Journal 等 net 资源，返回包名、
    来源 URL 与简介；拿到包名后用 ``stata_describe_package`` 看详情、
    ``stata_install_package`` 安装。

    访问 www.stata.com，实测单次 0.6–2 秒。**宽泛的多词查询输出很大** ——
    实测 "difference in differences" 默认返回 94K 字符（24 页），用
    ``scope="toc"`` 可收窄到 12K。仅搜本机已装帮助用
    ``stata_run("search <词>, local")``。

    Args:
        keyword: 搜索关键词，可多词（默认要求**全部**命中）。
        scope: 搜索范围 —— ``toc``（只搜目录，最省输出）/ ``pkg``（只搜包）/
            ``tocpkg``（默认，两者都搜）/ ``everywhere`` / ``filenames``。
        match_any: 命中**任一**关键词即可（官方 ``or`` 选项）。
            **实测显著变慢**：同一查询默认 2.3s，加 or 后 30s。
        exclude_sj: 排除 Stata Journal 来源，只看 SSC 等（官方 ``nosj``）。
        error_if_none: 无匹配时返回错误结果而非普通文本（官方 ``errnone``，
            rc=111）。默认 False —— 搜不到东西本身不是错误。
        options: 其余官方选项的自由文本逃生舱。

    Returns:
        匹配的包列表及简要描述。
    """
    if err := _validate_no_injection(keyword, "keyword"):
        return _result_or_error(err)
    if err := _validate_no_injection(options, "options"):
        return _result_or_error(err)
    if not keyword.strip():
        return _make_error_result("错误：请提供搜索关键词。")
    if scope and scope not in ("toc", "pkg", "tocpkg", "everywhere", "filenames"):
        return _make_error_result(
            '错误: scope 只能是 "toc" / "pkg" / "tocpkg" / "everywhere" / '
            f'"filenames"（收到 {scope!r}）'
        )

    opts = " ".join(
        p for p in (
            scope,
            "or" if match_any else "",
            "nosj" if exclude_sj else "",
            "errnone" if error_if_none else "",
            options.strip(),
        ) if p
    )
    cmd = f"net search {keyword.strip()}"
    if opts:
        cmd += f", {opts}"
    return _run_stata_command(cmd, timeout=120)


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

    覆盖 Agent 决策所需的全部会话前提：数据集概览、工作目录、内存、**当前与
    其余 frame**、**面板 / 时序设定**、**已存与当前活跃的估计结果**。

    这些前提此前只能靠试错发现 —— 例如 ``stata_xtreg`` 要求先 ``xtset``，
    ``stata_margins`` / ``stata_test`` / ``stata_predict`` 要求已有估计结果。
    变量清单请用 ``stata_describe``，此处只给概览。

    Returns:
        会话状态摘要。
    """
    # 查工作目录必须用 display c(pwd)，不能用裸 cd —— Stata 的 cd 不带参数会
    # **切换**到 home 目录（同 Unix shell）并把新目录打印出来，看着像查询实为修改。
    # 曾因此让本工具在 readOnlyHint=True 的情况下悄悄重置用户 set_cwd 的结果，
    # 使后续相对路径全部指向 home。
    #
    # xtset 不带参数是**查询**，未设定时报 r(459)。用 `capture noisily` 既不让
    # 整条链中断，又保留 "panel variable not set" 那句诊断 —— 它本身就是状态。
    # 只发 xtset 不发 tsset：实测二者对**已设定**状态的报告逐字相同（纯时序数据
    # 下 xtset 也照报 "Time variable: …"），同时发只会把同一段输出打两遍。
    return _run_stata_command(
        "\n".join(
            [
                'display "===== 数据集 ====="',
                "describe, short",
                'display "===== 工作目录 ====="',
                "display c(pwd)",
                'display "===== Frame ====="',
                'display "当前 frame: " c(frame)',
                "frame dir",
                'display "===== 面板 / 时序设定 ====="',
                "capture noisily xtset",
                'display "===== 估计结果 ====="',
                'display "当前活跃: " e(cmd)',
                "estimates dir",
                'display "===== 内存 ====="',
                "memory",
            ]
        )
    )


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
