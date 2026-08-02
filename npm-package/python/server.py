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
    JAVA_TOOL_OPTIONS: 若没有显式设置 java.awt.headless，MCP 会自动追加
        -Djava.awt.headless=true；已有 true/false 设置保持不变。
"""

import atexit
import base64
import io
import logging
import os
import re
import shlex
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from urllib.parse import unquote

# =============================================================================
# 纯辅助层重导出
# =============================================================================
# 校验、危险前缀护栏、路径字符串、命令块解析、分页与图形格式等**纯辅助函数**
# 及常量已抽取到 stata_helpers.py（该模块只依赖 os/re/mimetypes/urllib，不触碰
# 服务器状态）。此处以同名属性重导出，保持 patch("server.<name>") 与
# from server import <name> 的测试面不变 —— 被搬走的名称从这里解析，行为不变。
from stata_helpers import (  # noqa: F401
    _BARE_PREFIX_COMMANDS,
    _CELL_REFERENCE_RE,
    _COLON_DANGEROUS_HEADS,
    _DANGEROUS_COMMAND_PREFIXES,
    _EMPTY_SELECTION_MARKER,
    _FONTFACE_EXTS,
    _INCH_GRAPH_EXTS,
    _INJECTABLE_CHARS,
    _INSTALL_SOURCE_RE,
    _MAG_EXTS,
    _MAX_PREFIX_DEPTH,
    _NO_SIZE_GRAPH_EXTS,
    _QUALITY_EXTS,
    _SCHEME_NAME_RE,
    _STATA_IDENTIFIER_RE,
    _STORAGE_TYPE_RE,
    _VARLIST_FORBIDDEN_CHARS,
    PAGE_SIZE,
    UnbalancedBlockError,
    _append_default_extension,
    _canonicalize_path,
    _contains_injection_chars,
    _empty_selection_hint,
    _expand_win_short_path,
    _filter_clause,
    _flag_macro_obfuscation,
    _flush_block,
    _format_size,
    _graph_format_options,
    _graph_size_options,
    _has_dangerous_command_prefix,
    _has_delimit_change,
    _has_unsafe_brace,
    _light_strip_prefixes,
    _match_dangerous_prefix,
    _normalize_path,
    _opens_end_block,
    _paginate,
    _parse_command_blocks,
    _path_has_extension,
    _precheck_command,
    _resource_mime,
    _resource_uri,
    _split_top_level,
    _strip_command_prefixes,
    _validate_cell_reference,
    _validate_command_blocks,
    _validate_delimiter,
    _validate_filter_expr,
    _validate_fontface,
    _validate_identifier,
    _validate_install_source,
    _validate_no_injection,
    _validate_scheme_name,
    _validate_sheet_name,
    _validate_storage_type,
    _validate_varlist,
)

# =============================================================================
# 配置
# =============================================================================

STATA_HOME = os.environ.get("STATA_HOME", r"C:\Program Files\StataNow\StataNow19")
STATA_EDITION = os.environ.get("STATA_EDITION", "mp")

# 日志同时写入 stderr（避免污染 MCP stdio）和日志文件，便于故障排查。
# 默认优先写项目内 logs，便于源码运行；安装到只读目录时回退到用户临时目录，
# 不能因为日志目录不可写而让整个 MCP 在 import 阶段退出。
_DEFAULT_LOG_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"))
_LOG_DIR = os.path.normpath(os.environ.get("STATA_MCP_LOG_DIR", _DEFAULT_LOG_DIR))
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

try:
    os.makedirs(_LOG_DIR, exist_ok=True)
    _file_handler = RotatingFileHandler(
        _LOG_FILE, encoding="utf-8", maxBytes=5 * 1024 * 1024, backupCount=3
    )
except (OSError, PermissionError) as _log_error:
    # /tmp 由所有受支持平台提供；若连临时目录也不可写，stderr 仍足够让 MCP 启动。
    import tempfile

    _fallback_log_dir = os.path.join(tempfile.gettempdir(), "stata-mcp")
    try:
        os.makedirs(_fallback_log_dir, exist_ok=True)
        _LOG_DIR = _fallback_log_dir
        _LOG_FILE = os.path.join(_LOG_DIR, "stata-mcp.log")
        _file_handler = RotatingFileHandler(
            _LOG_FILE, encoding="utf-8", maxBytes=5 * 1024 * 1024, backupCount=3
        )
        logger.warning("日志目录不可写，已回退到 %s: %s", _LOG_DIR, _log_error)
    except (OSError, PermissionError):
        _file_handler = None
        logger.warning("日志文件不可用，将仅写入 stderr: %s", _log_error)

if _file_handler is not None:
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


_JAVA_HEADLESS_OPTION_RE = re.compile(r"(?<!\S)-Djava\.awt\.headless\s*=")


def _ensure_java_headless() -> bool:
    """为无 GUI 的 MCP 进程启用 Java headless，保留用户显式覆盖。

    Stata 的图形导出通过内置 JVM/Batik 完成；仅执行 ``set graphics off`` 不能
    阻止 JVM 初始化 AWT 渲染管线。在没有 DISPLAY 的桌面/CI 环境中，这会导致
    PNG/JPG 导出生成 0 字节文件并返回 r(5100)。必须在 pystata/config 初始化
    之前写入 ``JAVA_TOOL_OPTIONS``，让 Stata 随后启动的 JVM 看到该选项。

    返回值表示本次是否追加了默认选项。若调用方已经明确传入
    ``-Djava.awt.headless=true`` 或 ``false``，不覆盖其选择，便于需要 GUI 的
    本地调试场景显式关闭 headless。
    """
    current = os.environ.get("JAVA_TOOL_OPTIONS", "").strip()
    if _JAVA_HEADLESS_OPTION_RE.search(current):
        return False

    option = "-Djava.awt.headless=true"
    os.environ["JAVA_TOOL_OPTIONS"] = f"{current} {option}".strip()
    return True


# MCP 通过 stdio 工作，本身没有可依赖的 GUI 会话。必须在 import pystata 前
# 设置，避免 Stata 初始化内置 JVM 后再修改环境变量已经太晚。
_ensure_java_headless()

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

# 纯辅助函数/常量的重导出在文件顶部 import 区（from stata_helpers import ...）。
# Stata 返回码 3000 = "无错误但无实质输出"（如 r-class 命令）
STATA_RC_NO_OUTPUT = 3000
# 自定义返回码：StataSO_Execute 崩溃后已自动恢复，命令本身未执行，需重试。
# 区别于 999（崩溃未恢复）与 998（DLL 无响应）。视为非致命，不标记 MCP isError。
STATA_RC_RECOVERED = 997
# _execute_safe/_run_stata_command 对恢复分支使用的稳定提示。除了给调用方看，
# do 文件的 SSC 预安装也用它识别「命令根本没有执行」，不能把这类字符串当成功。
STATA_RECOVERED_NOTICE = "(Stata 已自动恢复，请重试命令)"
# 命令输入最大长度
MAX_COMMAND_LENGTH = 65_536
# 最近一次完整输出的缓存（支持翻页）
_last_output = ""
# 保护 _last_output 读写的独立锁，避免翻页与命令执行串行化
_output_lock = threading.Lock()
# Ping 缓存：避免高频命令的重复心跳开销
_last_ping_time = 0.0
PING_CACHE_SECONDS = 2.0  # 2 秒内跳过重复 ping

# 文件资源注册表：导出工具成功写入文件后在此登记，resource 模板只服务**登记过**
# 的文件。这是资源回传的安全边界 —— `stata-file:///{path*}` 模板会让远程客户端
# 能请求任意 URI，若不查此表就是服务器端任意文件读取原语。文件本身仍留在磁盘，
# 清空注册表只撤销「可经资源接口读取」的能力。
_resource_registry: dict[str, dict] = {}
_resource_lock = threading.Lock()
# 单次资源读取上限：防止把超大文件一次性读进内存撑爆 MCP 传输（资源协议路径）。
_MAX_RESOURCE_READ_BYTES = 16 * 1024 * 1024
# stata_read_file(action="read") 走 base64 工具返回，没有 _run_stata_command 的
# 120K 收口，单独钳制载荷：80KB 文件 → 约 106K base64 字符，贴近传输上限。
# 超限引导用 resources/read（流式二进制，上限 16MB）。
_MAX_TOOL_READ_BYTES = 80 * 1024

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
    1: "已中断（Break）—— 命令执行超过超时上限被看门狗打断",
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
    498: "变量含全部缺失值（reshape 常见：j 变量值非数值时需 options='string'）",
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


def _execute_safe(
    cmd: str,
    timeout: int = 60,
    full_output_path: str | None = None,
    cancel_event: threading.Event | None = None,
) -> tuple:
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
        full_output_path: 透传给 _execute_single，用于 save_output 的完整输出落盘。
        cancel_event: 透传给 _execute_single 的看门狗（后台任务显式取消）。

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
    rc, out = _execute_single(
        cmd, timeout, full_output_path=full_output_path, cancel_event=cancel_event
    )

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
                out += f"\n{STATA_RECOVERED_NOTICE}"
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
    tmpf = None
    try:
        tmpf = sfi.SFIToolkit.getTempFile()
        with open(tmpf, "w", encoding="utf-8") as f:
            f.write(cmd if cmd.endswith("\n") else cmd + "\n")
    except OSError:
        # getTempFile() 通常已经创建了文件；写入失败时也要清理，避免磁盘满、
        # 权限变化等异常在长驻 MCP 进程里累积临时文件。
        _cleanup_temp_block(tmpf)
        raise
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


def _execute_single(
    cmd: str,
    timeout: int = 60,
    full_output_path: str | None = None,
    cancel_event: threading.Event | None = None,
) -> tuple:
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
        full_output_path: 非空时，把**完整**输出（不受 120K 上限裁剪）追加写入
            该文件（二进制模式）。用于 save_output：超限输出的后半段交给文件，
            调用方再把它登记为资源供客户端读取。调用方负责先截断旧文件。
        cancel_event: 非空时，看门狗同时监听该事件 —— 置位即以与超时相同的
            锁内二次确认发出 SetBreak，使「显式取消」与「超时」走同一条安全
            路径，杜绝取消的 break 晚到被下一条命令消费。

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
        # 等待 完成 / 超时 / 取消 三者竞态，50ms 轮询粒度。超时与取消都走锁内
        # 二次确认 —— 确认 exec_done 未置位才 break，于是 break 永远不会晚到
        # 被下一次 StataSO_Execute（无关命令）消费。
        deadline = time.time() + timeout
        while not exec_done.wait(timeout=0.05):
            if cancel_event is not None and cancel_event.is_set():
                with break_guard:
                    if exec_done.is_set():
                        return
                    logger.info("Stata command cancelled, issuing break: %s", cmd[:80])
                    did_break = True
                    _set_break()
                return
            if time.time() >= deadline:
                with break_guard:
                    # 锁内二次确认：主线程置位 exec_done 同样要拿这把锁，于是
                    # 「确认未完成」与「发出 break」之间不可能再插入命令的完成。
                    if exec_done.is_set():
                        return
                    logger.warning(
                        "Stata command timed out (>%ss), issuing break: %s", timeout, cmd[:80]
                    )
                    # 先置位再 break：主线程要拿到锁才能往下走，因此不会读到
                    # 「break 已发出但 did_break 仍为 False」的中间态 —— 那会让
                    # 它既不清 break 残渣，也不追加超时说明，只看到一个通用 rc=1。
                    did_break = True
                    _set_break()
                return

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
    # full_output_path 非空时把**完整**输出（不受 120K 上限裁剪）同步落盘，
    # 供 save_output 场景将超限输出登记为文件资源回传。调用方负责先截断旧文件。
    full_fh = None
    if full_output_path:
        try:
            full_fh = open(full_output_path, "ab")
        except OSError as e:
            logger.warning("无法打开完整输出文件 %s: %s", full_output_path, e)
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
            # 完整输出先落盘（不受上限约束），内存缓冲仍按 120K 裁剪
            if full_fh is not None:
                full_fh.write(out.encode("utf-8", errors="replace"))
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
            if full_fh is not None:
                full_fh.write(tail.encode("utf-8", errors="replace"))
            room = MAX_OUTPUT_CHARS - total_len
            if len(tail) >= room:
                out_buf.write(tail[:room])
                out_buf.write(_TRUNCATION_NOTICE)
                total_len = MAX_OUTPUT_CHARS
            else:
                out_buf.write(tail)
                total_len += len(tail)

    # 看门狗中断后 Stata 返回的是通用错误码（实测 rc=1「未指定的错误」），
    # 单看返回码会让调用方去排查命令语法，故显式点明中断原因与可行的下一步。
    # 区分超时与取消：取消（cancel_event 置位）不伪造「超时、调大 timeout」的
    # 误导指引（实战审查发现被取消的后台任务会拿到假的超时建议）。
    if did_break:
        if cancel_event is not None and cancel_event.is_set():
            out_buf.write("\n(命令已被请求取消。)")
        else:
            out_buf.write(
                f"\n(命令执行超过 {timeout}s 上限已被中断。"
                "如属正常的长耗时任务，请显式传入更大的 timeout；"
                "若命令可能在 headless 环境挂起，请改用更轻量的写法。)"
            )

    if full_fh is not None:
        full_fh.close()

    return rc, out_buf.getvalue()


def _format_error(rc: int, block: str, out: str) -> str:
    """格式化 Stata 错误信息，包含返回码释义。"""
    msg = STATA_RC_MESSAGES.get(rc, f"未知返回码({rc})")
    if rc == 459:
        # r(459) 同时用于 isid/duplicates 与 xtreg 未先 xtset 两类完全不同的
        # 前提错误。后者若继续套用「唯一识别」会把用户引向错误修复方向；优先
        # 使用命令/原文中的面板线索做上下文释义，仍保留 Stata 原文在下一行。
        context = f"{block}\n{out}".lower()
        if re.search(r"\bxtreg\b", block.lower()) or "panelvar" in context or "xtset" in context:
            msg = "面板/时序结构未设置（请先 xtset/tsset）"
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


# 自由文本数据路径审计：从命令块里抽取「读/写本地文件」的引号路径并做沙箱校验。
# 只审计**带引号**的路径（Stata 对含空格路径的标准写法，无歧义）；裸未加引号的
# 单 token 可能是 varlist/选项，跳过以免误伤。webuse/sysuse 联网/本地库加载不含
# 用户本地路径，不审计。含宏（$ `）的路径无法静态解析，跳过（fail-open）。
# 官方最小缩写也纳入（实战审查发现全写匹配可被 `sav`/`imp`/`cop` 等缩写绕过；
# 精确 token 匹配，`lowess` 不会被误当 `lo`）。
_PATH_USING_COMMANDS = frozenset(
    {"import", "imp", "insheet", "ins", "infile", "inf", "infix", "infi",
     "merge", "mer", "append", "app", "joinby", "join",
     "export", "exp", "log", "lo", "graph", "gr",
     "filefilter", "fif", "translate", "tra", "copy", "cop"}
)
_PATH_ARG_COMMANDS = frozenset(
    {"use", "us", "save", "sav", "do", "run", "include", "inc", "cd",
     "mkdir", "rmdir", "erase"}
)


def _audit_block_paths(block: str) -> str | None:
    """对单条命令块做自由文本路径审计；返回错误文本或 None。

    调用者必须已持有 ``_stata_lock``（内部用 Stata cwd 解析相对路径）。
    仅当 STATA_ALLOWED_ROOTS 配置时由 _run_stata_command 调用。
    """
    # 用轻量前缀剥离：capture/quietly/by g: 等已知前缀不影响命令身份，
    # 但 `merge 1:1` 的匹配规格**不是**前缀 —— 重型 _strip_command_prefixes
    # 会把它当冒号前缀剥掉，丢失 merge 命令名导致漏审。
    line = _light_strip_prefixes(block).strip()
    head = line.split(None, 1)
    if not head:
        return None
    command = head[0].lower().split(".", 1)[0]  # 去掉 graph export 的子命令前的部分
    rest = head[1] if len(head) > 1 else ""

    # 命令名本身可能含子命令（graph export / import excel / log using）：
    # 先把「命令 + 首个子命令」拼回，便于匹配 _PATH_USING_COMMANDS
    sub_tokens = rest.split()
    cmd_key = command
    if sub_tokens and sub_tokens[0] in ("export", "excel", "delimited", "sas", "spss",
                                         "dbase", "parquet", "using", "save", "append", "replace"):
        cmd_key = f"{command} {sub_tokens[0].lower()}"
    base_cmd = cmd_key.split()[0]

    candidates: list[str] = []
    if base_cmd in _PATH_USING_COMMANDS:
        # using "path" 形态
        for m in re.finditer(r"\busing\s*\"([^\"]+)\"", line, re.IGNORECASE):
            candidates.append(m.group(1))
        # 无 using 的引号路径（graph export "x.png" / copy "a" "b"）
        # 无 using 的引号路径（graph export "x.png" / copy "a" "b" /
        # export delimited "x.csv"）—— 含官方缩写
        if base_cmd in (
            "graph", "gr", "copy", "cop", "filefilter", "fif",
            "translate", "tra", "export", "exp",
        ):
            candidates.extend(re.findall(r"\"([^\"]+)\"", line))
    elif base_cmd in _PATH_ARG_COMMANDS:
        m = re.search(r"\"([^\"]+)\"", line)
        if m:
            candidates.append(m.group(1))
    if not candidates:
        return None

    stata_cwd = _get_stata_cwd_locked() if _stata_lock.locked() else None
    for raw in candidates:
        if "$" in raw or "`" in raw or "'" in raw:
            continue  # 宏路径无法静态解析，fail-open
        if "://" in raw:
            continue  # URL（webuse/net 等），本地路径审计不适用
        if not raw.strip():
            continue
        if os.path.isabs(raw):
            abs_path = _normalize_path(raw)
        elif stata_cwd:
            abs_path = _normalize_path(os.path.join(stata_cwd, raw))
        else:
            abs_path = _normalize_path(raw)
        if err := _check_abs_path_safety(abs_path):
            return (
                f"错误: 自由文本命令引用了沙箱外路径 —— {raw}\n"
                f"  {err}\n"
                "自由文本命令（stata_run/stata_run_do_file）的路径同样受 "
                "STATA_ALLOWED_ROOTS 约束；请改用白名单内的路径，"
                "或经结构化工具（stata_use_dataset/stata_save_dataset 等）传入。"
            )
    return None


def _run_stata_command(
    cmd: str,
    page: int = 1,
    timeout: int = 60,
    require_file: str | None = None,
    full_output_path: str | None = None,
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
        full_output_path: 非空时，先把该文件截断，再把本次命令链的**完整**输出
            （不受 120K 上限裁剪）逐块追加写入。供 save_output 场景使用。

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
    # save_output 语义：覆盖式写入。截断旧文件由本函数负责，各块以追加方式落盘。
    if full_output_path:
        try:
            with open(full_output_path, "wb"):
                pass
        except OSError as e:
            return _make_error_result(
                f"错误: 无法写入完整输出文件 {full_output_path}: {e}"
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

        # 自由文本路径审计：仅当 STATA_ALLOWED_ROOTS 已配置时启用。结构化工具的
        # 路径参数已在各自入口过 _validate_path（含沙箱预检），此处针对 stata_run /
        # stata_run_do_file / stata_background 的自由文本命令 —— 配置白名单后
        # `stata_run('use "越界路径"')` 此前照常执行（CLAUDE.md 文档化的缺口），
        # 现在在锁内用真实 Stata cwd 权威校验。默认未配置白名单时无操作（向后兼容）。
        if _init_allowed_roots():
            for block in blocks:
                if err := _audit_block_paths(block):
                    return _make_error_result(err)

        all_buf = io.StringIO()
        hwritten = False
        had_error = False
        for index, block in enumerate(blocks):
            try:
                rc, out = _execute_safe(block, timeout, full_output_path=full_output_path)

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
            # 去重截断提示：单块超限时收集层已在块内注入 _TRUNCATION_NOTICE。
            # 但要收口到 120K 硬上限，并保留提示后的关键信息（如看门狗超时说明）——
            # 旧实现 `full[:idx] + notice` 在「截断块不是第一个」时 idx>120K 导致
            # 超过硬上限（实测 170K），且把提示后的超时指引整条丢弃。
            idx = full.find(_TRUNCATION_NOTICE)
            if idx != -1:
                after = full[idx + len(_TRUNCATION_NOTICE):]  # 提示后内容（超时说明等）
                room_for_head = MAX_OUTPUT_CHARS - len(_TRUNCATION_NOTICE) - len(after)
                full = full[: max(0, room_for_head)] + _TRUNCATION_NOTICE + after
            else:
                full = full[:MAX_OUTPUT_CHARS] + _TRUNCATION_NOTICE
        with _output_lock:
            _last_output = full

        # 若任何 block 出错，返回 isError=true
        if had_error:
            err_text = full
            if _TRUNCATION_NOTICE in full:
                # 错误路径不走 _paginate（无截断页首横幅），把截断提示前置
                err_text = "(输出已被 120K 上限截断，后半段已丢弃)\n" + err_text
            return _make_error_result(err_text)

        # 自动分页：仅当是单条命令且输出超过阈值
        # truncated 标志：文本已被 120K 上限截断时，让分页页首给出明确提示
        # （截断原文在文本末尾，翻到最后一页才看得到，首页会误读为完整总量）。
        truncated = _TRUNCATION_NOTICE in full
        if len(blocks) == 1 and len(full) > PAGE_SIZE:
            return _paginate(full, page, truncated=truncated)
        elif len(blocks) > 1 and len(full) > PAGE_SIZE * 3:
            # 多命令输出也分页
            return _paginate(full, page, truncated=truncated)

        return full








def _register_resource(path: str, source: str) -> str | None:
    """登记一个**已成功写入**的文件为可读资源；返回 None=成功，否则错误文本。

    这是资源回传的唯一入口：resource 模板 `stata-file:///{path*}` 只服务登记过的
    文件，远程客户端无法借此读取任意磁盘路径。导出工具在确认文件真正被写入后
    调用（图形/etable 以 mtime 判定，见各自实现）。
    """
    abs_path = _normalize_path(path)
    if not os.path.isfile(abs_path):
        return f"错误: 文件不存在，无法登记为资源 — {abs_path}"
    try:
        size = os.path.getsize(abs_path)
    except OSError as e:
        return f"错误: 无法读取文件大小 — {abs_path}: {e}"
    with _resource_lock:
        existing = _resource_registry.get(abs_path)
        # 重复登记保留原始来源（实战发现：已由 stata_save_dataset 登记的文件再
        # 经 stata_register_file 登记，来源被覆盖成后者的工具名，元数据失真）。
        source = existing["source"] if existing else source
    entry = {
        "path": abs_path,
        "source": source,
        "mime": _resource_mime(abs_path),
        "size": size,
        "ts": time.time(),
        "uri": _resource_uri(abs_path),
    }
    with _resource_lock:
        _resource_registry[abs_path] = entry
    return None


def _resource_lookup(path: str) -> dict | None:
    """按路径查注册表。

    **不二次 unquote**：资源模板的 ``{path*}`` 捕获值已被 fastmcp 解码过一次
    （match_uri_template 内部 unquote），这里再解会把含字面 ``%xx`` 的文件名
    （如 ``a%20b.csv``）二次解码成不同的路径，查表 miss。调用方传入的都是
    文件系统路径或已解码的模板路径，无需再解。
    """
    normalized = _normalize_path(path)
    with _resource_lock:
        return _resource_registry.get(normalized)


def _clear_resources() -> None:
    """清空注册表（stata_clear 全清会话时调用）。

    文件本身仍留在磁盘，撤销的是「可经资源接口读取」的能力。
    """
    with _resource_lock:
        _resource_registry.clear()






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
# 后台任务管理
# =============================================================================
# Stata DLL 非线程安全，所有命令由 _stata_lock 串行化 —— 后台任务同样要持这把锁
# 才能执行。因此「后台」改变的不是并发性（同一时刻仍只有一条命令在跑），而是
# **交互模型**：提交方立即拿到 task_id 返回，不必在 MCP 请求里阻塞数分钟；进度
# 可轮询、任务可显式取消。长任务（大循环、复杂回归、联网下载）不再受单个请求
# 的默认 60s 看门狗钳制。


@dataclass
class _BackgroundTask:
    task_id: str
    command: str
    timeout: int
    status: str = "queued"          # queued | running | done | failed | cancelled
    blocks: list = field(default_factory=list)
    block_index: int = -1           # 当前执行到的块下标（-1 = 未开始）
    current_block: str = ""
    result: str = ""
    is_error: bool = False
    cancel_requested: bool = False
    cancel_event: threading.Event = field(default_factory=threading.Event)
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None


_bg_tasks: dict[str, _BackgroundTask] = {}
_bg_lock = threading.Lock()
_MAX_BG_TASKS = 10
_BG_TIMEOUT_MIN = 10
_BG_TIMEOUT_MAX = 3600


def _prune_bg_tasks() -> None:
    """裁剪已结束任务：总数超上限时按创建时间淘汰最旧的已结束任务。"""
    with _bg_lock:
        if len(_bg_tasks) <= _MAX_BG_TASKS:
            return
        finished = [
            t for t in _bg_tasks.values()
            if t.status in ("done", "failed", "cancelled")
        ]
        finished.sort(key=lambda t: t.created_at)
        for t in finished[: len(_bg_tasks) - _MAX_BG_TASKS]:
            _bg_tasks.pop(t.task_id, None)


def _bg_worker(task: _BackgroundTask) -> None:
    """后台执行线程：持 _stata_lock 串行执行全部块，逐块上报进度并响应取消。

    取消语义：``_bg_cancel`` 置位 ``cancel_event``（连同 ``cancel_requested``）。
    任务正卡在 StataSO_Execute 里时，_execute_single 的看门狗观察到事件，以与
    超时相同的锁内二次确认发出 SetBreak —— 因此取消的 break **不可能晚到被
    下一条无关命令消费**。块与块之间靠命令返回后检查取消标志终止。
    """
    with _stata_lock:
        task.status = "running"
        buf = io.StringIO()
        hwritten = False
        had_error = False
        try:
            task.blocks = _parse_command_blocks(task.command)
        except UnbalancedBlockError as e:
            task.status = "failed"
            task.result = f"错误: {e}"
            task.is_error = True
            task.finished_at = time.time()
            return
        if not task.blocks:
            task.status = "done"
            task.result = "(无有效命令)"
            task.finished_at = time.time()
            return

        try:
            for index, block in enumerate(task.blocks):
                # 块间取消检查：不再执行剩余块
                if task.cancel_requested:
                    task.status = "cancelled"
                    task.result = buf.getvalue() + "\n(任务已取消，尚未执行剩余命令)"
                    task.finished_at = time.time()
                    return
                # 后台自由文本同样受路径审计（与 _run_stata_command 一致，仅配置
                # 白名单时启用）。实战审查发现此前后台块完全跳过审计。
                if _init_allowed_roots():
                    if err := _audit_block_paths(block):
                        task.status = "failed"
                        task.result = err
                        task.is_error = True
                        task.finished_at = time.time()
                        return
                task.block_index = index
                task.current_block = block
                rc, out = _execute_safe(
                    block, task.timeout, cancel_event=task.cancel_event
                )

                # 取消请求可能在看门狗窗口内到达（事件触发 SetBreak）：以取消为准
                if task.cancel_requested:
                    # 保留已执行部分的输出（含被中断块的产出），并明确是用户取消
                    # 而非失败 —— 实战发现取消前打印的内容曾被整块丢弃。
                    if out.strip():
                        if hwritten:
                            buf.write("\n")
                        buf.write(out.strip())
                        hwritten = True
                    task.status = "cancelled"
                    task.result = buf.getvalue() + (
                        f"\n(任务已取消，已执行 {index + 1}/{len(task.blocks)} 块)"
                    )
                    task.finished_at = time.time()
                    return
                if rc == 998:
                    had_error = True
                    if hwritten:
                        buf.write("\n")
                    buf.write(out)
                    hwritten = True
                    break
                if rc == STATA_RC_RECOVERED:
                    if hwritten:
                        buf.write("\n")
                    buf.write(out.strip())
                    hwritten = True
                    break
                if rc != 0 and rc != STATA_RC_NO_OUTPUT:
                    had_error = True
                    if hwritten:
                        buf.write("\n")
                    buf.write(_format_error(rc, block, out))
                    hwritten = True
                    # 与 _run_stata_command 的首错即停一致：提示剩余块被跳过 + capture 逃生
                    remaining = len(task.blocks) - index - 1
                    if remaining > 0:
                        buf.write(
                            f"\n[已跳过后续 {remaining} 条命令]"
                            " 出错后中止，避免在陈旧/半截状态上继续执行。"
                            " 如需忽略该错误继续，请在该命令前加 Stata 的 capture 前缀。"
                        )
                    break
                elif out.strip():
                    if hwritten:
                        buf.write("\n")
                    buf.write(out.strip())
                    hwritten = True
        except Exception:
            # 兜底：_execute_safe 内任何未预期异常都不允许任务永久停在 running
            logger.exception("后台任务执行异常: %s", task.task_id)
            task.status = "failed"
            task.result = buf.getvalue() + "\n(后台任务执行异常，详见服务器日志)"
            task.is_error = True
            task.finished_at = time.time()
            return

        # 取消可能在最后一块完成到置位状态之间到达：以取消为准
        if task.cancel_requested:
            task.status = "cancelled"
            task.result = buf.getvalue() + "\n(任务已取消)"
            task.finished_at = time.time()
            return

        full = buf.getvalue()
        if len(full) > MAX_OUTPUT_CHARS:
            idx = full.find(_TRUNCATION_NOTICE)
            if idx != -1:
                after = full[idx + len(_TRUNCATION_NOTICE):]
                room_for_head = MAX_OUTPUT_CHARS - len(_TRUNCATION_NOTICE) - len(after)
                full = full[: max(0, room_for_head)] + _TRUNCATION_NOTICE + after
            else:
                full = full[:MAX_OUTPUT_CHARS] + _TRUNCATION_NOTICE
        task.result = full
        task.is_error = had_error
        task.status = "failed" if had_error else "done"
        task.finished_at = time.time()


def _submit_bg_task(command: str, timeout: int) -> str:
    """提交后台任务（入口已完成危险前缀/长度校验），返回 task_id。

    未结束任务（queued/running）计数达上限时拒绝提交 —— 单 DLL 下同时只能有
    一个在跑，排队的任务多了只会累积 daemon 线程，毫无吞吐收益。
    """
    with _bg_lock:
        active = sum(
            1 for t in _bg_tasks.values() if t.status in ("queued", "running")
        )
        if active >= _MAX_BG_TASKS:
            raise ValueError(
                f"错误: 已有 {active} 个后台任务在排队/运行（上限 {_MAX_BG_TASKS}）。"
                "请先等任务结束、取消或调用 stata_clear 后再提交。"
            )
        task_id = uuid.uuid4().hex[:12]
        task = _BackgroundTask(task_id=task_id, command=command, timeout=timeout)
        _bg_tasks[task_id] = task
    _prune_bg_tasks()
    threading.Thread(
        target=_bg_worker, args=(task,), daemon=True, name=f"stata-bg-{task_id}"
    ).start()
    return task_id


def _bg_task(task_id: str) -> _BackgroundTask | None:
    with _bg_lock:
        return _bg_tasks.get(task_id)


def _bg_cancel(task_id: str) -> tuple[bool, str]:
    """请求取消后台任务；返回 (是否找到, 提示)。

    取消通过置位 ``cancel_event`` 生效：任务正卡在 StataSO_Execute 里时，由
    ``_execute_single`` 的看门狗观察到事件并以锁内二次确认发出 SetBreak ——
    取消方**不再直接跨线程 SetBreak**，从而根除「晚到 break 打断下一条无辜
    命令」的历史缺陷。
    """
    task = _bg_task(task_id)
    if task is None:
        return False, f"错误: 未找到任务 {task_id}"
    if task.status in ("done", "failed", "cancelled"):
        return True, f"任务 {task_id} 已结束（{task.status}），无需取消"
    task.cancel_requested = True
    task.cancel_event.set()
    return True, f"已请求取消任务 {task_id}"


def _bg_status_text(task: _BackgroundTask) -> str:
    """渲染单任务状态文本。"""
    lines = [
        f"任务: {task.task_id}",
        f"状态: {task.status}",
    ]
    if task.status == "queued" or not task.blocks:
        lines.append("进度: 待开始")
    else:
        lines.append(f"进度: {task.block_index + 1}/{len(task.blocks)} 块")
    if task.status == "running" and task.current_block:
        lines.append(f"当前命令: {task.current_block[:100]}")
    lines.append(f"提交于: {time.strftime('%H:%M:%S', time.localtime(task.created_at))}")
    if task.finished_at:
        lines.append(f"耗时: {task.finished_at - task.created_at:.1f}s")
    return "\n".join(lines)


# =============================================================================
# MCP 工具 — 核心执行
# =============================================================================


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def stata_run(
    command: str,
    page: int = 1,
    timeout: int = 60,
    save_output: str = "",
    compact: bool = False,
) -> str | ToolResult:
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

    **路径沙箱覆盖自由文本命令（配置时）**：``STATA_ALLOWED_ROOTS`` 配置后，
    本工具命令里的**引号路径**（``use "…"`` / ``save "…"`` / ``import … using "…"`` /
    ``graph export "…"`` 等数据命令）会做锁内权威校验 —— 越界路径被拒。这是刻意
    的收紧：此前自由文本路径不在审计范围（``stata_run('use "越界路径"')`` 照常
    执行），是文档化缺口。局限：① 只审**引号**路径（裸单 token 可能是 varlist，
    跳过以免误伤）；② 含宏的路径（``$mydir`` / 反引号）无法静态解析，fail-open；
    ③ 未配置白名单时不启用（向后兼容）。需要强制隔离时仍建议在操作系统层面
    限制本进程可访问的目录。

    使用示例：
    - 单条命令: "summarize mpg"
    - 多条命令: "sysuse auto, clear\\nsummarize mpg\\ntabulate foreign"

    Args:
        command: Stata 命令，多条命令用 \\n 分隔。
        page: 页码（1-based），仅对单条命令有效。默认 1。
        timeout: 命令超时秒数（默认 60，最长 1800）。
        save_output: 非空时，把本次命令链的**完整**输出（不受 120K 上限裁剪）
            写入该路径（**覆盖式**，已存在文件会被截断）并登记为文件资源，
            返回信息附资源 URI —— 超大输出的后半段在内存里被截断，但完整文本
            可经文件资源取回。示例：save_output="outputs/big_list.txt"。
        compact: 是否压缩输出（默认 False）：删掉 ``(N real changes made)`` 等
            统计计数行、折叠连续空行，节省 token。结果表与错误文本绝不动。

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

    if not save_output:
        return _apply_compact(
            _run_stata_command(command, page, timeout=safe_timeout), compact
        )

    # save_output：完整输出落盘 + 登记为资源。路径经沙箱校验；覆盖式写入。
    if err := _validate_path(save_output):
        return _result_or_error(err)
    out_path = _normalize_path(save_output)
    # 记录调用前状态：_run_stata_command 对空命令/超长命令会**早退**（不截断文件），
    # 此时 mtime 不变 —— 不登记陈旧文件，也不附「完整输出已保存」的误导说明。
    before_ns = _mtime_ns(out_path) if os.path.isfile(out_path) else None
    result = _run_stata_command(
        command, page, timeout=safe_timeout, full_output_path=out_path
    )
    written = _file_written_since(out_path, before_ns)
    note = f"\n(完整输出已保存: {out_path} | 资源 URI: {_resource_uri(out_path)})"
    if isinstance(result, ToolResult) and result.is_error and not written:
        # 早退：文件未被本次调用触碰，不登记陈旧文件
        return result
    reg_err = _register_resource(out_path, "stata_run save_output")
    if reg_err is not None:
        note = f"\n(完整输出已保存: {out_path}，但登记为资源失败: {reg_err})"
    return _apply_compact(_append_text(result, note), compact)


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


# 不受控的第三方包安装命令（行首，允许 qui/cap/noi 前缀）。这些不经过受控的
# ssc install 预装路径（_extract_ssc_installs），也未经来源白名单 —— do 文件里
# 出现即整体拒绝并重定向到 stata_install_package。net install 的第三方 URL 是
# 注入面，adoupdate/update all 会改写本机包/Stata 状态。
_UNMANAGED_PKG_RE = re.compile(
    r"(?:net|github)\s+ins(?:t(?:a?l?l?)?)?\b|adou(?:pdate)?\b|update\s+all\b",
    re.IGNORECASE,
)


def _flag_unmanaged_package_commands(do_text: str) -> list[str]:
    """扫描 do 文件里的不受控安装命令，返回违规行列表（空=安全）。

    先剥已知前缀（capture/quietly/by/version 冒号形态），再匹配含官方缩写
    （net inst / github ins / adou）的安装命令 —— 实战审查发现全写匹配可被
    缩写与 `version 15: net install` 前缀绕过。
    """
    blocked: list[str] = []
    for raw in do_text.split("\n"):
        if _UNMANAGED_PKG_RE.match(_light_strip_prefixes(raw).strip()):
            blocked.append(raw.strip())
    return blocked


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
        recovered = _is_recovered_result(res)
        ok = not isinstance(res, ToolResult) and not recovered
        if recovered:
            report.append(f"  · {pkg}: 安装未完成（Stata 已自动恢复，请重试）")
        else:
            report.append(f"  · {pkg}: {'已安装' if ok else '安装失败（详见下方输出）'}")
        if not ok:
            report.append("    " + _result_text_inline(res))
        if recovered:
            # 当前 install 命令没有执行，后续包安装/脚本主体都不应继续假设
            # 会话状态可靠；让 do 文件入口返回可重试的明确错误。
            break
    return report


def _result_text(value) -> str:
    """提取 str / ToolResult 的文本，保留换行。"""
    if isinstance(value, ToolResult):
        try:
            return value.content[0].text.strip()
        except (AttributeError, IndexError):
            return str(value)
    return str(value).strip()


def _is_recovered_result(value) -> bool:
    """判断命令结果是否是「Stata 已恢复、原命令未执行」的提示。"""
    return STATA_RECOVERED_NOTICE in _result_text(value)


def _result_text_inline(value) -> str:
    """提取 str / ToolResult 的文本并单行化，**仅**用于并入单行的报告条目。

    不要用它包装完整的命令输出：换行变 " | " 会把 Stata 的错误上下文、表格与
    行号提示压成一条巨型单行。需要保留格式时用 ``_result_text``。
    """
    return _result_text(value).replace("\n", " | ")


def _append_text(result: str | ToolResult, extra: str) -> str | ToolResult:
    """给 str 或 ToolResult 追加文本，保持 is_error 标志不变。

    用于导出工具在成功提示后补充「已登记为资源」等信息：ToolResult 需重建
    （FastMCP 的 ToolResult 是数据类，不能原地改 content）。
    """
    if isinstance(result, ToolResult):
        return ToolResult(
            content=_result_text(result) + extra, is_error=result.is_error
        )
    return result + extra


# compact 模式去掉的**结构明确无信息量**的行：Stata 的统计计数行
# （(N real changes made) 等）。这类行只报「改了几条」，对结果解读无用却每条
# 命令都打一行。只删计数行 + 压缩空行，绝不碰结果表/错误文本 —— 误差保留定位。
_COMPACT_COUNT_LINE_RE = re.compile(
    r"^\(\d+ (?:real changes made|observations? deleted|missing values generated"
    r"|observations? added|observations? changed)\)\s*$",
    re.MULTILINE,
)


def _compact_output(text: str) -> str:
    """压缩输出：删统计计数行、折叠连续空行。opt-in，绝不删结果表与错误行。"""
    text = _COMPACT_COUNT_LINE_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _apply_compact(result: str | ToolResult, compact: bool) -> str | ToolResult:
    """按需对工具结果应用 compact 压缩（保留 is_error）。"""
    if not compact:
        return result
    text = _result_text(result)
    compacted = _compact_output(text)
    if isinstance(result, ToolResult):
        return ToolResult(content=compacted, is_error=result.is_error)
    return compacted


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def stata_run_do_file(
    filepath: str, timeout: int = 300, compact: bool = False
) -> str | ToolResult:
    """执行一个 Stata .do 文件并返回全部输出。

    .do 文件是 Stata 的批处理脚本。此工具会执行指定路径的 .do 文件。

    **执行前自动拆出 `ssc install`**：do 文件常在开头写 `ssc install foo`，内联
    执行会让整段脚本卡在网络请求上。本工具先扫描并把这些行移出：已安装的包直接
    跳过（不重复联网），缺失的包各自单独安装（带 timeout，超时可干净中断），
    然后运行**去掉安装行**的脚本主体（安装行改成注释，行号不变）。文件里没有
    `ssc install` 时，脚本原样执行、行为完全不变。

    **不受控的第三方包安装会被拒绝**：`net install` / `github install` /
    `adoupdate` / `update all` 不经过受控预装路径，do 文件里出现即整体拒绝并
    引导改用 `stata_install_package`。

    compact=True 时压缩输出：删掉 `(N real changes made)` 等统计计数行、折叠
    连续空行，节省 token。结果表与错误文本绝不动（错误上下文完整保留）。

    注意：do 文件由 Stata 自行解析，**不经过** ``stata_run`` 的危险命令前缀
    护栏。只执行你信任的 do 文件。

    Args:
        filepath: .do 文件的绝对路径。
        timeout: 超时秒数（默认 300，范围 10–1800）。既是脚本主体的超时，也是
            每个拆出的包安装的超时。跑批量建模或大数据清洗时请显式调大。
        compact: 是否压缩输出（默认 False）。

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
        return _apply_compact(
            _run_stata_command(
                f'do "{normalized}"', require_file=filepath, timeout=safe_timeout
            ),
            compact,
        )

    # 不受控的第三方包安装（net/github install、adoupdate、update all）整体拒绝：
    # 不走受控的 ssc 预装路径，也未经来源白名单。redirect 到 stata_install_package。
    blocked_pkgs = _flag_unmanaged_package_commands(do_text)
    if blocked_pkgs:
        return _make_error_result(
            "错误: do 文件包含不受控的包安装命令，已拒绝执行：\n"
            + "\n".join(f"  {ln}" for ln in blocked_pkgs[:5])
            + ("\n  …" if len(blocked_pkgs) > 5 else "")
            + "\n请改用受控的 stata_install_package（ssc 或完整 https from() URL，"
            "带 timeout）：\n"
            '  stata_install_package("包名", source="ssc", timeout=120)'
        )

    # do 文件内容经 Stata 直接执行，**不经过** stata_run 的入口护栏 —— 真机确认
    # do 文件里的 `shell echo …` / `!rm` 会真实执行主机命令，且 shell 子进程的输出
    # 直接写进程 stdout（MCP 下会污染 stdio 通道）。对 do 文件内容同样施加护栏：
    # 用**解析后**块检查（挡 `sh/*x*/ell` 注释混淆），并拒宏间接调用。合法 do 文件
    # （建模/清洗/绘图）不含这些命令，不受影响。
    if reason := _validate_command_blocks(do_text):
        return _make_error_result(
            "错误: do 文件包含危险命令（可执行主机系统/删除文件代码），已拒绝执行：\n"
            f"  {reason}\n"
            "Stata MCP 不执行含 shell-out / 文件销毁 / 代码执行命令的 do 文件。\n"
            "如确有必要，请通过操作系统或 Stata 界面直接执行。"
        )
    if reason := _flag_macro_obfuscation(do_text):
        return _make_error_result(
            "错误: do 文件通过宏间接调用危险命令，已拒绝执行：\n"
            f"  {reason}"
        )

    # do 文件**内容**里的数据命令路径同样受沙箱约束（配置 STATA_ALLOWED_ROOTS 时）。
    # 此前只审计外层 `do "path"`，内容里的 `use "越界"` 完全漏审（实战审查发现）。
    # 锁内解析（相对路径按 Stata cwd 权威校验）；逐行审计（块内命令也逐行可判）。
    if _init_allowed_roots():
        with _stata_lock:
            for do_line in do_text.split("\n"):
                if err := _audit_block_paths(do_line):
                    return _make_error_result(
                        "错误: do 文件内容引用了沙箱外路径，已拒绝执行：\n"
                        f"  {do_line.strip()}\n  {err}"
                    )

    cleaned, installs = _extract_ssc_installs(do_text)
    if not installs:
        # 无 ssc install：原样执行，走标准 require_file 权威路径，行为不变。
        return _apply_compact(
            _run_stata_command(
                f'do "{normalized}"', require_file=filepath, timeout=safe_timeout
            ),
            compact,
        )

    report = _prepare_ssc_installs(installs, timeout=safe_timeout)
    header = (
        "已在执行前处理 do 文件中的 ssc install：\n"
        + "\n".join(report)
        + "\n"
        + "-" * 40
        + "\n"
    )
    if any(
        marker in line
        for line in report
        for marker in ("安装失败", "安装未完成", "探测失败")
    ):
        # 预安装失败时，清理后的主体必然缺少所需包；继续执行只会制造一串
        # 次生错误（更糟的是把旧会话状态当成成功）。把原始安装输出保留给
        # 调用方，并要求修复后整体重试。
        return _make_error_result(
            header + "脚本主体未执行：上述 SSC 安装未完成，请修复后重试。"
        )

    # 清理后的脚本写入 Stata 临时 do 文件执行（临时文件由 Stata 会话末清理，
    # 且 _cleanup 不适用于 do；此处即用即删）。
    tmpf = None
    try:
        tmpf = sfi.SFIToolkit.getTempFile()
        with open(tmpf, "w", encoding="utf-8") as f:
            f.write(cleaned if cleaned.endswith("\n") else cleaned + "\n")
    except OSError as e:
        _cleanup_temp_block(tmpf)
        return _make_error_result(f"错误: 无法写入清理后的临时 do 文件: {e}")

    try:
        result = _run_stata_command(f'do "{tmpf}"', timeout=safe_timeout)
    finally:
        _cleanup_temp_block(tmpf)

    if isinstance(result, ToolResult):
        # 保留原始换行：_result_text_inline 是为并入安装**报告行**设计的，套在
        # 可达 120K 字符的 do 文件完整输出上会把 Stata 的错误上下文、表格、行号
        # 提示压成一条巨型单行 —— 同一个 do 文件只要不含 ssc install 就走原路径、
        # 格式完好，一行 ssc install 不该改变错误报告的可读性。
        body = _result_text(result)
        if compact:
            body = _compact_output(body)
        return _make_error_result(header + body)
    if compact:
        result = _compact_output(result)
    return header + result


