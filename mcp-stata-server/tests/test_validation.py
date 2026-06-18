"""Tests for input validation helpers."""

import pytest

from server import (
    _has_dangerous_command_prefix,
    _validate_no_injection,
    _validate_varlist,
)


@pytest.mark.parametrize(
    "cmd",
    [
        "!dir",
        "! rm -rf /",  # purely a test payload string, never executed
        "shell notepad.exe",
        # Intentionally dangerous payload string for filter testing; never executed.
        "python: __import__('os').popen('whoami')",
        "python (print(1))",
        " summarize mpg\n!dir",
    ],
)
def test_dangerous_command_prefix_blocks(cmd):
    assert _has_dangerous_command_prefix(cmd) is not None


@pytest.mark.parametrize(
    "cmd",
    [
        "summarize mpg",
        "regress price mpg weight",
        "capture noisily { twoway scatter price weight }",
    ],
)
def test_dangerous_command_prefix_allows_safe(cmd):
    assert _has_dangerous_command_prefix(cmd) is None


def test_validate_no_injection_rejects_newline_and_semicolon():
    assert _validate_no_injection("a\nb", "x") is not None
    assert _validate_no_injection("a;b", "x") is not None
    assert _validate_no_injection("ok", "x") is None


def test_validate_varlist_allows_stata_extensions():
    assert _validate_varlist("i.foreign mpg L.price c.price##i.foreign [aw=weight]") is None
    assert _validate_varlist("x1-x10 mpg*") is None


def test_validate_varlist_rejects_dangerous_chars():
    assert _validate_varlist("mpg\nuse auto, clear") is not None
    assert _validate_varlist("mpg; use auto") is not None


def test_has_unsafe_brace_detects_unmatched_closing():
    from server import _has_unsafe_brace

    # 字符串外的 } 应检测为不安全
    assert _has_unsafe_brace("scatter price weight }") is True
    assert _has_unsafe_brace("} scatter price weight") is True


def test_has_unsafe_brace_allows_balanced_or_none():
    from server import _has_unsafe_brace

    # 无花括号 → 安全
    assert _has_unsafe_brace("scatter price weight") is False
    # 均衡的 { } → 安全
    assert _has_unsafe_brace("capture noisily { scatter price weight }") is False


def test_has_unsafe_brace_allows_brace_inside_string():
    from server import _has_unsafe_brace

    # 字符串内的 } 应视为安全
    assert _has_unsafe_brace('scatter price weight, title("a} b")') is False


def test_return_type_str_toolresult_consistency():
    """所有 MCP 工具函数的返回类型都应为 str | ToolResult。"""
    import inspect

    from server import (
        stata_codebook,
        stata_describe,
        stata_display,
        stata_export_excel,
        stata_find_package,
        stata_graph,
        stata_install_package,
        stata_list,
        stata_list_packages,
        stata_logistic,
        stata_more,
        stata_ping,
        stata_regress,
        stata_run,
        stata_run_do_file,
        stata_save_dataset,
        stata_set_cwd,
        stata_status,
        stata_summarize,
        stata_tabulate,
        stata_ttest,
        stata_use_dataset,
    )

    tools_with_str_only_return = []
    for name, func in [
        ("stata_run", stata_run),
        ("stata_run_do_file", stata_run_do_file),
        ("stata_use_dataset", stata_use_dataset),
        ("stata_save_dataset", stata_save_dataset),
        ("stata_set_cwd", stata_set_cwd),
        ("stata_describe", stata_describe),
        ("stata_summarize", stata_summarize),
        ("stata_list", stata_list),
        ("stata_codebook", stata_codebook),
        ("stata_tabulate", stata_tabulate),
        ("stata_display", stata_display),
        ("stata_regress", stata_regress),
        ("stata_logistic", stata_logistic),
        ("stata_ttest", stata_ttest),
        ("stata_graph", stata_graph),
        ("stata_export_excel", stata_export_excel),
        ("stata_install_package", stata_install_package),
        ("stata_find_package", stata_find_package),
        ("stata_list_packages", stata_list_packages),
        ("stata_more", stata_more),
        ("stata_status", stata_status),
        ("stata_ping", stata_ping),
    ]:
        sig = inspect.signature(func)
        ret = sig.return_annotation
        ret_str = str(ret)
        if "ToolResult" not in ret_str:
            tools_with_str_only_return.append(name)
    assert not tools_with_str_only_return, (
        f"以下工具返回类型应为 str | ToolResult，但缺少 ToolResult: {tools_with_str_only_return}"
    )
