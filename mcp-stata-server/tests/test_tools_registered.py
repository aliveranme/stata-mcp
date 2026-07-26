import asyncio
import importlib.util
import os
import sys

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SERVER_PATH = os.path.join(_SERVER_DIR, "server.py")

# 完整清单而非数量下界：原先的 `>= 20` 兜一个 22 工具的服务器，
# 掉 2 个工具也不报警，而工具消失对 Agent 是静默的能力回退。
EXPECTED_TOOLS = {
    # 核心执行
    "stata_run",
    "stata_run_do_file",
    # 数据管理
    "stata_use_dataset",
    "stata_import",
    "stata_save_dataset",
    "stata_set_cwd",
    "stata_generate",
    "stata_egen",
    "stata_xtset",
    "stata_merge",
    "stata_append",
    "stata_reshape",
    "stata_collapse",
    "stata_frame",
    "stata_verify",
    "stata_use_example",
    # 数据探索
    "stata_describe",
    "stata_codebook",
    "stata_summarize",
    "stata_list",
    "stata_tabulate",
    "stata_display",
    "stata_correlate",
    # 分析
    "stata_regress",
    "stata_logistic",
    "stata_ttest",
    "stata_probit",
    "stata_poisson",
    "stata_xtreg",
    "stata_ivregress",
    # 后估计
    "stata_margins",
    "stata_test",
    "stata_predict",
    "stata_estat",
    "stata_estimates",
    "stata_return_list",
    # 图形与导出
    "stata_graph",
    "stata_scheme",
    "stata_export_excel",
    "stata_export_delimited",
    # 包管理与帮助
    "stata_install_package",
    "stata_uninstall_package",
    "stata_describe_package",
    "stata_find_package",
    "stata_list_packages",
    "stata_help",
    # 会话
    "stata_more",
    "stata_status",
    "stata_ping",
}


def _load_server():
    module_name = "stata_server_test_import"
    if module_name in sys.modules:
        del sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, _SERVER_PATH)
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    return server


def test_registered_tools_match_expected_set():
    server = _load_server()
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert names == EXPECTED_TOOLS, (
        f"缺失: {sorted(EXPECTED_TOOLS - names)}；意外新增: {sorted(names - EXPECTED_TOOLS)}"
    )


def test_every_tool_has_description():
    """docstring 是 Agent 选择工具的唯一依据，缺失即等于该工具不可发现。"""
    server = _load_server()
    missing = [t.name for t in asyncio.run(server.mcp.list_tools()) if not (t.description or "").strip()]
    assert not missing, f"以下工具缺少描述: {missing}"


def test_write_tools_are_not_marked_read_only():
    """会写文件或改会话状态的工具不能标 readOnlyHint —— 客户端据此决定是否需要确认。"""
    server = _load_server()
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    must_not_be_readonly = {
        "stata_run_do_file",
        "stata_use_dataset",
        "stata_import",
        "stata_save_dataset",
        "stata_set_cwd",
        "stata_graph",
        # action="set" 会改变会话（甚至用 permanently 写进配置），非只读
        "stata_scheme",
        "stata_export_excel",
        "stata_export_delimited",
        "stata_install_package",
        "stata_uninstall_package",
        # 以下会创建变量，改动内存中的数据集
        "stata_generate",
        "stata_egen",
        "stata_xtset",
        "stata_estimates",
        "stata_use_example",
        "stata_merge",
        "stata_append",
        "stata_reshape",
        "stata_collapse",
        "stata_frame",
        "stata_predict",
    }
    wrong = [
        name
        for name in must_not_be_readonly
        if getattr(getattr(tools[name], "annotations", None), "readOnlyHint", False)
    ]
    assert not wrong, f"以下会产生副作用的工具被标为只读: {wrong}"


# ============================================================================
# 文档一致性 —— 防止文档与实现漂移
# ============================================================================
# 本轮仓库审查发现 5 处文档失实，全都不会让任何测试变红。代码有 491 单元 +
# 81 E2E + 变异验证守着，文档却没有任何自动化守卫，故补上。

import json  # noqa: E402
import re  # noqa: E402

_REPO_ROOT = os.path.dirname(_SERVER_DIR)
_DOCS = (
    os.path.join(_REPO_ROOT, "CLAUDE.md"),
    os.path.join(_REPO_ROOT, "README.md"),
    os.path.join(_REPO_ROOT, ".claude", "skills", "stata", "SKILL.md"),
)


def _doc_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _registered_tool_names():
    server = _load_server()
    return {t.name for t in asyncio.run(server.mcp.list_tools())}


def test_docs_do_not_mention_nonexistent_tools():
    """文档写了但代码里没有的工具 —— Agent 会照着调，然后失败。"""
    real = _registered_tool_names()
    for path in _DOCS:
        mentioned = set(re.findall(r"`(stata_\w+)`", _doc_text(path)))
        ghosts = sorted(mentioned - real)
        assert not ghosts, f"{os.path.basename(path)} 提到了不存在的工具: {ghosts}"


def test_docs_cover_every_registered_tool():
    """代码里有但文档没提的工具 —— Agent 不知道它存在，等于没有。"""
    real = _registered_tool_names()
    for path in _DOCS:
        mentioned = set(re.findall(r"`(stata_\w+)`", _doc_text(path)))
        missing = sorted(real - mentioned)
        assert not missing, f"{os.path.basename(path)} 未提及: {missing}"


# 只匹配「声明服务器规模」的计数，不匹配叙述性的「给 14 个工具补上参数」。
_COUNT_PATTERNS = (
    r"MCP 工具（(\d+) 个）",              # 章节标题
    r"MCP 执行层：(\d+) 个工具",           # CLAUDE.md 架构图
    r"把 (\d+) 个工具",                   # README 正文
    r"主程序（(\d+) 个工具）",             # README 项目结构
    r"`stata`，(\d+) 个工具",             # SKILL.md
    r"badge/tools-(\d+)-",               # README 徽章
)


def test_tool_count_in_docs_matches_reality():
    """「49 个工具」这类计数散落在多份文档里，加工具时最容易忘记同步。

    只看声明服务器规模的位置 —— 缺陷历史表里「给 14 个工具补上参数」这类
    叙述文字不是计数声明，不该被误判。
    """
    n = len(_registered_tool_names())
    for path in _DOCS:
        text = _doc_text(path)
        counts = {
            int(c) for pat in _COUNT_PATTERNS for c in re.findall(pat, text)
        }
        wrong = sorted(c for c in counts if c != n)
        assert not wrong, f"{os.path.basename(path)} 的工具计数 {wrong} 与实际 {n} 不符"


def test_claude_md_permission_block_matches_settings_json():
    """CLAUDE.md 曾声称配了 enableAllProjectMcpServers，实际文件里并没有。"""
    settings_path = os.path.join(_REPO_ROOT, ".claude", "settings.json")
    with open(settings_path, encoding="utf-8") as f:
        settings = json.load(f)
    claude_md = _doc_text(os.path.join(_REPO_ROOT, "CLAUDE.md"))

    if "enableAllProjectMcpServers" not in settings:
        # 允许在说明「没有这个键」的语境里提到它，但不能写成已配置的样子
        assert '"enableAllProjectMcpServers": true,  //' not in claude_md, (
            "CLAUDE.md 把 enableAllProjectMcpServers 写成已配置，"
            "但 .claude/settings.json 里没有这个键"
        )