# =============================================================================
# MCP 工具 — 统计分析 (readOnlyHint=True)
# =============================================================================




# =============================================================================
# MCP 工具 — 图形 (readOnlyHint=True)
# =============================================================================



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
    # 若 stat 恰好遇到替换竞态，但文件仍存在，保守地认为它被写过；若文件
    # 已经被删除，则必须判定失败，不能让导出工具回报「已导出 大小未知」。
    return os.path.isfile(path) if now_ns is None else now_ns != before_ns


def _file_is_nonempty(path: str) -> bool:
    """判断导出产物是否是一个可用的非空普通文件。"""
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


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
    return _paginate(cached, page, ps, truncated=_TRUNCATION_NOTICE in cached)


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
# MCP 资源 — 文件二进制回传
# =============================================================================
# 导出工具（stata_graph / stata_export_excel / stata_etable /
# stata_export_delimited / stata_save_dataset / stata_run save_output）成功写入
# 文件后调用 _register_resource 登记。MCP resource 模板 `stata-file:///{path*}`
# 只服务登记过的文件 —— 远程客户端可经标准的 resources/read 取回图表、Excel、
# CSV、dta 的二进制内容，而不只是路径字符串。


def _read_registered_file(path: str) -> tuple[bytes | None, dict | None, str | None]:
    """读取已登记文件的内容；返回 (data, entry, error)，data/entry 与 error 互斥。

    这是资源读取的统一入口，资源模板与 stata_read_file 共用：先查注册表（安全
    边界），再校验大小上限，最后读二进制。
    """
    entry = _resource_lookup(path)
    if entry is None:
        return None, None, (
            f"错误: 文件未登记为可读资源: {unquote(path)}。\n"
            "  · 导出工具（stata_graph/stata_export_*/stata_etable/"
            "stata_save_dataset/stata_run save_output）成功后会登记\n"
            "  · 已有文件可用 stata_register_file 显式登记"
        )
    abs_path = entry["path"]
    try:
        size = os.path.getsize(abs_path)
    except OSError as e:
        return None, entry, f"错误: 无法读取文件大小: {e}"
    if size > _MAX_RESOURCE_READ_BYTES:
        return None, entry, (
            f"错误: 文件过大（{size} 字节，上限 {_MAX_RESOURCE_READ_BYTES}）。"
            "请缩小文件，或仍经 resources/read 读取（流式）。"
        )
    try:
        with open(abs_path, "rb") as f:
            # 有界读取（上限 + 1 字节）：即便文件在 size 检查后被替换/增长
            # （TOCTOU / 符号链接换靶），也不会一次性读入超限内容。
            data = f.read(_MAX_RESOURCE_READ_BYTES + 1)
    except OSError as e:
        return None, entry, f"错误: 读取文件失败: {e}"
    if len(data) > _MAX_RESOURCE_READ_BYTES:
        return None, entry, (
            f"错误: 文件在读取时超过上限（{len(data)} 字节 > "
            f"{_MAX_RESOURCE_READ_BYTES}），拒绝读取。"
        )
    return data, entry, None


