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
    "stata_save_dataset",
    "stata_set_cwd",
    "stata_generate",
    "stata_egen",
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
    # 图形与导出
    "stata_graph",
    "stata_export_excel",
    # 包管理与帮助
    "stata_install_package",
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
        "stata_save_dataset",
        "stata_set_cwd",
        "stata_graph",
        "stata_export_excel",
        "stata_install_package",
        # 以下会创建变量，改动内存中的数据集
        "stata_generate",
        "stata_egen",
        "stata_predict",
    }
    wrong = [
        name
        for name in must_not_be_readonly
        if getattr(getattr(tools[name], "annotations", None), "readOnlyHint", False)
    ]
    assert not wrong, f"以下会产生副作用的工具被标为只读: {wrong}"
