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

import sys
import os
import io
import re
import time
import threading
import atexit
import logging
from logging.handlers import RotatingFileHandler
from functools import wraps

# =============================================================================
# 配置
# =============================================================================

STATA_HOME = os.environ.get("STATA_HOME", r"C:\Program Files\StataNow\StataNow19")
STATA_EDITION = os.environ.get("STATA_EDITION", "mp")

# 日志同时写入 stderr（避免污染 MCP stdio）和日志文件，便于故障排查
_LOG_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
)
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
    config.stconfig['streamout'] = 'off'
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

# stout 必须在 init() 之后导入（check_initialized 检查）
from pystata.core import stout

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

from fastmcp import FastMCP
from fastmcp.tools.base import ToolResult
from mcp.types import ToolAnnotations

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
# 命令输入最大长度
MAX_COMMAND_LENGTH = 65_536
# 最近一次完整输出的缓存（支持翻页）
_last_output = ""
# 保护 _last_output 读写的独立锁，避免翻页与命令执行串行化
_output_lock = threading.Lock()
# Ping 缓存：避免高频命令的重复心跳开销
_last_ping_time = 0.0
PING_CACHE_SECONDS = 2.0  # 2 秒内跳过重复 ping

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
    111: "变量名已存在",
    198: "命令语法错误",
    199: "选项语法错误",
    3000: "命令执行成功，无文本输出",
    999: "Stata DLL 内部崩溃",
    998: "Stata DLL 无响应",
}

# 输入安全：允许的 Stata 标识符（变量/包名）字符集合
# Stata 变量名最大 32 字符，必须以字母或下划线开头，后续可为字母/数字/下划线
_STATA_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,31}$")
# 允许的包来源：ssc 或 HTTPS URL（至少限制 scheme 与基本主机格式）
_INSTALL_SOURCE_RE = re.compile(r"^https://[a-zA-Z0-9][-a-zA-Z0-9.]*(/)?.*$", re.IGNORECASE)

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
_DANGEROUS_COMMAND_PREFIXES = ("!", "shell", "python:", "python(")


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
                target = line[len(prefix):].strip() or "<empty>"
                return (
                    f"错误: 命令 '{line[:60]}' 包含危险前缀 '{prefix}'，"
                    f"可能执行主机系统/Py 代码（目标: {target[:40]}）。"
                    "如确有必要，请使用操作系统直接操作，而非通过 Stata MCP。"
                )
        # 检测 python 主机命令（部分 Stata 版本使用 python 子命令）
        if lowered == "python" or lowered.startswith("python "):
            return f"错误: 命令 '{line[:60]}' 尝试调用 Stata 内嵌 Python，已被禁止"
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


def _validate_identifier(value: str, label: str = "变量名") -> str | None:
    """校验单个 Stata 标识符格式。"""
    if not value or not value.strip():
        return None
    value = value.strip()
    if _contains_injection_chars(value):
        return f"错误: {label} 包含非法字符"
    if not _STATA_IDENTIFIER_RE.match(value):
        return (
            f"错误: {label} '{value}' 不符合安全格式。"
            "只允许字母、数字、下划线，且不能以数字开头。"
        )
    return None


def _validate_varlist(value: str, label: str = "varlist") -> str | None:
    """校验 Stata 变量列表字符串，仅阻止注入与危险元字符，不限制合法语法。

    允许因子变量（i.var）、时间序列算子（L.var）、交互项（c.x##i.g）、
    权重子句（[aw=...]）、范围（x1-x10）、通配符（mpg*）等。
    """
    if not value or not value.strip():
        return None
    if any(ch in value for ch in _VARLIST_FORBIDDEN_CHARS):
        return f"错误: {label} 包含非法字符（换行、回车、空字节、分号、!、|、&、反引号或 $）"
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