def _format_resource_info(entry: dict) -> str:
    """渲染单个资源条目的元信息文本。"""
    return (
        f"文件: {entry['path']}\n"
        f"大小: {_format_size(entry['path'])}\n"
        f"类型: {entry['mime']}\n"
        f"来源: {entry['source']}\n"
        f"资源 URI: {entry['uri']}\n"
        "提示: 用 resources/read 读取该 URI 可取回文件内容；"
        'stata_read_file(action="read") 返回 base64。'
    )


@mcp.resource(
    uri="stata-file:///{path*}",
    title="Stata 导出文件",
    description=(
        "本会话导出/保存的文件。按 'stata-file:///<绝对路径>' 读取二进制内容。"
        "只有本会话导出工具登记过的文件可读（安全边界），未登记报错。"
        "用 stata_list_resources 查看可用文件。"
    ),
    mime_type="application/octet-stream",
)
def _stata_file_resource(path: str) -> bytes:
    """MCP resource 模板处理器：把登记过的文件以二进制返回。

    ``{path*}`` 是通配占位符 —— 路径里的 ``/`` 必须能跨段匹配（普通 ``{path}``
    只匹配单段，POSIX/Windows 绝对路径都会失败）。读取前查注册表，杜绝远程
    客户端借此读取任意磁盘路径。
    """
    data, _, err = _read_registered_file(path)
    if err:
        raise ValueError(err)
    return data


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_read_file(
    filepath: str, action: str = "info", encoding: str = "base64"
) -> str | ToolResult:
    """读取本会话导出/登记的文件（**实际内容**，而不仅是路径）。

    导出工具（stata_graph / stata_export_excel / stata_etable /
    stata_export_delimited / stata_save_dataset / stata_run save_output）成功后
    会自动登记输出文件；远程客户端据此取回图表、Excel、CSV、dta。也支持 MCP
    资源协议：resources/read 按 `stata-file:///<绝对路径>` 读取二进制。

    安全边界：**只读登记过的文件**。未登记会报错并提示登记方式，不会退化成
    任意路径读取原语。

    Args:
        filepath: 文件路径（须是已登记的资源）。
        action: "info"（默认，返回路径/大小/类型/资源 URI，不读内容）或
                "read"（返回 base64 内容）。
        encoding: 内容编码（默认 base64；仅 action="read" 生效）。

    Returns:
        info：元数据文本；read：base64 内容（上限 16MB）。
    """
    if action not in ("info", "read"):
        return _make_error_result(
            f'错误: action 只能是 "info" 或 "read"（收到 {action!r}）'
        )
    if err := _validate_path(filepath):
        return _result_or_error(err)
    entry = _resource_lookup(filepath)
    if entry is None:
        return _make_error_result(
            f"错误: 文件未登记为可读资源: {_normalize_path(filepath)}。\n"
            "  · 导出工具（stata_graph/stata_export_*/stata_etable/"
            "stata_save_dataset）成功后会登记\n"
            "  · 已有文件用 stata_register_file 显式登记"
        )
    if action == "info":
        return _format_resource_info(entry)
    # base64 工具返回没有 _run_stata_command 的 120K 收口：先按载荷上限拦下，
    # 避免一个 16MB 文件编码成约 21MB 的单一工具结果撑爆 MCP 传输。
    if entry["size"] > _MAX_TOOL_READ_BYTES:
        return _make_error_result(
            f"错误: 文件过大（{entry['size']} 字节），无法经 base64 工具返回"
            f"（工具载荷上限约 {_MAX_TOOL_READ_BYTES} 字节）。\n"
            "  · 请用 MCP 资源协议：resources/read 读 "
            f"{entry['uri']}（流式二进制，上限 16MB）\n"
            "  · 或先用 stata_list_resources 确认文件名。"
        )
    data, _, err2 = _read_registered_file(filepath)
    if err2:
        return _make_error_result(err2)
    if encoding == "base64":
        return base64.b64encode(data).decode("ascii")
    return _make_error_result(
        f'错误: encoding 只支持 "base64"（收到 {encoding!r}）'
    )


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def stata_register_file(filepath: str) -> str | ToolResult:
    """把磁盘上已有的文件显式登记为可读资源。

    导出工具会自动登记输出文件；这条工具用于登记由 ``stata_run`` 等自由文本
    命令生成的产物，或此前导出但未登记的文件。登记后才能经资源协议 /
    ``stata_read_file`` 读取 —— 这是资源读取的安全白名单。

    Args:
        filepath: 要登记的文件路径（经路径沙箱校验，须真实存在）。

    Returns:
        登记确认与资源 URI。
    """
    if err := _validate_path(filepath):
        return _result_or_error(err)
    err2 = _register_resource(filepath, "stata_register_file")
    if err2:
        return _make_error_result(err2)
    entry = _resource_lookup(filepath)
    return _format_resource_info(entry)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_list_resources() -> str | ToolResult:
    """列出本会话全部已登记的文件资源。

    导出工具成功后会登记输出文件；此工具让 Agent 知道有哪些产物可经资源协议
    （resources/read）或 ``stata_read_file`` 取回。单条完整信息（含资源 URI）
    用 ``stata_read_file(filepath=..., action="info")`` 查看。

    Returns:
        资源清单文本；无资源时提示。
    """
    with _resource_lock:
        entries = sorted(_resource_registry.values(), key=lambda e: e["ts"])
    if not entries:
        return (
            "(当前没有已登记的文件资源。"
            "导出工具成功后会登记；stata_register_file 可登记已有文件。)"
        )
    lines = [
        "已登记的文件资源（可用 resources/read 或 stata_read_file 读取）:",
        f"{'路径':<52} {'大小':<9} {'来源':<22}",
    ]
    for e in entries:
        lines.append(f"{e['path']:<52} {_format_size(e['path']):<9} {e['source']:<22}")
    lines.append('\n完整 URI 用 stata_read_file(action="info") 查看。')
    return "\n".join(lines)


