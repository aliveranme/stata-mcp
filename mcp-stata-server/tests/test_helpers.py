import os
from unittest.mock import patch

import pytest

from server import (
    _cleanup_temp_block,
    _ensure_java_headless,
    _file_written_since,
    _format_size,
    _graph_size_options,
    _materialize_block,
    _mtime_ns,
    _normalize_path,
    _paginate,
)


def test_ensure_java_headless_appends_default_when_unspecified(monkeypatch):
    monkeypatch.delenv("JAVA_TOOL_OPTIONS", raising=False)

    assert _ensure_java_headless() is True
    assert os.environ["JAVA_TOOL_OPTIONS"] == "-Djava.awt.headless=true"


@pytest.mark.parametrize(
    "existing",
    [
        "-Xmx1g -Djava.awt.headless=true",
        "-Djava.awt.headless=false -Xmx1g",
    ],
)
def test_ensure_java_headless_preserves_explicit_setting(monkeypatch, existing):
    monkeypatch.setenv("JAVA_TOOL_OPTIONS", existing)

    assert _ensure_java_headless() is False
    assert os.environ["JAVA_TOOL_OPTIONS"] == existing


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
    assert "语法无效" in result
    assert "var not found" in result


@pytest.mark.parametrize(
    ("rc", "keyword"),
    [
        # 真机（Stata 19.5 MP）逐条触发核对过的释义，防止再次凭印象改写。
        # 括号内是 Stata 自己的原文与触发方式。
        (4, "未保存"),        # no; dataset in memory has changed since last saved
        (5, "排序"),          # not sorted
        (9, "assert"),        # assertion is false ← 旧表误标为「变量类型不匹配」
        (109, "类型不匹配"),   # type mismatch ← 「变量类型不匹配」实际属于这里
        (110, "已存在"),       # variable already defined
        (111, "未找到"),       # variable not found
        (199, "命令不存在"),   # command is unrecognized ← 旧表误标为「选项语法错误」
        (459, "唯一识别"),     # does not uniquely identify the observations（isid）
        (601, "文件不存在"),   # file not found
        (2000, "没有观测值"),  # no observations
    ],
)
def test_format_error_rc_messages_match_real_stata(rc, keyword):
    from server import _format_error

    assert keyword in _format_error(rc, "cmd", "")


def test_format_error_drops_unverified_codes():
    """未经真机核对的返回码应退化为「未知返回码」，其后紧跟 Stata 原文。

    给错方向比不给更糟：释义拼在 Stata 报错**之前**，是 Agent 首先读到的一行。
    """
    from server import _format_error

    for rc in (6, 8, 10, 20, 99):
        assert f"未知返回码({rc})" in _format_error(rc, "cmd", "")


def test_format_error_rc459_uses_xtset_context_for_xtreg():
    from server import _format_error

    result = _format_error(459, "xtreg price weight, fe", "must specify panelvar; use xtset")
    assert "xtset/tsset" in result
    assert "唯一识别" not in result


def test_format_error_rc459_keeps_isid_context():
    from server import _format_error

    result = _format_error(459, "isid id", "variables id do not uniquely identify the observations")
    assert "唯一识别" in result


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


def test_graph_size_inch_formats_drop_pixel_values():
    """英寸制格式（pdf 及 Windows 的 emf/wmf）不能收到像素值。"""
    for ext in (".pdf", ".emf", ".wmf"):
        opts, _ = _graph_size_options(f"/tmp/a{ext}", 800, 0)
        assert opts == "", f"{ext} 按英寸计，应忽略像素宽度"


def test_graph_size_svg_uses_pixels_not_inches():
    """实测 Stata 19.5 MP：svg 输出头写作 width="800px"，width() 是像素。

    把 svg 当英寸会两头错：合法的 800 被丢弃；而 width=6 会产出 6 像素的废图，
    文件却写出成功 —— 静默失败。
    """
    opts, note = _graph_size_options("/tmp/a.svg", 800, 600)
    assert opts == "width(800) height(600)"
    assert note == ""


def test_graph_size_postscript_rejects_size_options():
    """实测：eps/ps 传任何 width()/height() 都是 option width() not allowed → r(198)。"""
    for ext in (".eps", ".ps"):
        opts, note = _graph_size_options(f"/tmp/a{ext}", 800, 600)
        assert opts == "", f"{ext} 不支持尺寸选项"
        assert "不支持" in note, f"{ext} 丢弃参数必须告知调用方"


def test_graph_size_postscript_silent_when_no_size_requested():
    opts, note = _graph_size_options("/tmp/a.eps", 0, 0)
    assert opts == ""
    assert note == "", "没传尺寸就没什么可提示的"


def test_graph_size_vector_keeps_upper_bound():
    """20 英寸是 Stata 允许的上界，不能连同越界值一起丢掉。"""
    opts, note = _graph_size_options("/tmp/a.pdf", 20, 0)
    assert opts == "width(20)"
    assert note == ""


