"""会话状态感知、导入命令族、面板声明 —— 真实 Stata 端到端验证。

运行：``.venv/bin/python -m pytest tests_e2e/ -q``
"""

import pytest

from tests_e2e.conftest import SKIP_REASON, STATA_AVAILABLE, result_text

pytestmark = [
    pytest.mark.stata,
    pytest.mark.skipif(not STATA_AVAILABLE, reason=SKIP_REASON),
]


def _ok(result, label=""):
    assert not getattr(result, "is_error", False), f"{label}{result_text(result)}"
    return result_text(result)


# --- 会话状态感知 -------------------------------------------------------------


def test_status_surfaces_panel_declaration(stata):
    """Agent 调 stata_xtreg 前要能确认是否已 xtset。"""
    stata.stata_run("webuse nlswork, clear")
    stata.stata_run("xtset, clear")
    unset = _ok(stata.stata_status())
    assert "panel variable not set" in unset, unset[:400]

    _ok(stata.stata_xtset(panelvar="idcode", timevar="year"))
    setted = _ok(stata.stata_status())
    assert "Panel variable: idcode" in setted, setted[:400]


def test_status_surfaces_frames_and_estimates(stata):
    stata.stata_run("sysuse auto, clear")
    stata.stata_run("capture frame drop probe")
    stata.stata_run("frame create probe")
    stata.stata_run("regress price weight")
    stata.stata_run("estimates store mprobe")
    try:
        out = _ok(stata.stata_status())
        assert "当前 frame: default" in out, out[:400]
        assert "probe" in out, "其余 frame 也要报告"
        assert "当前活跃: regress" in out, out[:400]
        assert "mprobe" in out, "已存估计要报告"
    finally:
        stata.stata_run("capture frame drop probe")
        stata.stata_run("capture estimates drop mprobe")


def test_status_does_not_change_working_directory(stata, outdir):
    """readOnlyHint=True 的工具不能有副作用 —— 裸 cd 会切到 home。"""
    _ok(stata.stata_set_cwd(str(outdir)))
    before = result_text(stata.stata_run("display c(pwd)")).strip()
    _ok(stata.stata_status())
    after = result_text(stata.stata_run("display c(pwd)")).strip()
    assert after == before, f"工作目录被改动：{before!r} → {after!r}"


# --- 面板 / 时序声明 ----------------------------------------------------------


def test_xtset_declare_show_clear_roundtrip(stata):
    stata.stata_run("webuse nlswork, clear")
    out = _ok(stata.stata_xtset(panelvar="idcode", timevar="year"))
    assert "Panel variable: idcode" in out

    shown = _ok(stata.stata_xtset(action="show"))
    assert "Panel variable: idcode" in shown

    _ok(stata.stata_xtset(action="clear"))
    after = _ok(stata.stata_xtset(action="show"))
    assert "not set" in after, after


def test_xtset_enables_xtreg(stata):
    """未声明面板时 xtreg 报 r(459)；声明后应能跑通。"""
    stata.stata_run("webuse nlswork, clear")
    stata.stata_run("xtset, clear")
    failed = stata.stata_xtreg("ln_wage", "age")
    assert getattr(failed, "is_error", False), "未 xtset 时 xtreg 应失败"

    _ok(stata.stata_xtset(panelvar="idcode", timevar="year"))
    _ok(stata.stata_xtreg("ln_wage", "age"), "xtset 之后: ")


def test_tsset_for_pure_time_series(stata):
    stata.stata_run("sysuse auto, clear")
    stata.stata_run("capture drop tvar")
    stata.stata_run("generate tvar = _n")
    out = _ok(stata.stata_xtset(timevar="tvar"))
    assert "Time variable: tvar" in out, out


# --- 导入命令族 --------------------------------------------------------------


def test_import_excel_roundtrip(stata, outdir):
    """export → import 往返：变量名与观测数都要还原。"""
    src = outdir / "rt.xlsx"
    stata.stata_run("sysuse auto, clear")
    _ok(stata.stata_export_excel(str(src), varlist="make price mpg", replace=True))

    _ok(stata.stata_import(str(src), firstrow=True))
    desc = _ok(stata.stata_describe())
    for var in ("make", "price", "mpg"):
        assert var in desc, desc[:300]
    assert "74" in desc


def test_import_delimited_roundtrip_with_options(stata, outdir):
    src = outdir / "rt.csv"
    stata.stata_run("sysuse auto, clear")
    _ok(stata.stata_export_delimited(str(src), varlist="make price", replace=True))

    _ok(stata.stata_import(str(src), delimiter=",", case="lower"))
    desc = _ok(stata.stata_describe())
    assert "make" in desc and "price" in desc


def test_import_drops_inapplicable_option_instead_of_failing(stata, outdir):
    """firstrow 只属 excel；对 csv 传了会 r(198)，须丢弃并说明。"""
    src = outdir / "opt.csv"
    stata.stata_run("sysuse auto, clear")
    _ok(stata.stata_export_delimited(str(src), varlist="make price", replace=True))

    out = _ok(stata.stata_import(str(src), firstrow=True))
    assert "firstrow" in out and "不支持" in out, out[:300]


def test_import_dta_redirects_to_use_dataset(stata, outdir):
    dta = outdir / "x.dta"
    stata.stata_run("sysuse auto, clear")
    _ok(stata.stata_save_dataset(str(dta), replace=True))

    result = stata.stata_import(str(dta))
    assert getattr(result, "is_error", False)
    assert "stata_use_dataset" in result_text(result)


def test_import_missing_file_is_reported(stata, outdir):
    result = stata.stata_import(str(outdir / "nope.csv"))
    assert getattr(result, "is_error", False)