# =============================================================================
# MCP 工具 — 会话生命周期
# =============================================================================


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def stata_clear(scope: str = "all") -> str | ToolResult:
    """重置 / 清空当前 Stata 会话状态。

    所有调用共享同一个 Stata 会话，长时间工作后可能残留无关数据、估计、图形。
    本工具按 scope 清理，等价于「部分或全部重置会话」：

    ============  ====================================================
    scope          清理内容
    ============  ====================================================
    data          内存数据集（clear all）+ 全部非默认 frame
    estimates     已存储的估计结果（estimates clear）
    graphs        全部图形（graph drop _all）
    panels        面板/时序设定（xtset, clear / tsset, clear）
    all           data + estimates + graphs + panels
    ============  ====================================================

    scope="all" 同时清空输出翻页缓存与本会话的文件资源登记（磁盘文件不删除）。

    Args:
        scope: 清理范围（默认 "all"）。

    Returns:
        清理确认（含各步骤结果）。
    """
    if scope not in ("data", "estimates", "graphs", "panels", "all"):
        return _make_error_result(
            f'错误: scope 只能是 "data"/"estimates"/"graphs"/"panels"/"all"'
            f"（收到 {scope!r}）"
        )
    cmds = []
    if scope in ("data", "all"):
        cmds.append("clear all")
        cmds.append("capture frame drop _all")
    if scope in ("estimates", "all"):
        cmds.append("capture estimates clear")
    if scope in ("graphs", "all"):
        cmds.append("capture graph drop _all")
    if scope in ("panels", "all"):
        cmds.append("capture xtset, clear")
        cmds.append("capture tsset, clear")
    result = _run_stata_command("\n".join(cmds), timeout=120)
    # 清空后 c(N)=0，_describe_empty_result 会误报「请先载入数据」（实战发现：
    # Agent 可能以为 clear 失败而多余地重新载入）。清空是**故意的**，返回确认。
    if isinstance(result, str) and (
        "当前内存中没有数据集" in result or "无文本输出" in result
    ):
        result = f"已清理会话（scope={scope}）：数据集/估计/图形/面板已重置。"
    if scope == "all":
        # 命令执行完（成功或失败）才撤内存侧状态；磁盘文件本身不删除。
        # 必须声明 global：否则赋值会创建同名局部变量，重置形同虚设。
        global _last_output
        with _output_lock:
            _last_output = ""
        _clear_resources()
    return result


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def stata_snapshot(
    action: str = "save", number: int = 0, label: str = ""
) -> str | ToolResult:
    """会话内快照：保存 / 恢复 / 列出 / 删除当前内存数据集。

    包 Stata 原生 ``snapshot`` 命令 —— 同一会话内可在不同数据处理阶段间快速
    回退（例如「清洗后」「缩尾后」各存一个快照，发现异常随时恢复）。这是多会话
    隔离的轻量近似：数据层面的隔离，估计结果/宏等不在快照内。

    Args:
        action: "save"（保存当前数据集为快照，自动编号）/"list"（列出）/
                "restore"（恢复快照 N）/"erase"（删除快照 N）。
        number: 快照编号；restore/erase 必填（正整数）。save/list 忽略。
        label: 保存时的可选标签（如 "清洗后"）；仅 save 生效。

    Returns:
        操作结果（save 后附加快照列表便于确认编号）。
    """
    if action not in ("save", "list", "restore", "erase"):
        return _make_error_result(
            f'错误: action 只能是 "save"/"list"/"restore"/"erase"（收到 {action!r}）'
        )
    if action in ("restore", "erase"):
        if number <= 0:
            return _make_error_result(
                f'错误: action="{action}" 必须提供正整数 number（快照编号）'
            )
        return _run_stata_command(f"snapshot {action} {number}", timeout=120)
    if action == "list":
        return _run_stata_command("snapshot list", timeout=60)
    # save：label 是官方选项（snapshot save, label("...")），不是位置参数 ——
    # 实测位置传参（带不带引号）都报 r(101) varlist not allowed。
    if label and (err := _validate_no_injection(label, "label")):
        return _result_or_error(err)
    if '"' in label or "`" in label:
        return _make_error_result("错误: label 不能包含双引号或反引号")
    cmd = "snapshot save"
    if label.strip():
        cmd += f', label("{label.strip()}")'
    # 不再附加 snapshot list：save 输出本身已含 "snapshot N (label) created at"，
    # 再 list 一遍会把创建记录打印两次（真机实测冗余）。编号用
    # stata_snapshot(action="list") 单独查。
    return _run_stata_command(cmd, timeout=120)


