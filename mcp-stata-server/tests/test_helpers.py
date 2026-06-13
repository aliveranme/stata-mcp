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