def test_graph_size_vector_drops_just_over_upper_bound():
    opts, note = _graph_size_options("/tmp/a.pdf", 21, 0)
    assert opts == ""
    assert "width=21" in note


def test_graph_size_vector_keeps_lower_bound():
    """int 参数下 1 是能表达的最小合法英寸值。"""
    opts, note = _graph_size_options("/tmp/a.pdf", 1, 0)
    assert opts == "width(1)"
    assert note == ""


def test_graph_size_vector_names_every_dropped_dimension():
    """只报 width 会让调用方以为 height 生效了。"""
    opts, note = _graph_size_options("/tmp/a.pdf", 800, 600)
    assert opts == ""
    assert "width=800" in note
    assert "height=600" in note


def test_graph_size_matches_extension_case_insensitively():
    """.PDF 若被当成位图会收到像素值 → r(198)。"""
    opts, note = _graph_size_options("/tmp/A.PDF", 800, 0)
    assert opts == ""
    assert "英寸" in note


def test_graph_size_without_extension_falls_back_to_pixels():
    """无扩展名时 Stata 按默认位图处理，尺寸单位仍是像素。"""
    opts, note = _graph_size_options("/tmp/noext", 800, 600)
    assert opts == "width(800) height(600)"
    assert note == ""


def test_graph_size_inch_format_omits_options_when_all_unset():
    opts, note = _graph_size_options("/tmp/a.pdf", 0, 0)
    assert opts == ""
    assert note == "", "没传尺寸就不该有「已忽略」的提示"


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


def test_format_size_megabytes():
    """大图导出常在 MB 级；不走 MB 分支会显示成四位数 KB。"""
    with patch("server.os.path.getsize", return_value=3 * 1024 * 1024 + 512 * 1024):
        assert _format_size("/tmp/big.png") == "3.5 MB"


def test_mtime_ns_returns_none_for_missing_file(tmp_path):
    assert _mtime_ns(str(tmp_path / "gone.png")) is None


def test_file_written_since_treats_stat_race_as_written(tmp_path):
    """isfile 之后文件被替换 → 拿不到 mtime。宁可报成功，也不把成功的导出报成失败。"""
    p = tmp_path / "fig.png"
    p.write_bytes(b"x")
    before = os.stat(p).st_mtime_ns
    with patch("server._mtime_ns", return_value=None):
        assert _file_written_since(str(p), before) is True


def test_file_written_since_false_when_file_is_deleted_after_start(tmp_path):
    """导出产物在 stat 竞态后被删除时，不能误报为成功。"""
    p = tmp_path / "fig.png"
    p.write_bytes(b"old")
    before = os.stat(p).st_mtime_ns
    p.unlink()
    assert _file_written_since(str(p), before) is False


# ============================================================================
# do 文件的 ssc install 拆分（_extract_ssc_installs）
# ============================================================================
from server import _extract_ssc_installs  # noqa: E402


def test_extract_ssc_plain_and_replace():
    cleaned, installs = _extract_ssc_installs("ssc install estout\nssc install winsor2, replace")
    assert installs == [("estout", False), ("winsor2", True)]
    # 安装行改注释，非安装内容不受影响
    assert all(line.startswith("* [stata-mcp]") for line in cleaned.split("\n"))


def test_extract_ssc_with_prefixes():
    for src, pkg in [
        ("qui ssc install reghdfe", "reghdfe"),
        ("cap noi ssc install ivreg2", "ivreg2"),
        ("  ssc install coefplot ", "coefplot"),
        ("QUIETLY SSC INSTALL Estout", "Estout"),
    ]:
        _cleaned, installs = _extract_ssc_installs(src)
        assert installs == [(pkg, False)], src


def test_extract_ssc_preserves_line_numbers():
    src = "sysuse auto, clear\nssc install estout\nregress price weight"
    cleaned, installs = _extract_ssc_installs(src)
    assert installs == [("estout", False)]
    lines = cleaned.split("\n")
    assert len(lines) == 3, "行号必须保持不变（安装行改注释而非删除）"
    assert lines[0] == "sysuse auto, clear"
    assert lines[2] == "regress price weight"
    assert lines[1].startswith("* [stata-mcp]")


def test_extract_ssc_dedup():
    _cleaned, installs = _extract_ssc_installs("ssc install estout\nssc install estout, replace")
    assert installs == [("estout", False)], "同包去重，保留首次出现"


def test_extract_ssc_no_installs_untouched():
    src = "sysuse auto, clear\nregress price weight\ngraph export x.png"
    cleaned, installs = _extract_ssc_installs(src)
    assert installs == []
    assert cleaned == src, "无 ssc install 时文本逐字不变"


def test_extract_ssc_ignores_non_install_ssc():
    """ssc describe / uninstall 不是安装，不应被拆出。"""
    _cleaned, installs = _extract_ssc_installs("ssc describe estout\nssc uninstall foo")
    assert installs == []


