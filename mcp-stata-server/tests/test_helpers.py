import os

from server import _normalize_path, _paginate


def test_normalize_path_converts_backslashes_to_slashes():
    normalized = _normalize_path(r"data\subdir\file.dta")
    assert "/" in normalized
    assert "\\" not in normalized


def test_normalize_path_makes_absolute():
    normalized = _normalize_path("relative/path.dta")
    assert os.path.isabs(normalized)


def test_paginate_page_zero_returns_full_text():
    text = "a" * 10_000
    assert _paginate(text, 0) == text


def test_paginate_empty_text():
    assert _paginate("", 1) == "(无输出)"


def test_paginate_first_page_includes_header_and_content():
    text = "b" * 1_000
    result = _paginate(text, 1, page_size=100)
    assert result.startswith("── 第 1/10 页")
    assert "b" * 100 in result
    assert "第 1/10 页" in result


def test_paginate_out_of_range_page_clamps():
    text = "short"
    # page < 1 clamps to first page; page > total_pages clamps to last page.
    assert "第 1/1 页" in _paginate(text, -5, page_size=10)
    assert "第 1/1 页" in _paginate(text, 100, page_size=10)
    # page == 0 means "return all" without pagination header.
    assert _paginate(text, 0, page_size=10) == text


def test_paginate_middle_page_has_both_nav_links():
    """中间页应同时含「翻上页」和「翻下页」导航。"""
    text = "a" * 300
    result = _paginate(text, 2, page_size=100)
    assert "第 2/3 页" in result
    assert "stata_more(page=1)" in result  # 翻上页
    assert "stata_more(page=3)" in result  # 翻下页


def test_paginate_last_page_has_no_next_link():
    """末页应含「翻上页」但无「翻下页」。"""
    text = "a" * 300
    result = _paginate(text, 3, page_size=100)
    assert "第 3/3 页" in result
    assert "stata_more(page=2)" in result
    assert "stata_more(page=4)" not in result


def test_paginate_page_size_zero_returns_full_text():
    """page_size <= 0 与 page=0 同分支，返回原文。"""
    assert _paginate("abc", 1, page_size=0) == "abc"
    assert _paginate("abc", 1, page_size=-1) == "abc"


def test_format_error_known_rc():
    from server import _format_error

    result = _format_error(198, "regress bad", "var not found")
    assert "[返回码: 198]" in result
    assert "命令语法错误" in result
    assert "var not found" in result


def test_format_error_unknown_rc():
    from server import _format_error

    result = _format_error(500, "x", "")
    assert "未知返回码(500)" in result
    # 无输出时不附加多余换行
    assert not result.endswith("\n")


def test_format_error_snippet_truncated_to_60():
    from server import _format_error

    long_cmd = "y" * 200
    result = _format_error(198, long_cmd, "out")
    # snippet 截断到 60 字符
    assert long_cmd not in result


def test_result_or_error_wraps_error_prefixes():
    from server import _result_or_error

    # 各错误前缀应被包装为 ToolResult(is_error=True)
    for err_str in [
        "错误: 路径非法",
        "错误：全角冒号",
        "[错误] something",
        "[返回码: 198] 语法错误",
        "(无有效命令)",
    ]:
        result = _result_or_error(err_str)
        assert hasattr(result, "is_error"), f"应包装为 ToolResult: {err_str}"
        assert result.is_error is True


def test_result_or_error_passes_through_success_text():
    from server import _result_or_error

    # 纯成功文本应原样返回 str（不包装）
    result = _result_or_error("summarize mpg 的输出结果")
    assert isinstance(result, str)
    assert result == "summarize mpg 的输出结果"


def test_result_or_error_passes_through_toolresult():
    from fastmcp.tools.base import ToolResult

    from server import _result_or_error

    existing = ToolResult(content="已有错误", is_error=True)
    # 已是 ToolResult 应原样透传同一对象
    assert _result_or_error(existing) is existing
