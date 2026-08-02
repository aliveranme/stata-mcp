"""图形绘制与导出的真实 Stata 端到端测试。

这里只放**单元测试无法证伪**的断言 —— 即代码对 Stata 实际行为所做的假设。
命令拼接、参数校验等不需要 Stata 的部分留在 ``tests/``。

运行：``.venv/bin/python -m pytest tests_e2e/ -q``（见 conftest 的目录说明）
"""

import pytest

from tests_e2e.conftest import SKIP_REASON, STATA_AVAILABLE, result_text

pytestmark = [
    pytest.mark.stata,
    pytest.mark.skipif(not STATA_AVAILABLE, reason=SKIP_REASON),
]


def _svg_header(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read(300).split("<svg", 1)[-1][:80]


# --- 图形导出 ----------------------------------------------------------------


def test_png_export_writes_a_real_file(auto_data, outdir):
    """复合块把 graph + export 原子执行；headless 下必须真的产出文件。"""
    target = outdir / "scatter.png"
    result = auto_data.stata_graph("scatter price weight", export=str(target), replace=True)
    assert not getattr(result, "is_error", False), result_text(result)
    assert target.stat().st_size > 1000, "图形文件不该只有几个字节"


def test_refused_overwrite_is_reported_as_failure(auto_data, outdir):
    """replace=False 且目标已存在 → Stata r(602)，文件仍在。

    复合块的 capture 让 rc 恒为 0，只看返回码会把这次失败报成成功；
    工具改用 mtime 判定，这条用例是那个判定的真机证据。
    """
    target = outdir / "scatter.png"
    auto_data.stata_graph("scatter price weight", export=str(target), replace=True)
    stale_mtime = target.stat().st_mtime_ns

    result = auto_data.stata_graph("scatter price weight", export=str(target))
    text = result_text(result)
    assert getattr(result, "is_error", False), "拒绝覆盖必须报错"
    assert "replace=True" in text, "要给出可操作的下一步"
    assert target.stat().st_mtime_ns == stale_mtime, "文件不该被动过"


def test_export_without_any_option_is_valid_stata_syntax(auto_data, outdir):
    """replace=False 且未设尺寸时命令是 `graph export "f.png",` —— 尾部裸逗号。

    实测 Stata 接受这种空选项列表；若哪天不接受，导出会整体失效。
    """
    target = outdir / "bare.png"
    result = auto_data.stata_graph("scatter price weight", export=str(target), width=0, height=0)
    assert not getattr(result, "is_error", False), result_text(result)
    assert target.is_file()


def test_bitmap_width_is_pixels(auto_data, outdir):
    """位图的 width() 以像素计：默认 800 与显式 1600 应产出不同尺寸的文件。"""
    small, large = outdir / "s.png", outdir / "l.png"
    auto_data.stata_graph("scatter price weight", export=str(small), width=400, replace=True)
    auto_data.stata_graph("scatter price weight", export=str(large), width=1600, replace=True)
    assert large.stat().st_size > small.stat().st_size


def test_bitmap_out_of_range_width_surfaces_stata_error(auto_data, outdir):
    """位图尺寸不在工具层预校验，靠 Stata 兜底 —— 前提是错误能透传出来。

    实测 width=6 → "width() must be an integer between 8 and 16,000"；
    错误被复合块 capture 吞掉，全靠「文件未写入」判定才没被报成成功。
    """
    target = outdir / "tiny.png"
    result = auto_data.stata_graph(
        "scatter price weight", export=str(target), width=6, replace=True
    )
    assert getattr(result, "is_error", False)
    assert "8 and 16,000" in result_text(result), "必须保留 Stata 的精确诊断"
    assert not target.exists()


def test_pdf_export_drops_pixel_width_instead_of_failing(auto_data, outdir):
    """pdf 的 width() 以英寸计（0.5–20）；默认值 800 若下传会 r(198) 导致导出失败。"""
    target = outdir / "fig.pdf"
    result = auto_data.stata_graph(
        "scatter price weight", export=str(target), width=800, replace=True
    )
    text = result_text(result)
    assert not getattr(result, "is_error", False), text
    assert target.is_file()
    assert "英寸" in text, "丢弃参数必须告知调用方"


def test_pdf_export_accepts_inch_width(auto_data, outdir):
    target = outdir / "fig6.pdf"
    result = auto_data.stata_graph(
        "scatter price weight", export=str(target), width=6, height=4, replace=True
    )
    assert not getattr(result, "is_error", False), result_text(result)
    assert target.is_file()


def test_svg_width_is_pixels_not_inches(auto_data, outdir):
    """svg 虽是矢量格式，width() 却以**像素**计 —— 输出头写作 width="800px"。

    曾被误归入英寸组：合法的 800 被丢弃，而 6 会产出 6 像素的废图且导出「成功」。
    """
    target = outdir / "fig.svg"
    result = auto_data.stata_graph(
        "scatter price weight", export=str(target), width=800, height=600, replace=True
    )
    assert not getattr(result, "is_error", False), result_text(result)
    header = _svg_header(str(target))
    assert 'width="800px"' in header, f"width 未生效：{header}"
    assert 'height="600px"' in header, f"height 未生效：{header}"


def test_postscript_export_drops_unsupported_size_options(auto_data, outdir):
    """eps/ps 完全不支持 width()/height()，传了就 option width() not allowed → r(198)。"""
    for ext in ("eps", "ps"):
        target = outdir / f"fig.{ext}"
        result = auto_data.stata_graph(
            "scatter price weight", export=str(target), width=6, replace=True
        )
        text = result_text(result)
        assert not getattr(result, "is_error", False), f".{ext} 应成功导出：{text}"
        assert target.is_file()
        assert "不支持" in text, f".{ext} 丢弃参数必须告知调用方"


def test_export_drops_cached_graphs(auto_data, outdir):
    """graph drop _all 在复合块外执行，导出后不该有图形对象残留。"""
    auto_data.stata_graph("scatter price weight", export=str(outdir / "a.png"), replace=True)
    listing = result_text(auto_data.stata_run("graph dir"))
    assert "Graph" not in listing, f"图形对象未清理：{listing}"


# --- 数据与结果导出 ----------------------------------------------------------


def test_export_excel_writes_dataset(auto_data, outdir):
    target = outdir / "data.xlsx"
    result = auto_data.stata_export_excel(str(target), replace=True)
    assert not getattr(result, "is_error", False), result_text(result)
    assert target.stat().st_size > 1000


def test_export_excel_accepts_sheet_name_with_parens(auto_data, outdir):
    """`)` 在 sheet 名里是故意放行的 —— sheet("Q1 (2024)") 是常见写法。"""
    target = outdir / "sub.xlsx"
    result = auto_data.stata_export_excel(
        str(target), varlist="make price mpg", sheet="Q1 (2024)", replace=True
    )
    assert not getattr(result, "is_error", False), result_text(result)
    assert target.is_file()


def test_export_excel_refuses_overwrite_without_replace(auto_data, outdir):
    """replace=False 且目标已存在 → Stata r(602)，文件不被改动。

    注意与 stata_graph 的差异：graph 导出的错误被复合块 capture 吞掉（rc=0），
    因而会走到 mtime 判定并附上「请传 replace=True」的提示；export excel 没有
    capture，rc=602 直接短路返回，用户只看到 Stata 那句谈 worksheet 的原文。
    """
    target = outdir / "data.xlsx"
    auto_data.stata_export_excel(str(target), replace=True)
    stale_mtime = target.stat().st_mtime_ns

    result = auto_data.stata_export_excel(str(target))
    text = result_text(result)
    assert getattr(result, "is_error", False), "拒绝覆盖必须报错"
    assert "602" in text, "要保留 Stata 的原始返回码"
    assert target.stat().st_mtime_ns == stale_mtime, "文件不该被动过"


def test_export_results_rewrites_xlsx_to_csv(auto_data, outdir):
    """esttab 不支持 xlsx/sheet()，路径必须被改写成 .csv 且真的写出内容。"""
    if "111" in result_text(auto_data.stata_run("capture which estout\ndisplay _rc")):
        pytest.skip("未安装 estout")

    auto_data.stata_run("regress price weight mpg")
    result = auto_data.stata_export_excel(str(outdir / "res.xlsx"), results=True, replace=True)
    text = result_text(result)
    assert not getattr(result, "is_error", False), text
    assert "已自动改用 CSV" in text
    csv_path = outdir / "res.csv"
    assert csv_path.is_file()
    assert "weight" in csv_path.read_text(encoding="utf-8", errors="replace")
    assert not (outdir / "res.xlsx").exists(), "不该同时留下空的 .xlsx"


def test_export_results_without_estimates_fails_loudly(auto_data, outdir):
    """没有估计结果时 esttab 报 r(301)，不能留下空文件并回报成功。

    必须用 ``ereturn clear``：``estimates clear`` 只清**存储的**估计，活跃的
    e() 仍在（实测清完 ``e(cmd)`` 依旧是 regress），esttab 照样能导出。
    """
    if "111" in result_text(auto_data.stata_run("capture which estout\ndisplay _rc")):
        pytest.skip("未安装 estout")

    auto_data.stata_run("ereturn clear")
    result = auto_data.stata_export_excel(str(outdir / "empty.csv"), results=True, replace=True)
    assert getattr(result, "is_error", False), result_text(result)


# --- 未导出模式 --------------------------------------------------------------


def test_plot_without_export_succeeds_headless(auto_data):
    """headless 下 set graphics off 生效，绘图命令不该挂起或报错。"""
    for cmd in ("scatter price weight", "histogram price", "twoway line price mpg"):
        result = auto_data.stata_graph(cmd)
        assert not getattr(result, "is_error", False), f"{cmd}: {result_text(result)}"


# ============================================================================
# 与官方能力边界对齐 —— 主题 / 格式选项 / 分隔文本导出
# ============================================================================


def test_default_scheme_is_left_untouched(auto_data, outdir):
    """不传 scheme 时导出前后 c(scheme) 必须不变。

    Stata 19 默认是 stcolor；旧实现每次绘图都 `set scheme s2color`，
    把用户的主题悄悄改掉且不还原。
    """
    before = result_text(auto_data.stata_run("display c(scheme)"))
    auto_data.stata_graph("scatter price weight", export=str(outdir / "a.png"), replace=True)
    after = result_text(auto_data.stata_run("display c(scheme)"))
    assert after.strip() == before.strip(), f"scheme 被改动：{before!r} → {after!r}"


def test_scheme_tool_lists_get_and_set(auto_data):
    """stata_scheme 的三个动作都要对真实 Stata 生效。"""
    listing = result_text(auto_data.stata_scheme())
    assert "s2color" in listing and "stcolor" in listing, listing[:200]

    original = result_text(auto_data.stata_scheme(action="get")).strip()
    try:
        auto_data.stata_scheme(action="set", scheme="s2mono")
        assert "s2mono" in result_text(auto_data.stata_scheme(action="get"))
    finally:
        auto_data.stata_scheme(action="set", scheme=original.splitlines()[-1].strip())


def test_graph_applies_scheme_when_requested(auto_data, outdir):
    """显式传 scheme 时确实影响产物 —— 彩色与灰度的 svg 内容应不同。"""
    color, mono = outdir / "c.svg", outdir / "m.svg"
    auto_data.stata_graph("scatter price weight", export=str(color), scheme="s2color", replace=True)
    auto_data.stata_graph("scatter price weight", export=str(mono), scheme="s2mono", replace=True)
    assert color.read_text(errors="replace") != mono.read_text(errors="replace")


def test_jpg_quality_is_applied(auto_data, outdir):
    """quality() 仅 jpg 支持；低质量文件必须明显更小。"""
    low, high = outdir / "l.jpg", outdir / "h.jpg"
    r1 = auto_data.stata_graph("scatter price weight", export=str(low), quality=10, replace=True)
    r2 = auto_data.stata_graph("scatter price weight", export=str(high), quality=100, replace=True)
    assert not getattr(r1, "is_error", False), result_text(r1)
    assert not getattr(r2, "is_error", False), result_text(r2)
    assert low.stat().st_size < high.stat().st_size


def test_quality_dropped_for_png_instead_of_failing(auto_data, outdir):
    """png 传 quality 会 option quality() not allowed，且错误被 capture 吞掉。"""
    target = outdir / "q.png"
    result = auto_data.stata_graph(
        "scatter price weight", export=str(target), quality=50, replace=True
    )
    text = result_text(result)
    assert not getattr(result, "is_error", False), text
    assert target.is_file()
    assert "quality" in text and "不支持" in text


def test_mag_applies_to_pdf_only(auto_data, outdir):
    """mag() 仅 pdf/eps/ps；png 上要被丢弃而不是让导出失败。"""
    scaled = outdir / "m.pdf"
    r = auto_data.stata_graph(
        "scatter price weight", export=str(scaled), width=0, mag=200, replace=True
    )
    assert not getattr(r, "is_error", False), result_text(r)
    assert scaled.is_file()

    png = outdir / "m.png"
    r2 = auto_data.stata_graph("scatter price weight", export=str(png), mag=200, replace=True)
    assert not getattr(r2, "is_error", False), result_text(r2)
    assert png.is_file()
    assert "mag" in result_text(r2)


def test_fontface_applies_to_vector_formats(auto_data, outdir):
    """fontface() 在 pdf/svg 上生效；字体名含空格也要能传。"""
    target = outdir / "f.svg"
    r = auto_data.stata_graph(
        "scatter price weight", export=str(target), fontface="Times New Roman", replace=True
    )
    assert not getattr(r, "is_error", False), result_text(r)
    assert "Times New Roman" in target.read_text(errors="replace")


def test_emf_and_wmf_get_no_size_options(auto_data, outdir):
    """两者无 override_options；本环境（Unix）还会直接拒绝创建，错误须透传。"""
    for ext in ("emf", "wmf"):
        result = auto_data.stata_graph(
            "scatter price weight", export=str(outdir / f"x.{ext}"), width=6, replace=True
        )
        text = result_text(result)
        assert getattr(result, "is_error", False), f".{ext} 在 Unix 上不可创建"
        assert "cannot create" in text, text[:150]


# --- export delimited --------------------------------------------------------


def test_export_delimited_writes_csv(auto_data, outdir):
    target = outdir / "d.csv"
    result = auto_data.stata_export_delimited(str(target), replace=True)
    assert not getattr(result, "is_error", False), result_text(result)
    head = target.read_text(errors="replace").splitlines()[0]
    assert head.startswith("make,price,mpg"), head


def test_export_delimited_tab_and_novarnames(auto_data, outdir):
    target = outdir / "d.txt"
    result = auto_data.stata_export_delimited(
        str(target), varlist="make price", delimiter="tab", novarnames=True, replace=True
    )
    assert not getattr(result, "is_error", False), result_text(result)
    first = target.read_text(errors="replace").splitlines()[0]
    assert "\t" in first
    assert "make" not in first, "novarnames 应去掉变量名首行"


def test_export_delimited_custom_delimiter_and_filters(auto_data, outdir):
    target = outdir / "d2.csv"
    result = auto_data.stata_export_delimited(
        str(target),
        varlist="make price",
        delimiter=";",
        condition="foreign == 1",
        in_range="1/5",
        replace=True,
    )
    assert not getattr(result, "is_error", False), result_text(result)
    lines = target.read_text(errors="replace").splitlines()
    assert ";" in lines[0]
    assert len(lines) <= 6, "if+in 应真的限制了行数（含表头）"


# --- export excel 补齐的官方选项 ---------------------------------------------


def test_export_excel_sheet_replace_resolves_conflict(auto_data, outdir):
    """官方对 worksheet 已存在的解法：sheet(..., replace)，且不能叠加文件级 replace。"""
    target = outdir / "d.xlsx"
    auto_data.stata_export_excel(str(target), sheet="Data", replace=True)
    result = auto_data.stata_export_excel(str(target), sheet="Data", sheet_mode="replace")
    assert not getattr(result, "is_error", False), result_text(result)


def test_export_excel_rejects_sheet_mode_with_file_replace(auto_data, outdir):
    """实测 Stata：sheet(...,replace) may not be combined with option replace。

    工具在入口拦下并说明二选一，比让用户读 Stata 那句 invalid syntax 更可行动。
    """
    result = auto_data.stata_export_excel(
        str(outdir / "d.xlsx"), sheet_mode="replace", replace=True
    )
    assert getattr(result, "is_error", False)
    assert "不能同时" in result_text(result)


def test_export_excel_filters_and_cell_offset(auto_data, outdir):
    """if + in + cell 三者同时生效。

    范围要选真的含 foreign==1 的观测：auto 前 10 条全是国产车，
    `if foreign == 1 in 1/10` 会选中 0 条 —— 见下一条用例。
    """
    target = outdir / "sub.xlsx"
    result = auto_data.stata_export_excel(
        str(target),
        varlist="make price",
        condition="foreign == 1",
        in_range="50/70",
        cell="B2",
        replace=True,
    )
    assert not getattr(result, "is_error", False), result_text(result)
    assert target.is_file()


def test_export_excel_empty_selection_gets_readable_hint(auto_data, outdir):
    """auto 前 10 条全为国产车 → 0 条命中，Stata 却报「行数上限」。

    下界是 1，所以 0 条也算越界；原始诊断与真实原因毫无关系，必须翻译。
    """
    result = auto_data.stata_export_excel(
        str(outdir / "empty.xlsx"), condition="foreign == 1", in_range="1/10", replace=True
    )
    text = result_text(result)
    assert getattr(result, "is_error", False)
    assert "未匹配到任何观测" in text
    assert "前 n 条观测里" in text


def test_export_excel_firstrow_varlabels(auto_data, outdir):
    target = outdir / "lab.xlsx"
    result = auto_data.stata_export_excel(
        str(target), varlist="make price", firstrow="varlabels", replace=True
    )
    assert not getattr(result, "is_error", False), result_text(result)


def test_export_excel_options_escape_hatch(auto_data, outdir):
    """长尾官方选项走 options 自由文本，须真能被 Stata 接受。"""
    target = outdir / "na.xlsx"
    result = auto_data.stata_export_excel(
        str(target), varlist="make price rep78", replace=True, options='missing("NA")'
    )
    assert not getattr(result, "is_error", False), result_text(result)
