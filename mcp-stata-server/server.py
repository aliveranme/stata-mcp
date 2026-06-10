#!/usr/bin/env python3
"""
Stata MCP Server — 通过 pystata 执行 Stata 命令。

使用 Stata 内置的 Python 集成 (pystata) 直接调用 Stata DLL，
支持执行 do 文件、交互式命令、包管理和数据处理。

兼容 StataNow 19 / Stata 18（MP / SE / BE 版本）。

环境变量:
    STATA_HOME: Stata 安装目录（默认 D:\\StataNow19）
    STATA_EDITION: Stata 版本 mp|se|be（默认 mp）
"""

import sys
import os
import time
import threading
import atexit
import logging
import tempfile

# =============================================================================
# 配置
# =============================================================================

STATA_HOME = os.environ.get("STATA_HOME", r"C:\Program Files\StataNow\StataNow19")
STATA_EDITION = os.environ.get("STATA_EDITION", "mp")

# 日志（写入 stderr 以免干扰 stdio MCP 通信）
logging.basicConfig(
    level=logging.WARNING,
    format="[stata-mcp] %(levelname)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("stata-mcp")

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


# =============================================================================
# MCP Server
# =============================================================================

from fastmcp import FastMCP
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


def _parse_command_blocks(cmd: str) -> list:
    """将多行 Stata 输入解析为可执行块。

    修复以下 MCP Server 核心问题：
    - `///` 续行符被误判为 `//` 注释过滤 → 保持原样
    - `{ }` 复合块（compound braces）跨多行被拆散 → 合并为单次执行
    - `*` 和 `//` 注释行正常跳过

    Args:
        cmd: 原始多行命令字符串。

    Returns:
        可执行命令块列表，每块作为单一 StataSO_Execute 调用。
    """
    raw_lines = cmd.strip().split("\n")
    blocks = []
    i = 0

    while i < len(raw_lines):
        line = raw_lines[i]
        stripped = line.strip()

        # 跳过空行
        if not stripped:
            i += 1
            continue

        # 跳过注释行：* 行首 和 // 行（但不包括 /// 续行符）
        if stripped.startswith("*"):
            i += 1
            continue
        if stripped.startswith("//") and not stripped.startswith("///"):
            i += 1
            continue

        # 处理 /// 续行符：合并到下一行
        while stripped.endswith("///"):
            i += 1
            if i < len(raw_lines):
                # 去掉 ///，合并续行内容
                stripped = stripped.rstrip("/") + " " + raw_lines[i].strip()
            else:
                break

        # 处理 { } 复合块：跨多行合并为单次执行
        if "{" in stripped and "}" not in stripped:
            block_lines = [stripped]
            brace_depth = stripped.count("{") - stripped.count("}")
            i += 1
            while i < len(raw_lines) and brace_depth > 0:
                next_line = raw_lines[i]
                block_lines.append(next_line)
                brace_depth += next_line.count("{") - next_line.count("}")
                i += 1
            blocks.append("\n".join(block_lines))
        else:
            blocks.append(stripped)
            i += 1

    return blocks


def _drain_output() -> str:
    """排空输出缓冲，返回残留内容。

    等待至少 200ms 确保 Stata 完成所有输出生产。
    用于执行前清理和 SetBreak 后的错误恢复。
    """
    parts = []
    t_start = time.time()
    last_nonempty = time.time()

    while time.time() - t_start < 0.2:  # 最少等待 200ms
        out = config.get_output()
        if out:
            parts.append(out)
            last_nonempty = time.time()
        # 最近一次输出后至少 30ms 无新输出才退出
        if time.time() - last_nonempty > 0.03:
            break
        time.sleep(0.005)

    return "".join(parts)


def _set_break():
    """安全调用 Stata 中断，用于超时恢复。"""
    try:
        sb = config.stlib.StataSO_SetBreak
        if sb:
            sb()
    except Exception as e:
        logger.warning("StataSO_SetBreak failed: %s", e)


def _ping_stata() -> bool:
    """快速心跳：检测 Stata DLL 是否存活。

    执行一个无害命令(display 42)，预期返回码 0。
    若失败则尝试排空缓冲 + SetBreak 恢复一次。
    Returns:
        True = 存活, False = 无响应。
    """
    for attempt in range(2):  # 首次 + 一次恢复重试
        try:
            with stout.RedirectOutput(stout.StataDisplay(), stout.StataError(), stecho=False):
                encoded = config.get_encode_str("display 42")
                rc = config.stlib.StataSO_Execute(encoded, False)
            # 收集并丢弃输出
            for _ in range(30):
                config.get_output()
                time.sleep(0.001)
            if rc == 0 or rc == 3000:
                return True
        except Exception:
            pass

        if attempt == 0:
            logger.warning("Stata ping failed, attempting recovery...")
            _drain_output()
            _set_break()
            time.sleep(0.1)

    return False


def _execute_safe(cmd: str, timeout: int = 60) -> tuple:
    """安全执行 Stata 命令，包含预检 + 崩溃恢复。

    流程：
      1. ping 预检（Stata DLL 是否存活）
      2. 若存活 → _execute_single() 正常执行
      3. 若 _execute_single 返回崩溃码(999) → 尝试恢复
      4. 若 ping 失败 → 直接返回错误信息

    Args:
        cmd: Stata 命令字符串。
        timeout: 超时秒数（默认 60）。

    Returns:
        (return_code, output_text)
    """
    # --- 预检 ---
    if not _ping_stata():
        logger.error("Stata 无响应，无法执行命令: %s", cmd[:80])
        return 998, (
            "[错误] Stata DLL 无响应。这可能由以下原因导致：\n"
            "  1. 上一个命令导致 Stata DLL 崩溃\n"
            "  2. headless 环境中图形操作过载\n"
            "  3. Stata 会话已损坏\n\n"
            "建议: 重启 MCP Server（退出并重新打开 Claude Code）\n"
        )

    # --- 执行 ---
    rc, out = _execute_single(cmd, timeout)

    # --- 崩溃检测与恢复 ---
    if rc == 999:
        logger.warning("StataSO_Execute 崩溃, 尝试恢复会话...")
        _drain_output()
        _set_break()
        time.sleep(0.2)

        # 再次 ping 确认恢复
        if _ping_stata():
            out += "\n(Stata 已自动恢复，请重试命令)"
        else:
            out += (
                "\n(Stata 崩溃且无法自动恢复，需要重启 MCP Server)"
            )

    return rc, out


def _execute_single(cmd: str, timeout: int = 60) -> tuple:
    """执行单条 Stata 命令，返回 (return_code, output_text)。

    使用 RedirectOutput 防止 Stata 输出泄漏到 MCP stdio 通道。
    内置超时保护：命令执行超过 timeout 秒时调用 StataSO_SetBreak 中断。

    支持两阶段输出收集：1ms 快轮询 + 5ms 慢轮询清尾。

    Args:
        cmd: Stata 命令字符串。
        timeout: 超时秒数（默认 60）。

    Returns:
        (return_code, output_text)
    """
    # 执行前排空残留缓冲
    _drain_output()

    # 超时看门狗（防止 StataSO_Execute 挂起导致 MCP 通信阻塞）
    # 使用 threading.Event 避免竞态：SetBreak 仅在命令实际还在运行时触发
    exec_done = threading.Event()
    did_break = False

    def _timeout_watchdog():
        nonlocal did_break
        if not exec_done.wait(timeout=timeout):
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
        _drain_output()

    # 收集输出
    output_parts = []
    total_len = 0
    empty_count = 0

    for _ in range(300):
        out = config.get_output()
        if out:
            output_parts.append(out)
            total_len += len(out)
            empty_count = 0
            if total_len >= MAX_OUTPUT_CHARS:
                output_parts.append("\n(输出已截断)")
                break
        else:
            empty_count += 1
            if empty_count >= 3:
                break
        time.sleep(0.001)

    if total_len < MAX_OUTPUT_CHARS:
        tail = _drain_output()
        if tail:
            output_parts.append(tail)
            total_len += len(tail)

    return rc, "".join(output_parts)


def _run_stata_command(cmd: str, page: int = 1, timeout: int = 60) -> str:
    """执行 Stata 命令，支持分页浏览。

    多行命令按 \\n 拆分后逐条执行。
    支援 `///` 续行符和 `{ }` 复合块（自动合并为单次 StataSO_Execute 调用）。
    当输出超过 PAGE_SIZE 时自动缓存完整输出并返回首页。
    所有执行经过 _execute_safe（预检 + 超时 + 崩溃恢复）。

    Args:
        cmd: Stata 命令字符串（多命令用 \\n 分隔）。
        page: 页码（1-based），0 = 全部，仅对单命令有效。
        timeout: 每条命令的超时秒数（默认 60）。

    Returns:
        Stata 输出文本（可能包含分页导航）。
    """
    global _last_output

    # 输入验证
    if not cmd or not cmd.strip():
        return "(无有效命令)"
    if len(cmd) > MAX_COMMAND_LENGTH:
        return f"错误: 命令过长（{len(cmd)} 字符），上限 {MAX_COMMAND_LENGTH} 字符"

    with _stata_lock:
        # 使用新的解析器：正确处理 /// 续行和 { } 复合块
        blocks = _parse_command_blocks(cmd)

        if not blocks:
            return "(无有效命令)"

        all_output = []
        for block in blocks:
            try:
                rc, out = _execute_safe(block, timeout)

                # RC=998: Stata DLL dead, abort chain
                if rc == 998:
                    all_output.append(out)
                    break

                # STATA_RC_NO_OUTPUT (3000) = 无错误但无实质输出
                if rc != 0 and rc != STATA_RC_NO_OUTPUT:
                    prefix = f"[返回码: {rc}] {block[:60]}"
                    all_output.append(f"{prefix}\n{out.strip()}" if out.strip() else prefix)
                elif out.strip():
                    all_output.append(out.strip())

            except SystemError as e:
                all_output.append(f"Stata 系统错误 ({block[:40]}): {e}")
            except Exception:
                logger.exception("Error executing: %s", block[:200])
                all_output.append(f"执行错误 ({block[:40]}): {sys.exc_info()[1]}")

        full = "\n".join(all_output) if all_output else "(命令执行成功，无文本输出)"
        _last_output = full

        # 自动分页：仅当是单条命令且输出超过阈值
        if len(blocks) == 1 and len(full) > PAGE_SIZE:
            return _paginate(full, page)
        elif len(blocks) > 1 and sum(len(o) for o in all_output) > PAGE_SIZE * 3:
            # 多命令输出也分页
            return _paginate(full, page)

        return full


def _normalize_path(path: str) -> str:
    """将路径转换为 Stata 可接受的格式（正斜杠）。"""
    return os.path.normpath(os.path.abspath(path)).replace("\\", "/")


def _check_file_exists(filepath: str) -> str | None:
    """检查文件是否存在，返回错误消息或 None。"""
    if not os.path.isfile(filepath):
        return f"错误: 文件不存在 — {filepath}"
    return None


# =============================================================================
# 生命周期
# =============================================================================


@atexit.register
def _shutdown_stata():
    """优雅关闭 Stata 会话。"""
    try:
        if config.is_stata_initialized():
            config.shutdown()
            logger.info("Stata shut down cleanly")
    except Exception:
        logger.exception("Error during Stata shutdown")


# =============================================================================
# MCP 工具 — 核心执行
# =============================================================================


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def stata_run(command: str, page: int = 1, timeout: int = 60) -> str:
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
        timeout: 命令超时秒数（默认 60，最长 300）。

    Returns:
        Stata 输出文本（可能包含分页导航）。
    """
    # 限定时长范围
    safe_timeout = max(10, min(timeout, 300))
    return _run_stata_command(command, page, timeout=safe_timeout)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def stata_run_do_file(filepath: str) -> str:
    """执行一个 Stata .do 文件并返回全部输出。

    .do 文件是 Stata 的批处理脚本。此工具会执行指定路径的 .do 文件。

    Args:
        filepath: .do 文件的绝对路径。

    Returns:
        do 文件执行过程中的全部 Stata 输出。
    """
    if err := _check_file_exists(filepath):
        return err
    return _run_stata_command(f'do "{_normalize_path(filepath)}"')


# =============================================================================
# MCP 工具 — 数据管理 (destructiveHint=True)
# =============================================================================


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def stata_use_dataset(filepath: str, clear: bool = True) -> str:
    """加载 Stata 数据集 (.dta 文件) 到内存中。

    加载后可使用 stata_describe、stata_summarize 等工具查看数据。

    Args:
        filepath: .dta 文件的绝对路径。
        clear: 是否先清除内存中的已有数据（默认 True）。

    Returns:
        数据集加载确认信息及变量列表。
    """
    if err := _check_file_exists(filepath):
        return err
    normalized = _normalize_path(filepath)
    suffix = ", clear" if clear else ""
    return _run_stata_command(f'use "{normalized}"{suffix}')


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def stata_save_dataset(filepath: str, replace: bool = False) -> str:
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
def stata_set_cwd(path: str) -> str:
    """更改 Stata 的工作目录。

    Args:
        path: 新的工作目录路径。

    Returns:
        当前工作目录确认信息。
    """
    return _run_stata_command(f'cd "{_normalize_path(path)}"')


# =============================================================================
# MCP 工具 — 数据探索 (readOnlyHint=True)
# =============================================================================


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_describe(varlist: str = "", simple: bool = False) -> str:
    """描述当前数据集的变量信息。

    显示变量名、存储类型、显示格式、变量标签和值标签。
    使用 simple=True 可获得更精简的输出。

    Args:
        varlist: 要描述的变量（空格分隔），留空 = 全部变量。
        simple: 是否使用精简模式（默认 False）。

    Returns:
        变量描述信息表。
    """
    if simple:
        cmd = "describe, simple"
    elif varlist.strip():
        cmd = f"describe {varlist}"
    else:
        cmd = "describe"
    return _run_stata_command(cmd)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_summarize(varlist: str = "", detail: bool = False) -> str:
    """计算变量的摘要统计量。

    包括观测数、均值、标准差、最小值、最大值。
    使用 detail=True 可获得百分位数、偏度、峰度等。

    Args:
        varlist: 变量列表（空格分隔），留空 = 全部变量。
        detail: 是否显示详细统计量（默认 False）。

    Returns:
        摘要统计量表格。
    """
    cmd = f"summarize {varlist}".strip()
    if detail:
        cmd += ", detail"
    return _run_stata_command(cmd)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_list(varlist: str = "", n: int = 10, in_range: str = "") -> str:
    """列出当前数据集中的数据值。

    以表格形式展示观测数据。默认显示前 10 条。

    Args:
        varlist: 要列出的变量（空格分隔），留空 = 全部。
        n: 显示前 n 条观测（默认 10，设为 0 显示全部，慎用）。
        in_range: 观测范围如 "1/20" 或 "1/l"。

    Returns:
        数据表格。
    """
    cmd = "list"
    if varlist.strip():
        cmd += f" {varlist}"
    if in_range.strip():
        cmd += f" in {in_range}"
    elif n > 0:
        cmd += f" in 1/{n}"
    return _run_stata_command(cmd)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_codebook(varlist: str = "", compact: bool = False) -> str:
    """生成数据集的 Codebook（变量字典）。

    显示变量标签、值标签、缺失值、分布信息等。
    比 describe 更详细。

    Args:
        varlist: 变量列表（空格分隔），留空 = 全部变量。
        compact: 是否使用紧凑模式（默认 False）。

    Returns:
        Codebook 报告。
    """
    cmd = f"codebook {varlist}".strip()
    if compact:
        cmd += ", compact"
    return _run_stata_command(cmd)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_tabulate(varname: str, byvar: str = "", chi2: bool = False) -> str:
    """创建频数分布表或交叉表。

    单变量：频数分布表。双变量：二维交叉表，可选卡方检验。

    Args:
        varname: 主变量名。
        byvar: 可选的第二个变量，用于交叉表。
        chi2: 是否显示卡方检验结果（默认 False）。

    Returns:
        频数/交叉表。
    """
    if not varname.strip():
        return "错误：请提供至少一个变量名。"
    cmd = f"tabulate {varname}"
    if byvar.strip():
        cmd += f" {byvar}"
        if chi2:
            cmd += ", chi2"
    return _run_stata_command(cmd)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_display(expression: str) -> str:
    """计算并显示 Stata 表达式的结果。

    可用于简单计算、宏展开、返回值查看。
    适合查看 r(mean)、e(N)、e(r2) 等存储结果。

    Args:
        expression: Stata 表达式，如 "2+2"、"r(mean)"、"e(r2)"。

    Returns:
        表达式计算结果。
    """
    return _run_stata_command(f"display {expression}")


# =============================================================================
# MCP 工具 — 统计分析 (readOnlyHint=True)
# =============================================================================


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_regress(depvar: str, indepvars: str, options: str = "") -> str:
    """运行线性回归分析 (OLS)。

    返回系数表、标准误、t 值、p 值和模型诊断统计量。

    Args:
        depvar: 因变量名。
        indepvars: 自变量列表（空格分隔）。
        options: 额外选项，如 "robust"（稳健标准误）、"noconstant"。

    Returns:
        回归分析结果表。
    """
    cmd = f"regress {depvar} {indepvars}"
    if options.strip():
        cmd += f", {options}"
    return _run_stata_command(cmd)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_logistic(depvar: str, indepvars: str, options: str = "") -> str:
    """运行 Logistic 回归分析。

    执行 Logit 模型估计，返回系数、标准误和模型拟合统计量。

    Args:
        depvar: 二元因变量名（取值 0/1）。
        indepvars: 自变量列表（空格分隔）。
        options: 额外选项，如 "or"（优势比）、"robust"。

    Returns:
        Logistic 回归结果表。
    """
    cmd = f"logit {depvar} {indepvars}"
    if options.strip():
        cmd += f", {options}"
    return _run_stata_command(cmd)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_ttest(varname: str, byvar: str = "", options: str = "") -> str:
    """运行 t 检验。

    支持单样本 t 检验、独立样本 t 检验（按分组变量）、配对 t 检验。

    Args:
        varname: 要检验的变量名。
        byvar: 分组变量（可选，用于独立样本 t 检验）。
        options: 额外选项，如 "unequal"。

    Returns:
        t 检验结果表。
    """
    if byvar.strip():
        cmd = f"ttest {varname}, by({byvar})"
        if options.strip():
            cmd += f" {options}"
    else:
        cmd = f"ttest {varname}"
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


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_graph(
    command: str,
    scheme: str = "s2color",
    export: str = "",
    width: int = 800,
    replace: bool = True,
) -> str:
    """生成 Stata 图形并可选导出为文件。

    自动在命令前设置图形方案（scheme）。
    当指定 export 时,使用临时 .do 文件将 graph + export 包装在单次
    StataSO_Execute 调用中,避免图形窗口在 headless 环境中丢失。

    Args:
        command: 图形命令(scatter mpg weight, histogram price 等)。
        scheme: 图形方案(默认 's2color')。常用方案:
                s2color(默认彩色), s2mono(灰度), s2manual(手册风格)。
        export: 导出 PNG 文件路径(留空不导出)。例:"C:/output/scatter.png"。
        width: 导出图片宽度(像素,默认 800)。
        replace: 是否覆盖已有文件(默认 True)。

    Returns:
        图形生成确认信息。
    """
    try:
        if not export:
            return _run_stata_command(f"set scheme {scheme}\n{command}")

        # 导出模式：通过 .do 文件确保 graph + export 在同一次调用中
        export_path = _normalize_path(export)
        replace_opt = "replace" if replace else ""

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".do", delete=False, encoding="utf-8"
        ) as f:
            do_path = f.name
            f.write(f"set scheme {scheme}\n")
            f.write(f"{command}\n")
            f.write(
                f'graph export "{export_path}", {replace_opt} width({width})\n'
            )
            f.write("graph drop _all\n")

        do_path_normalized = _normalize_path(do_path)
        result = _run_stata_command(f'do "{do_path_normalized}"')
        return result

    except Exception as e:
        return f"图形生成失败: {type(e).__name__}: {e}"
    finally:
        # 确保 .do 文件被清理
        try:
            if "do_path" in dir() and os.path.isfile(do_path):
                os.unlink(do_path)
        except Exception:
            pass


# =============================================================================
# MCP 工具 — Excel 导出 (destructiveHint=True)
# =============================================================================


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def stata_export_excel(
    filepath: str,
    varlist: str = "",
    sheet: str = "Sheet1",
    replace: bool = True,
    results: bool = False,
) -> str:
    """将当前数据集导出为 Excel (.xlsx) 文件。

    使用 Stata 的 export excel 命令导出数据。
    也支持使用 esttab 或 outreg2 导出回归结果表到 Excel。

    Args:
        filepath: 导出路径（建议 .xlsx 扩展名）。
        varlist: 要导出的变量列表（空格分隔），留空 = 全部变量。
        sheet: Excel 工作表名（默认 "Sheet1"）。
        replace: 是否覆盖已有文件（默认 True）。
        results: 若为 True，将当前存储的回归结果导出为表格而非原始数据。

    Returns:
        导出确认信息。
    """
    export_path = _normalize_path(filepath)
    replace_opt = "replace" if replace else ""
    firstrow_opt = "firstrow(variables)"

    if results:
        # 导出回归结果为 Excel 表
        cmd = (
            f"esttab using \"{export_path}\", {replace_opt} "
            f"sheet({sheet}) plain nogaps nomtitle nonumber"
        )
    else:
        # 导出数据集
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

    result = _run_stata_command(cmd)

    # 验证文件已生成
    if os.path.isfile(export_path):
        size_kb = os.path.getsize(export_path) // 1024
        return f"✓ 已导出 {size_kb} KB → {export_path}\n{result}"
    return result


# =============================================================================
# MCP 工具 — 包管理 (destructiveHint=True)
# =============================================================================


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def stata_install_package(package: str, source: str = "ssc", replace: bool = False) -> str:
    """安装 Stata 扩展包。

    从 ssc、net 地址或 GitHub 安装 Stata 包。
    支持 force/replace 选项来解决版本冲突。

    Args:
        package: 包名称（如 "outreg2"、"estout"、"ivreg2"）。
        source: 安装源 — "ssc"（默认）、"net"、或完整 URL。
        replace: 是否强制替换已有文件（解决版本冲突，默认 False）。

    Returns:
        安装过程输出。
    """
    replace_opt = ", replace" if replace else ""
    if source.lower() == "ssc":
        cmd = f"ssc install {package}{replace_opt}"
    elif source.lower() == "net":
        cmd = (
            f"net install {package}{replace_opt},"
            f" from(https://fmwww.bc.edu/RePEc/bocode/{package[0]})"
        )
    else:
        cmd = f"net install {package}{replace_opt}, from({source})"
    return _run_stata_command(cmd)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_find_package(keyword: str) -> str:
    """搜索 Stata 扩展包。

    在 ssc 存档中搜索与关键词匹配的 Stata 包。

    Args:
        keyword: 搜索关键词（如 "panel"、"graph"、"iv"）。

    Returns:
        匹配的包列表及简要描述。
    """
    return _run_stata_command(f"ssc search {keyword}")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_list_packages() -> str:
    """列出当前已安装的所有 Stata 扩展包。

    Returns:
        已安装包列表。
    """
    return _run_stata_command("ado describe")


# =============================================================================
# MCP 工具 — 会话 (readOnlyHint=True)
# =============================================================================


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_more(page: int = 0, page_size: int = 0) -> str:
    """翻页浏览上一条 Stata 命令的完整输出。

    当 stata_run 等工具返回的输出过长时，完整内容被缓存，
    可使用此工具按页浏览。

    Args:
        page: 页码（1-based），0 = 显示全部。默认 0。
        page_size: 每页字符数，0 = 使用默认值 (4000)。默认 0。

    Returns:
        指定页的输出内容及导航信息。
    """
    global _last_output
    if not _last_output:
        return "(没有缓存的输出，请先执行 Stata 命令)"
    ps = page_size if page_size > 0 else PAGE_SIZE
    return _paginate(_last_output, page, ps)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_status() -> str:
    """获取当前 Stata 会话状态。

    显示当前加载的数据集、变量数量、观测数量、工作目录和内存使用情况。

    Returns:
        会话状态摘要。
    """
    return _run_stata_command("describe\ncd\nmemory")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_ping() -> str:
    """心跳检测 — 快速测试 Stata MCP Server 是否存活。

    执行一个极简的 Stata 命令(display 42)并返回。
    如果 Stata DLL 已崩溃或 MCP 连接已断开，此工具会报错。

    Returns:
        "pong" + 当前 Stata 版本信息。
    """
    try:
        result = _run_stata_command("display 42")
        version = getattr(config, "stversion", "?")
        edition = getattr(config, "stedition", "?")
        status = "alive" if "42" in result else "degraded"
        return f"pong | Stata {version} {edition} | {status}"
    except Exception as e:
        return f"Stata 心跳失败: {type(e).__name__}: {e}"


# =============================================================================
# 入口
# =============================================================================

if __name__ == "__main__":
    mcp.run(transport="stdio")