def _validate_path(path: str) -> str | None:
    """校验路径安全性：拒绝空字节、双引号、分号、换行以及越界路径穿越。

    当 STATA_ALLOWED_ROOTS 环境变量配置时，所有路径必须落在允许根目录下。
    """
    if not path or not path.strip():
        return "错误: 路径为空"
    if "\x00" in path or '"' in path or ";" in path or "\n" in path or "\r" in path:
        return "错误: 路径包含非法字符"
    normalized = os.path.normpath(os.path.abspath(path))
    # 拒绝 UNC 路径（除非 STATA_ALLOW_UNC=1）
    if normalized.startswith("\\\\") and not _STATA_ALLOW_UNC:
        return "错误: 不允许 UNC 网络路径"
    # 相对路径限制在当前工作目录内，防止 .. 越界
    if not os.path.isabs(path):
        try:
            rel = os.path.relpath(normalized, os.getcwd())
            if rel.startswith(".."):
                return "错误: 相对路径不能超出当前工作目录"
        except ValueError:
            return "错误: 路径无效"
    # ALLOWED_ROOTS 沙箱检查（仅对工具入口路径做检查，STATA_ALLOWED_ROOTS 配置后生效）
    if not _is_path_allowed(normalized):
        roots = _init_allowed_roots()
        return (
            f"错误: 路径 '{normalized}' 不在允许目录下。"
            f"已配置的允许根目录: {'; '.join(roots)}。"
            "如确需访问此路径，请更新 STATA_ALLOWED_ROOTS 环境变量。"
        )
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

    total_chars = len(text)
    total_pages = max(1, (total_chars + page_size - 1) // page_size)

    if page == 0 or page_size <= 0:
        return text

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
        footer += f" — 使用 stata_more(page={page+1}) 翻下页"
    if page > 1:
        footer += f" — stata_more(page={page-1}) 翻上页"
    footer += " — stata_more(page=0) 显示全部"

    return header + chunk + footer


def _flush_block(buffer: list[str], blocks: list[str]) -> None:
    """将 buffer 中非空内容作为一个命令块追加，然后清空 buffer。"""
    block_text = "\n".join(buffer)
    if block_text.strip():
        blocks.append(block_text)
    buffer.clear()


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
                    if ch == "'" and nxt == '"':
                        in_compound_string = False
                        in_string = False
                else:
                    if ch == '"':
                        in_string = False
                i += 1
                continue

            # 复合字符串 '" ... "'
            if ch == '"' and nxt == "'":
                in_string = True
                in_compound_string = True
                content.append(ch)
                i += 1
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

    for raw_line in cmd.split("\n"):
        line = raw_line.strip("\r")
        if not line.strip() and brace_depth == 0 and not buffer and not in_block_comment and not in_continuation:
            continue

        content, has_cont, delta, in_block_comment, space_before = _scan_line(line, in_block_comment)
        brace_depth += delta
        if brace_depth < 0:
            brace_depth = 0

        if has_cont:
            if content.strip():
                # 与 buffer 中当前行合并：/// 所在行与下一行属于同一条命令
                if buffer:
                    last = buffer[-1]
                    sep = " " if space_before else ""
                    buffer[-1] = last.rstrip() + sep + content.rstrip()
                else:
                    buffer.append(content.rstrip())
                in_continuation = True
                cont_space_before = space_before
            else:
                # /// 行无实质内容（如 /// comment 或行首 ///）时中断当前续行链，
                # 并把已累积的 buffer 作为一个 block 发出，避免后续行被错误拼接。
                in_continuation = False
                if brace_depth == 0 and not in_block_comment:
                    _flush_block(buffer, blocks)
            continue

        if in_continuation and buffer:
            if content.strip():
                last = buffer[-1]
                sep = " " if cont_space_before else ""
                buffer[-1] = last.rstrip() + sep + content.lstrip()
            in_continuation = False
            # 若续行被空行（或仅注释行）结束，直接尝试发出当前 block
            if brace_depth == 0 and not in_block_comment:
                _flush_block(buffer, blocks)
                continue
        else:
            buffer.append(content)

        if brace_depth == 0 and not in_block_comment:
            _flush_block(buffer, blocks)

    if buffer:
        _flush_block(buffer, blocks)

    return blocks


def _drain_output(min_wait: float = 0.1, quiet_gap: float = 0.02) -> str:
    """排空输出缓冲，返回残留内容。

    使用指数退避轮询：初始 1ms，最大 20ms；在检测到输出后重置回 1ms。
    安静窗口用于确认输出已结束，避免固定高频轮询浪费 CPU。

    参数可调：min_wait=最小等待秒，quiet_gap=安静判定秒。

    用于执行前清理和 SetBreak 后的错误恢复。
    """
    parts = io.StringIO()
    t_start = time.time()
    last_nonempty = time.time()
    sleep_ms = 1

    while time.time() - t_start < min_wait:
        out = config.get_output()
        if out:
            parts.write(out)
            last_nonempty = time.time()
            sleep_ms = 1
        if time.time() - last_nonempty > quiet_gap:
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
                out += "\n(Stata 已自动恢复，请重试命令)"
            else:
                rc = 998
                out += "\n(Stata 崩溃且无法自动恢复，需要重启 MCP Server)"
        except Exception as e:
            logger.exception("Stata 崩溃恢复失败: %s", e)
            rc = 998
            out += "\n(Stata 崩溃且无法自动恢复，需要重启 MCP Server)"

    return rc, out


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
            encoded = config.get_encode_str(cmd)
            rc = config.stlib.StataSO_Execute(encoded, False)
    except Exception as e:
        logger.exception("StataSO_Execute crashed on: %s", cmd[:80])
        exec_done.set()
        return 999, f"StataSO_Execute 崩溃: {e}"

    exec_done.set()

    # 仅在看门狗触发 break 后排空错误输出
    if did_break:
        time.sleep(0.1)
        _drain_output(min_wait=0.05)

    # --- 自适应输出收集 ---
    out_buf = io.StringIO()
    total_len = 0
    empty_count = 0

    # 阶段 1: 快轮询（最多 300 次，连续 3 次空转退出，指数退避）
    sleep_ms = 1
    attempts = 0
    while attempts < 300:
        out = config.get_output()
        if out:
            out_buf.write(out)
            total_len += len(out)
            empty_count = 0
            sleep_ms = 1
            if total_len >= MAX_OUTPUT_CHARS:
                out_buf.write("\n(输出已截断)")
                break
        else:
            empty_count += 1
            if empty_count >= 3:
                break
        time.sleep(sleep_ms / 1000.0)
        sleep_ms = min(sleep_ms * 2, 20)
        attempts += 1

    # 阶段 2: 智能清尾 — 仅在输出较小时做短 drain
    if total_len < MAX_OUTPUT_CHARS:
        if total_len < 10_000:
            # 小输出：短 drain（50ms 上限）
            tail = _drain_output(min_wait=0.05, quiet_gap=0.01)
        else:
            # 大输出：完整 drain（100ms 上限）
            tail = _drain_output(min_wait=0.1, quiet_gap=0.015)
        if tail:
            out_buf.write(tail)
            total_len += len(tail)

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


def _run_stata_command(cmd: str, page: int = 1, timeout: int = 60, require_file: str | None = None) -> str | ToolResult:
    """执行 Stata 命令，支持分页浏览。

    多行命令按 \\n 拆分后逐条执行。
    支援 `///` 续行符和 `{ }` 复合块（自动合并为单次 StataSO_Execute 调用）。
    当输出超过 PAGE_SIZE 时自动缓存完整输出并返回首页。
    所有执行经过 _execute_safe（预检 + 超时 + 崩溃恢复）。

    Args:
        cmd: Stata 命令字符串（多命令用 \\n 分隔）。
        page: 页码（1-based），0 = 全部，仅对单命令有效。
        timeout: 每条命令的超时秒数（默认 60）。
        require_file: 若提供，在获取锁后先校验该文件是否存在；
            不存在则直接返回错误，不会访问 Stata DLL 执行 cmd。

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
        # 若调用方要求预先校验文件，在锁内使用 Stata cwd 检查
        if require_file:
            if err := _check_file_exists_locked(require_file):
                return _make_error_result(err)
            # 对相对路径，确保 Stata 实际执行时使用的路径与检查路径一致
            if not os.path.isabs(require_file):
                cwd = _get_stata_cwd_locked()
                if cwd:
                    py_abs = _normalize_path(require_file)
                    st_abs = os.path.normpath(os.path.join(cwd, require_file)).replace("\\", "/")
                    cmd = cmd.replace(f'"{py_abs}"', f'"{st_abs}"')

        # 使用新的解析器：正确处理 /// 续行和 { } 复合块
        blocks = _parse_command_blocks(cmd)

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

        full = all_buf.getvalue() if hwritten else "(命令执行成功，无文本输出)"
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


def _resolve_path_for_stata(filepath: str) -> str:
    """将用户传入路径解析为 Stata 可接受的绝对路径；失败时返回错误文本。

    该函数只进行纯 Python 字符串/文件系统校验，不访问 Stata DLL。
    """
    if err := _validate_path(filepath):
        return err
    normalized = os.path.normpath(os.path.abspath(filepath)).replace("\\", "/")
    return normalized


def _check_file_exists_locked(filepath: str) -> str | None:
    """在 _stata_lock 保护下检查文件是否存在。

    相对路径优先使用 Stata 当前工作目录解析（因为后续命令由 Stata 执行）。
    若无法获取 Stata cwd，则回退到 MCP server 进程当前工作目录。
    返回错误消息或 None。
    """
    resolved = _resolve_path_for_stata(filepath)
    if isinstance(resolved, str) and resolved.startswith("错误:"):
        return resolved
    check_path = resolved
    if not os.path.isabs(filepath):
        cwd = _get_stata_cwd_locked()
        if cwd:
            check_path = os.path.normpath(os.path.join(cwd, filepath)).replace("\\", "/")
    if not os.path.isfile(check_path):
        return f"错误: 文件不存在 — {check_path}"
    return None


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
    if reason := _has_dangerous_command_prefix(command):
        return _make_error_result(reason)
    return _run_stata_command(command, page, timeout=safe_timeout)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def stata_run_do_file(filepath: str) -> str | ToolResult:
    """执行一个 Stata .do 文件并返回全部输出。

    .do 文件是 Stata 的批处理脚本。此工具会执行指定路径的 .do 文件。

    Args:
        filepath: .do 文件的绝对路径。

    Returns:
        do 文件执行过程中的全部 Stata 输出。
    """
    return _run_stata_command(f'do "{_normalize_path(filepath)}"', require_file=filepath, timeout=300)


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

    Args:
        varlist: 要列出的变量（空格分隔），留空 = 全部。
        n: 显示前 n 条观测（默认 10，设为 0 显示全部，慎用）。
        in_range: 观测范围如 "1/20" 或 "1/l"。
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
    if err := _validate_identifier(varname, "varname"):
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
    if err := _validate_identifier(depvar, "depvar"):
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
        options: 额外选项，如 "or"（优势比）、"robust"。
        condition: if 条件子句（可选）。例："age >= 18"。

    Returns:
        Logistic 回归结果表。
    """
    if err := _validate_identifier(depvar, "depvar"):
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
    """运行 t 检验。

    支持单样本 t 检验、独立样本 t 检验（按分组变量）、配对 t 检验。

    Args:
        varname: 要检验的变量名。
        byvar: 分组变量（可选，用于独立样本 t 检验）。
        options: 额外选项，如 "unequal"。
        condition: if 条件子句（可选）。例："!missing(price)".

    Returns:
        t 检验结果表。
    """
    if err := _validate_identifier(varname, "varname"):
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
    """
    blocks = _parse_command_blocks("{\n" + cmd + "\n}")
    return len(blocks) != 1


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
        size_opts = f"width({width})"
        if height > 0:
            size_opts += f" height({height})"

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

        # 验证文件是否生成
        if os.path.isfile(export_path) and "[返回码:" not in result:
            size_kb = os.path.getsize(export_path) // 1024
            result += f"\n(图形已导出: {export_path}, {size_kb}KB)"

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
    if err := _validate_no_injection(sheet, "sheet"):
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
                    f"提示：回归结果导出不支持 .xlsx/sheet()，"
                    f"已自动改用 CSV 路径：{export_path}\n"
                )
            else:
                changed_msg = f"提示：回归结果已导出为 CSV：{export_path}\n"
        else:
            changed_msg = ""

        cmd = (
            f"capture which estout\n"
            f'if _rc {{\n'
            f'    display "正在安装 estout..."\n'
            f"    ssc install estout, quiet\n"
            f"}}\n"
            f'esttab using "{export_path}", csv {replace_opt} '
            f"plain nogaps nomtitles nonumber"
        )
    else:
        changed_msg = ""
        # 导出数据集为 Excel
        if varlist.strip():
            cmd = (
                f"export excel {varlist} using \"{export_path}\", "
                f"{replace_opt} {firstrow_opt} sheet({sheet})"
            )
        else:
            cmd = (
                f"export excel using \"{export_path}\", "
                f"{replace_opt} {firstrow_opt} sheet({sheet})"
            )

    result = _run_stata_command(cmd, timeout=120)

    # 若 _run_stata_command 已标记错误，直接透传
    if isinstance(result, ToolResult):
        return result

    # 验证文件已生成；仅在 Stata 未报错时追加成功提示，避免 replace=False 已存在文件时误判
    if os.path.isfile(export_path) and "[返回码:" not in result:
        size_kb = os.path.getsize(export_path) // 1024
        return f"{changed_msg}已导出 {size_kb} KB -> {export_path}\n{result}"
    return changed_msg + result


# =============================================================================
# MCP 工具 — 包管理 (destructiveHint=True)
# =============================================================================


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def stata_install_package(package: str, source: str = "ssc", replace: bool = False) -> str | ToolResult:
    """安装 Stata 扩展包。

    从 ssc 或完整 from() URL 安装 Stata 包。
    支持 force/replace 选项来解决版本冲突。

    Args:
        package: 包名称（如 "outreg2"、"estout"、"ivreg2"）。
        source: 安装源 — "ssc"（默认）或完整的 from() URL。
                例："https://fmwww.bc.edu/RePEc/bocode/o"
        replace: 是否强制替换已有文件（解决版本冲突，默认 False）。

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
    return _run_stata_command(cmd, timeout=300)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_find_package(keyword: str) -> str | ToolResult:
    """搜索 Stata 扩展包。

    在 ssc 存档中搜索与关键词匹配的 Stata 包。

    Args:
        keyword: 搜索关键词（如 "panel"、"graph"、"iv"）。

    Returns:
        匹配的包列表及简要描述。
    """
    if err := _validate_no_injection(keyword, "keyword"):
        return _result_or_error(err)
    return _run_stata_command(f"ssc search {keyword}", timeout=120)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_list_packages() -> str | ToolResult:
    """列出当前已安装的所有 Stata 扩展包。

    Returns:
        已安装包列表。
    """
    return _run_stata_command("ado describe", timeout=120)


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

    Returns:
        会话状态摘要。
    """
    return _run_stata_command("describe\ncd\nmemory")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_ping() -> str | ToolResult:
    """心跳检测 — 快速测试 Stata MCP Server 是否存活。

    执行一个极简的 Stata 命令(display 42)并返回。
    如果 Stata DLL 已崩溃或 MCP 连接已断开，此工具会报错。

    Returns:
        "pong" + 当前 Stata 版本信息。
    """
    try:
        with _stata_lock:
            rc, result = _execute_single("display 42")
        version = getattr(config, "stversion", "?")
        edition = getattr(config, "stedition", "?")
        status = "alive" if rc in (0, STATA_RC_NO_OUTPUT) and "42" in result else "degraded"
        return f"pong | Stata {version} {edition} | {status}"
    except Exception as e:
        return _make_error_result(f"Stata 心跳失败: {type(e).__name__}: {e}")


# =============================================================================
# 入口
# =============================================================================

if __name__ == "__main__":
    mcp.run(transport="stdio")