# =============================================================================
# MCP 工具 — 后台任务
# =============================================================================


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def stata_background(command: str, timeout: int = 300) -> str | ToolResult:
    """在后台执行 Stata 命令，立即返回任务号，不阻塞当前请求。

    适合耗时的长任务（大循环、复杂回归、联网下载）：默认 60s 看门狗会把它们
    打断，而 ``stata_background`` 允许单块最长 3600s。任务在后台线程执行，
    Stata DLL 依旧单线程串行 —— 后台任务运行期间，其他工具调用会等待它完成
    （共享同一把 ``_stata_lock``）。

    用 ``stata_task_status`` 查进度、``stata_task_cancel`` 取消、
    ``stata_task_result`` 取结果、``stata_task_list`` 列全部。

    **后台任务运行期间不要向进程 stdout 写内容**：pystata 的 RedirectOutput 是
    进程级替换 sys.stdout，任务执行窗口内主线程的任何 print() 会被捕获进任务
    结果（实测并发主线程 print 出现在 stata_task_result 里）。MCP 生产环境走
    stdio、工具不打印，故无实际影响；但测试/自定义主线程打印需避开该窗口。

    Args:
        command: 要执行的 Stata 命令（可多行，含 { } 复合块）。
        timeout: 单块超时秒数（默认 300，钳制 10–3600）。

    Returns:
        任务号与提交确认。
    """
    safe_timeout = max(_BG_TIMEOUT_MIN, min(timeout, _BG_TIMEOUT_MAX))
    if not command or not command.strip():
        return _make_error_result("错误: command 不能为空")
    if "\x00" in command:
        return _make_error_result("错误: command 包含空字节")
    if len(command) > MAX_COMMAND_LENGTH:
        return _make_error_result(
            f"错误: 命令过长（{len(command)} 字符），上限 {MAX_COMMAND_LENGTH} 字符"
        )
    if reason := _precheck_command(command):
        return _make_error_result(reason)
    try:
        task_id = _submit_bg_task(command, safe_timeout)
    except ValueError as e:
        return _make_error_result(str(e))
    snippet = command[:120] + ("..." if len(command) > 120 else "")
    return (
        f"后台任务已提交: {task_id}\n"
        f"命令: {snippet}\n"
        "用 stata_task_status / stata_task_result 查看进度与结果。"
    )


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_task_status(task_id: str) -> str | ToolResult:
    """查询后台任务的状态与进度。

    Args:
        task_id: stata_background 返回的任务号。

    Returns:
        状态、进度（当前块/总块数）、耗时等文本。
    """
    task = _bg_task(task_id)
    if task is None:
        return _make_error_result(f"错误: 未找到任务 {task_id}")
    return _bg_status_text(task)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def stata_task_cancel(task_id: str) -> str | ToolResult:
    """请求取消一个运行中的后台任务。

    取消请求置位后：任务正卡在单条命令里时立即 SetBreak 打断；处于块与块之间
    时在执行下一条命令前终止。已结束的任务取消是空操作。

    Args:
        task_id: 要取消的任务号。

    Returns:
        取消请求确认。
    """
    found, msg = _bg_cancel(task_id)
    if not found:
        return _make_error_result(msg)
    return msg


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_task_result(task_id: str) -> str | ToolResult:
    """获取后台任务的完整输出结果。

    Args:
        task_id: 要取结果的任务号。

    Returns:
        任务完整输出（成功时）；失败时返回 isError 结果；任务未完成时返回
        当前进度并提示稍后重试。
    """
    task = _bg_task(task_id)
    if task is None:
        return _make_error_result(f"错误: 未找到任务 {task_id}")
    if task.status in ("queued", "running"):
        return _bg_status_text(task) + "\n(任务仍在运行，请稍后重试 stata_task_result)"
    if task.status == "cancelled":
        return _make_error_result(task.result)
    if task.is_error:
        return _make_error_result(task.result)
    return task.result or "(任务完成，无文本输出)"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_task_list() -> str | ToolResult:
    """列出全部（含已结束的）后台任务。

    Returns:
        任务清单：任务号、状态、进度、提交时间。
    """
    with _bg_lock:
        tasks = sorted(_bg_tasks.values(), key=lambda t: t.created_at)
    if not tasks:
        return "(当前没有后台任务。用 stata_background 提交长任务。)"
    lines = [f"{'任务号':<14} {'状态':<10} {'进度':<18} 提交时间"]
    for t in tasks:
        if t.status == "queued" or not t.blocks:
            prog = "待开始"
        else:
            prog = f"{t.block_index + 1}/{len(t.blocks)} 块"
        lines.append(
            f"{t.task_id:<14} {t.status:<10} {prog:<18} "
            f"{time.strftime('%H:%M:%S', time.localtime(t.created_at))}"
        )
    return "\n".join(lines)


