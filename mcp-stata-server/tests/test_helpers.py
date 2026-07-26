import os

from server import (
    _cleanup_temp_block,
    _file_written_since,
    _format_size,
    _graph_size_options,
    _materialize_block,
    _normalize_path,
    _paginate,
)


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


# --- 多行块落盘 --------------------------------------------------------------
# StataSO_Execute 是单命令接口：多行输入会被当成同一条命令的续写，
# 含 { } 时报 "code follows on the same line as open brace"，
# if{} / program define 更会挂死会话。故多行块须写入临时 do 文件后 include。


def test_materialize_block_passes_single_line_through():
    """单行走 StataSO_Execute 快路径（实测 12ms vs include 257ms），不落盘。"""
    assert _materialize_block("summarize price") == ("summarize price", None)


def test_materialize_block_writes_multiline_to_temp_do():
    block = 'forvalues i = 1/3 {\n    display `i\'\n}'
    cmd, path = _materialize_block(block)
    assert cmd == f'include "{path}"'
    with open(path, encoding="utf-8") as f:
        written = f.read()
    assert written.startswith("forvalues i = 1/3 {")
    assert written.endswith("\n"), "do 文件须以换行结尾，否则 Stata 会漏掉末行"
    os.unlink(path)


def test_materialize_block_trailing_newline_not_doubled():
    _cmd, path = _materialize_block("display 1\ndisplay 2\n")
    with open(path, encoding="utf-8") as f:
        assert f.read() == "display 1\ndisplay 2\n"
    os.unlink(path)


def test_cleanup_temp_block_removes_file():
    """长驻进程里每个多行块留一个文件会累积（实测 50 块 → 50 文件），须即用即删。"""
    _cmd, path = _materialize_block("display 1\ndisplay 2")
    assert os.path.exists(path)
    _cleanup_temp_block(path)
    assert not os.path.exists(path)


def test_cleanup_temp_block_tolerates_none_and_missing(tmp_path):
    """单行路径传入 None；文件已被清掉时也不能抛。"""
    _cleanup_temp_block(None)
    _cleanup_temp_block(str(tmp_path / "never_existed.do"))


# --- 图形导出尺寸单位 --------------------------------------------------------
# 位图的 width()/height() 以像素计，矢量格式以英寸计（0.5–20）；
# 对 .pdf 传 width(800) 会 r(198) "must be a number between 0.5 and 20"。


def test_graph_size_bitmap_uses_pixels():
    opts, note = _graph_size_options("/tmp/a.png", 800, 600)
    assert opts == "width(800) height(600)"
    assert note == ""


def test_graph_size_bitmap_omits_unset_height():
    opts, _ = _graph_size_options("/tmp/a.png", 800, 0)
    assert opts == "width(800)"


def test_graph_size_vector_drops_pixel_values_and_explains():
    opts, note = _graph_size_options("/tmp/a.pdf", 800, 0)
    assert opts == ""
    assert "英寸" in note and "width=800" in note


def test_graph_size_vector_keeps_inch_values():
    opts, note = _graph_size_options("/tmp/a.pdf", 6, 4)
    assert opts == "width(6) height(4)"
    assert note == ""


def test_graph_size_vector_covers_common_extensions():
    for ext in (".pdf", ".eps", ".ps", ".svg", ".emf", ".wmf"):
        opts, _ = _graph_size_options(f"/tmp/a{ext}", 800, 0)
        assert opts == "", f"{ext} 应按矢量格式忽略像素宽度"


# --- 写入判定与大小格式 ------------------------------------------------------


def test_file_written_since_detects_new_file(tmp_path):
    p = tmp_path / "new.png"
    p.write_bytes(b"x")
    assert _file_written_since(str(p), None) is True


def test_file_written_since_false_when_missing(tmp_path):
    assert _file_written_since(str(tmp_path / "nope.png"), None) is False


def test_file_written_since_false_when_untouched(tmp_path):
    """replace=False 且目标已存在时 Stata 拒绝写入，文件仍在——不能算成功。"""
    p = tmp_path / "old.png"
    p.write_bytes(b"x")
    before = os.stat(p).st_mtime_ns
    assert _file_written_since(str(p), before) is False


def test_file_written_since_true_when_overwritten(tmp_path):
    p = tmp_path / "old.png"
    p.write_bytes(b"x")
    before = os.stat(p).st_mtime_ns
    os.utime(p, ns=(before + 1_000_000, before + 1_000_000))
    assert _file_written_since(str(p), before) is True


def test_format_size_small_file_uses_bytes(tmp_path):
    """117 字节的回归结果表按整数 KB 会显示成 0 KB，看着像导出失败。"""
    p = tmp_path / "small.csv"
    p.write_bytes(b"x" * 117)
    assert _format_size(str(p)) == "117 B"


def test_format_size_kilobytes(tmp_path):
    p = tmp_path / "mid.png"
    p.write_bytes(b"x" * 2048)
    assert _format_size(str(p)) == "2.0 KB"


def test_format_size_missing_file_is_graceful(tmp_path):
    assert _format_size(str(tmp_path / "gone.png")) == "大小未知"