def test_graph_size_emf_has_no_override_options():
    """实测 `help emf_options` 不存在，graph export 的 override_options 表也无 emf。

    旧实现把 .emf 归入英寸组，会把 width(6) 传下去 —— emf 不接受任何尺寸选项。
    """
    opts, note = _graph_size_options("/tmp/a.emf", 6, 4)
    assert opts == ""
    assert "不支持" in note


def test_graph_size_wmf_is_not_an_official_format():
    """.wmf 根本不在 graph export 的格式表里，同样不该收到尺寸选项。"""
    opts, note = _graph_size_options("/tmp/a.wmf", 6, 4)
    assert opts == ""
    assert "不支持" in note


# --- 格式专属导出选项（quality / mag / fontface）------------------------------
# 依据各 [G-3] *_options 条目并在 Stata 19.5 MP 实测：
#   quality() 仅 jpg（1–100）；png/pdf 报 option quality() not allowed
#   mag()     仅 pdf/eps/ps（1–10000）；png/jpg/svg 报 option mag() not allowed
#   fontface() 仅 pdf/eps/ps/svg
from server import _graph_format_options  # noqa: E402


def test_graph_format_quality_only_for_jpg():
    opts, note = _graph_format_options("/tmp/a.jpg", quality=60, mag=0, fontface="")
    assert opts == "quality(60)"
    assert note == ""


def test_graph_format_quality_dropped_for_other_formats():
    """png 传 quality 会 option quality() not allowed，错误又被复合块 capture 吞掉。"""
    for ext in (".png", ".pdf", ".svg"):
        opts, note = _graph_format_options(f"/tmp/a{ext}", quality=60, mag=0, fontface="")
        assert opts == "", f"{ext} 不支持 quality()"
        assert "quality" in note and "不支持" in note


def test_graph_format_mag_for_postscript_family():
    for ext in (".pdf", ".eps", ".ps"):
        opts, note = _graph_format_options(f"/tmp/a{ext}", quality=0, mag=150, fontface="")
        assert opts == "mag(150)", f"{ext} 支持 mag()"
        assert note == ""


def test_graph_format_mag_dropped_for_bitmap_and_svg():
    for ext in (".png", ".jpg", ".svg"):
        opts, note = _graph_format_options(f"/tmp/a{ext}", quality=0, mag=150, fontface="")
        assert opts == "", f"{ext} 不支持 mag()"
        assert "mag" in note


def test_graph_format_fontface_quoted_for_vector_formats():
    """字体名可能含空格（Times New Roman），必须用双引号包裹。"""
    for ext in (".pdf", ".eps", ".ps", ".svg"):
        opts, note = _graph_format_options(
            f"/tmp/a{ext}", quality=0, mag=0, fontface="Times New Roman"
        )
        assert opts == 'fontface("Times New Roman")', ext
        assert note == ""


def test_graph_format_fontface_dropped_for_bitmap():
    opts, note = _graph_format_options("/tmp/a.png", quality=0, mag=0, fontface="Helvetica")
    assert opts == ""
    assert "fontface" in note


def test_graph_format_combines_applicable_options():
    opts, note = _graph_format_options(
        "/tmp/a.pdf", quality=0, mag=150, fontface="Helvetica"
    )
    assert opts == 'mag(150) fontface("Helvetica")'
    assert note == ""


def test_graph_format_silent_when_nothing_requested():
    for ext in (".png", ".pdf", ".jpg"):
        opts, note = _graph_format_options(f"/tmp/a{ext}", quality=0, mag=0, fontface="")
        assert opts == ""
        assert note == ""


def test_graph_format_jpeg_suffix_is_not_official():
    """实测 .jpeg → translator Graph2jpeg not found；官方后缀表只有 jpg。"""
    opts, note = _graph_format_options("/tmp/a.jpeg", quality=60, mag=0, fontface="")
    assert opts == "", ".jpeg 不是官方后缀，不该当 jpg 处理"
    assert "quality" in note


# ---------------------------------------------------------------------------
# compact 输出压缩
# ---------------------------------------------------------------------------


def test_compact_removes_count_lines_keeps_tables():
    from server import _compact_output

    text = (
        "sysuse auto\n"
        "\n"
        "(22 real changes made)\n"
        "\n"
        "    Variable |        Obs        Mean\n"
        "-------------+-------------------------\n"
        "       price |         74    6165.257\n"
        "\n"
        "\n"
        "\n"
        "(1 observation deleted)\n"
        "done"
    )
    out = _compact_output(text)
    assert "(22 real changes made)" not in out
    assert "(1 observation deleted)" not in out
    assert "price |" in out  # 结果表保留
    assert "6165.257" in out
    assert "done" in out
    assert "\n\n\n" not in out  # 空行被折叠


def test_compact_keeps_error_text():
    from server import _compact_output

    text = "(10 real changes made)\n[返回码: 601] file not found\nsome context"
    out = _compact_output(text)
    assert "[返回码: 601] file not found" in out
    assert "some context" in out