# =============================================================================
# MCP 工具 — 服务器日志
# =============================================================================


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_read_log(action: str = "tail", lines: int = 200) -> str | ToolResult:
    """读取本 MCP Server 自身的运行日志（stata-mcp.log）。

    用于排查远程客户端无法直接观察的服务器侧问题（DLL 崩溃、看门狗中断、
    初始化失败）。日志同时写 stderr 与文件，MCP 传输中断后仍可追溯。

    Args:
        action: "tail"（默认，返回日志末尾 lines 行）/"path"（返回日志路径）。
        lines: tail 返回的行数（默认 200，上限 2000）。

    Returns:
        日志末尾文本或日志路径。
    """
    if action not in ("tail", "path"):
        return _make_error_result(
            f'错误: action 只能是 "tail" 或 "path"（收到 {action!r}）'
        )
    if action == "path":
        return f"日志文件: {_LOG_FILE}"
    if not os.path.isfile(_LOG_FILE):
        return _make_error_result(f"错误: 日志文件不存在（{_LOG_FILE}）")
    n = max(1, min(lines, 2000))
    try:
        with open(_LOG_FILE, encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-n:]
    except OSError as e:
        return _make_error_result(f"错误: 读取日志失败: {e}")
    return "".join(tail)


# =============================================================================
# 结构化便利工具装配（tool_modules/）
# =============================================================================
# 便利工具（数据重构 / 扩展估计 / 后估计）由独立模块提供，经 register() 装配。
# 模块不 import server，只依赖注入的 _API_DEPS 中的既有助手 —— 保持 server.py
# 单文件执行核心不变，也让各模块可独立测试。

