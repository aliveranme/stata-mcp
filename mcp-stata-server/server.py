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

# =============================================================================
# 配置
# =============================================================================

STATA_HOME = os.environ.get("STATA_HOME", r"D:\StataNow19")
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
    logger.info(
        "Stata %s %s initialized at %s",
        config.stversion,
        config.stedition,
        config.sthome,
    )
except SystemError as e:
    logger.error("Stata initialization failed: %s", e)
    print(f"FATAL: Failed to initialize Stata: {e}", file=sys.stderr)
    sys.exit(1)


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


def _execute_single(cmd: str) -> tuple:
    """执行单条 Stata 命令，返回 (return_code, output_text)。"""
    encoded = config.get_encode_str(cmd)
    rc = config.stlib.StataSO_Execute(encoded, False)

    # 阶段 1：快速轮询
    output_parts = []
    empty_count = 0
    for _ in range(200):
        out = config.get_output()
        if out:
            output_parts.append(out)
            empty_count = 0
        else:
            empty_count += 1
            if empty_count >= 5:
                break
        time.sleep(0.005)

    # 阶段 2：延迟等待
    time.sleep(0.08)
    for _ in range(10):
        out = config.get_output()
        if out:
            output_parts.append(out)
            time.sleep(0.01)
        else:
            break

    return rc, "".join(output_parts)


def _run_stata_command(cmd: str) -> str:
    """执行 Stata 命令并返回输出文本。

    支持多行命令——按 \\n 拆分后逐条执行，保持会话状态。

    Args:
        cmd: Stata 命令字符串（可包含多行命令，\\n 分隔）。

    Returns:
        Stata 输出文本。
    """
    with _stata_lock:
        # 分割命令，过滤空行和纯注释行
        lines = []
        for line in cmd.strip().split("\n"):
            stripped = line.strip()
            if stripped and not stripped.startswith("*"):
                lines.append(stripped)

        if not lines:
            return "(无有效命令)"

        all_output = []
        for line in lines:
            try:
                rc, out = _execute_single(line)

                # 返回码处理
                if rc != 0 and rc != 3000:
                    prefix = f"[返回码: {rc}] {line[:60]}"
                    all_output.append(f"{prefix}\n{out.strip()}" if out.strip() else prefix)
                elif out.strip():
                    all_output.append(out.strip())

            except SystemError as e:
                all_output.append(f"Stata 系统错误 ({line[:40]}): {e}")
            except Exception:
                logger.exception("Error executing: %s", line[:200])
                all_output.append(f"执行错误 ({line[:40]}): {sys.exc_info()[1]}")

        return "\n".join(all_output) if all_output else "(命令执行成功，无文本输出)"


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
def stata_run(command: str) -> str:
    """执行一条或多条 Stata 命令并返回输出。

    这是最核心的工具，可以执行任意 Stata 命令。
    支持多行命令，每行一条命令。支持数据加载、统计分析、
    回归、图形生成、数据管理等各种 Stata 操作。

    使用示例：
    - 单条命令: "summarize mpg"
    - 多条命令: "sysuse auto, clear\\nsummarize mpg\\ntabulate foreign"
    - 加载并分析: "use \\"C:/data/mydata.dta\\", clear\\ndescribe\\nsummarize"

    Args:
        command: Stata 命令，多条命令用 \\n 分隔。

    Returns:
        Stata 输出文本（结果表格、统计量、日志消息等）。
    """
    return _run_stata_command(command)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
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


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def stata_graph(command: str) -> str:
    """生成 Stata 图形并返回描述信息。

    注意：MCP 模式无法直接展示图形，建议使用 graph export 导出为文件。

    使用示例：
    - "scatter mpg weight"
    - "histogram price, frequency"
    - "graph export \\"C:/output/scatter.png\\", replace"

    Args:
        command: 图形命令（scatter, histogram, twoway 等）。

    Returns:
        图形生成确认信息。
    """
    return _run_stata_command(command)


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
def stata_status() -> str:
    """获取当前 Stata 会话状态。

    显示当前加载的数据集、变量数量、观测数量、工作目录和内存使用情况。

    Returns:
        会话状态摘要。
    """
    return _run_stata_command("describe")


# =============================================================================
# 入口
# =============================================================================

if __name__ == "__main__":
    mcp.run(transport="stdio")