from types import SimpleNamespace  # noqa: E402

from tool_modules.data_explore import register as _register_data_explore  # noqa: E402
from tool_modules.data_gen import register as _register_data_gen  # noqa: E402
from tool_modules.data_io import register as _register_data_io  # noqa: E402
from tool_modules.data_restructure import register as _register_data_restructure  # noqa: E402
from tool_modules.data_restructure_core import (  # noqa: E402
    register as _register_data_restructure_core,
)
from tool_modules.estimation import register as _register_estimation  # noqa: E402
from tool_modules.estimation_core import register as _register_estimation_core  # noqa: E402
from tool_modules.export import register as _register_export  # noqa: E402
from tool_modules.graph import register as _register_graph  # noqa: E402

# 帮助主题白名单已随 stata_help 迁入 tool_modules/package.py；以同名属性重导出，
# 保持 from server import _HELP_TOPIC_RE 的测试面不变。
from tool_modules.package import _HELP_TOPIC_RE  # noqa: E402, F401
from tool_modules.package import register as _register_package  # noqa: E402
from tool_modules.postestimation import register as _register_postestimation  # noqa: E402
from tool_modules.postestimation_core import (  # noqa: E402
    register as _register_postestimation_core,
)

# 晚绑定：deps 里的助手在**调用时**经 server 模块全局解析。这样把核心工具搬进
# tool_modules 后，测试的 `patch("server._run_stata_command")` 依然能截获 ——
# 工具经 deps.run_stata_command(...) 调用时，lambda 在调用点读取 server 当前的
# `_run_stata_command`（可能已被 patch 替换）。
_API_DEPS = SimpleNamespace(
    ToolAnnotations=ToolAnnotations,
    ToolResult=ToolResult,
    run_stata_command=lambda *a, **k: _run_stata_command(*a, **k),
    make_error=lambda msg: _make_error_result(msg),
    result_or_error=lambda v: _result_or_error(v),
    validate_identifier=lambda v, label="变量名", required=False: _validate_identifier(v, label, required),
    validate_varlist=lambda v, label="varlist": _validate_varlist(v, label),
    validate_filter_expr=lambda v, label: _validate_filter_expr(v, label),
    validate_no_injection=lambda v, label="参数": _validate_no_injection(v, label),
    validate_storage_type=lambda v: _validate_storage_type(v),
    filter_clause=lambda c, r: _filter_clause(c, r),
    # merge/append 的 using 路径解析（server.py 内既有函数，晚绑定注入）
    split_using_paths=lambda *a, **k: _split_using_paths(*a, **k),
    # 数据 I/O 工具族（data_io.py）—— 全部晚绑定，patch("server.<name>") 仍截获
    validate_path=lambda v: _validate_path(v),
    normalize_path=lambda v: _normalize_path(v),
    path_has_extension=lambda p: _path_has_extension(p),
    append_default_extension=lambda p, e: _append_default_extension(p, e),
    validate_sheet_name=lambda v: _validate_sheet_name(v),
    validate_delimiter=lambda v, label="delimiter": _validate_delimiter(v, label),
    validate_cell_reference=lambda v: _validate_cell_reference(v),
    register_resource=lambda p, s: _register_resource(p, s),
    resource_uri=lambda p: _resource_uri(p),
    append_text=lambda r, e: _append_text(r, e),
    # 导出工具族（export.py）—— 全部晚绑定，patch("server.<name>") 仍截获
    stata_lock=lambda: _stata_lock,
    execute_safe=lambda *a, **k: _execute_safe(*a, **k),
    mtime_ns=lambda p: _mtime_ns(p),
    file_written_since=lambda p, b: _file_written_since(p, b),
    format_size=lambda p: _format_size(p),
    empty_selection_hint=lambda t, c, r: _empty_selection_hint(t, c, r),
    ESTOUT_PROBE_CMD=_ESTOUT_PROBE_CMD,
    RC_RECOVERED=STATA_RC_RECOVERED,
    RC_NO_OUTPUT=STATA_RC_NO_OUTPUT,
    # 图形工具族（graph.py）—— 全部晚绑定，patch("server.<name>") 仍截获
    precheck_command=lambda v: _precheck_command(v),
    has_unsafe_brace=lambda v: _has_unsafe_brace(v),
    graph_size_options=lambda p, w, h: _graph_size_options(p, w, h),
    graph_format_options=lambda p, q, m, f: _graph_format_options(p, q, m, f),
    validate_scheme_name=lambda v: _validate_scheme_name(v),
    validate_fontface=lambda v: _validate_fontface(v),
    file_is_nonempty=lambda p: _file_is_nonempty(p),
    # 包管理工具族（package.py）—— 全部晚绑定，patch("server.<name>") 仍截获
    validate_install_source=lambda v: _validate_install_source(v),
)

# register() 返回 {工具名: 函数}；update 到模块全局，让 `from server import
# stata_replace` 与 E2E 的 `stata.stata_replace(...)` 与既有 50 工具行为一致。
for _module in (
    _register_data_explore,
    _register_data_gen,
    _register_data_io,
    _register_data_restructure,
    _register_data_restructure_core,
    _register_estimation,
    _register_estimation_core,
    _register_export,
    _register_graph,
    _register_package,
    _register_postestimation,
    _register_postestimation_core,
):
    globals().update(_module(mcp, _API_DEPS))


# =============================================================================
# 入口
# =============================================================================


def main() -> None:
    """console script 入口（pip 安装后 `stata-mcp-server` 命令 / uvx 运行）。

    与 `python server.py` 等价：启动 stdio 传输的 MCP 服务器。
    """
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
