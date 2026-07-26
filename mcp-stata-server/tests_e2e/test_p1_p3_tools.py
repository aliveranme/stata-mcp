"""P1–P3 新增工具与包搜索 —— 真实 Stata 端到端验证。

运行：``.venv/bin/python -m pytest tests_e2e/ -q``
"""

import re

import pytest

from tests_e2e.conftest import SKIP_REASON, STATA_AVAILABLE, result_text

pytestmark = [
    pytest.mark.stata,
    pytest.mark.skipif(not STATA_AVAILABLE, reason=SKIP_REASON),
]


def _ok(result, label=""):
    assert not getattr(result, "is_error", False), f"{label}{result_text(result)}"
    return result_text(result)


# --- P1: estat / estimates / 示例数据 ----------------------------------------


def test_use_example_sysuse_and_list(stata):
    out = _ok(stata.stata_use_example("auto"))
    assert "1978" in out or "automobile" in out.lower(), out[:200]
    listing = _ok(stata.stata_use_example(action="list"))
    assert "auto" in listing, listing[:200]


def test_use_example_webuse_needs_network(stata):
    out = _ok(stata.stata_use_example("nlswork", source="webuse"))
    assert "National Longitudinal" in out, out[:200]


def test_estat_diagnostics_on_real_model(stata):
    stata.stata_run("sysuse auto, clear")
    stata.stata_run("regress price weight mpg")
    vif = _ok(stata.stata_estat("vif"), "vif: ")
    assert "VIF" in vif, vif[:200]
    het = _ok(stata.stata_estat("hettest"), "hettest: ")
    assert "chi2" in het.lower(), het[:200]
    ic = _ok(stata.stata_estat("ic"), "ic: ")
    assert "AIC" in ic, ic[:200]


def test_estat_options_are_passed(stata):
    stata.stata_run("sysuse auto, clear")
    stata.stata_run("regress price weight mpg")
    _ok(stata.stata_estat("hettest", options="rhs iid"), "hettest rhs: ")


def test_estat_without_estimates_reports_error(stata):
    stata.stata_run("sysuse auto, clear")
    stata.stata_run("ereturn clear")
    result = stata.stata_estat("vif")
    assert getattr(result, "is_error", False), "无估计结果时 estat 应失败"


def test_estimates_store_table_roundtrip(stata):
    stata.stata_run("sysuse auto, clear")
    stata.stata_run("capture estimates clear")
    stata.stata_run("regress price weight")
    _ok(stata.stata_estimates(action="store", name="mw"))
    stata.stata_run("regress price mpg")
    _ok(stata.stata_estimates(action="store", name="mm"))

    listing = _ok(stata.stata_estimates(action="dir"))
    assert "mw" in listing and "mm" in listing, listing[:300]

    table = _ok(stata.stata_estimates(action="table", name="mw mm",
                                      options="stats(N r2)"))
    assert "mw" in table and "mm" in table, table[:300]

    _ok(stata.stata_estimates(action="restore", name="mw"))
    _ok(stata.stata_estimates(action="clear"))
    assert "mw" not in _ok(stata.stata_estimates(action="dir"))


# --- P2: 数据重构 -------------------------------------------------------------


def test_merge_and_verify_match_results(stata, outdir):
    """合并后用 _merge 检查匹配情况 —— 官方推荐的验证方式。"""
    using = outdir / "using.dta"
    stata.stata_run("sysuse auto, clear")
    stata.stata_run("keep make foreign")
    _ok(stata.stata_save_dataset(str(using), replace=True))

    stata.stata_run("sysuse auto, clear")
    stata.stata_run("drop foreign")
    out = _ok(stata.stata_merge(kind="1:1", keyvars="make", using=str(using)))
    assert "matched" in out.lower(), out[:300]

    tab = _ok(stata.stata_tabulate("_merge"))
    assert "74" in tab, tab[:200]


def test_merge_keepusing_limits_incoming_variables(stata, outdir):
    using = outdir / "u2.dta"
    stata.stata_run("sysuse auto, clear")
    _ok(stata.stata_save_dataset(str(using), replace=True))

    stata.stata_run("sysuse auto, clear")
    stata.stata_run("keep make price")
    _ok(stata.stata_merge(kind="1:1", keyvars="make", using=str(using),
                          keepusing="mpg", options="nogenerate"))
    desc = _ok(stata.stata_describe())
    assert "mpg" in desc
    assert "headroom" not in desc, "keepusing 应挡住其余变量"


def test_append_stacks_datasets(stata, outdir):
    part = outdir / "part.dta"
    stata.stata_run("sysuse auto, clear")
    stata.stata_run("keep in 1/10")
    _ok(stata.stata_save_dataset(str(part), replace=True))

    stata.stata_run("sysuse auto, clear")
    stata.stata_run("keep in 1/10")
    _ok(stata.stata_append(using=str(part), options="generate(src)"))
    count = _ok(stata.stata_verify(check="count"))
    assert "20" in count, count[:200]


def test_reshape_long_wide_roundtrip(stata):
    """宽 → 长 → 宽 应回到原形态。"""
    stata.stata_run("webuse reshape1, clear")
    long_out = _ok(stata.stata_reshape(direction="long", stub="inc", i="id", j="year"))
    assert "long" in long_out.lower(), long_out[:200]
    desc = _ok(stata.stata_describe())
    assert "year" in desc, "long 形态应有 j 变量"

    _ok(stata.stata_reshape(direction="wide", stub="inc", i="id", j="year"))
    back = _ok(stata.stata_describe())
    assert "inc80" in back, back[:300]


def test_collapse_aggregates_by_group(stata):
    stata.stata_run("sysuse auto, clear")
    _ok(stata.stata_collapse(clist="(mean) price (sd) mpg", by="foreign"))
    count = _ok(stata.stata_verify(check="count"))
    assert "2" in count, "按 foreign 聚合应只剩 2 行"
    desc = _ok(stata.stata_describe())
    assert "price" in desc and "mpg" in desc


def test_collapse_respects_filters(stata):
    stata.stata_run("sysuse auto, clear")
    _ok(stata.stata_collapse(clist="(mean) price", by="foreign",
                             condition="price < 10000"))
    assert "2" in _ok(stata.stata_verify(check="count"))


def test_return_list_exposes_r_e_and_c(stata):
    stata.stata_run("sysuse auto, clear")
    stata.stata_run("summarize price")
    r = _ok(stata.stata_return_list("r"))
    assert "r(mean)" in r, r[:300]

    stata.stata_run("regress price weight")
    e = _ok(stata.stata_return_list("e"))
    assert "e(r2)" in e or "e(N)" in e, e[:300]

    c = _ok(stata.stata_return_list("c"))
    assert "c(pwd)" in c or "pwd" in c, c[:300]


# --- P3: frames / 校验 --------------------------------------------------------


def test_frame_lifecycle(stata):
    stata.stata_run("capture frame drop fa")
    stata.stata_run("capture frame drop fb")
    try:
        _ok(stata.stata_frame(action="create", name="fa"))
        listing = _ok(stata.stata_frame(action="dir"))
        assert "fa" in listing, listing[:200]

        _ok(stata.stata_frame(action="rename", name="fa", newname="fb"))
        assert "fb" in _ok(stata.stata_frame(action="dir"))

        _ok(stata.stata_frame(action="change", name="fb"))
        assert "fb" in _ok(stata.stata_frame(action="current"))
    finally:
        stata.stata_run("frame change default")
        stata.stata_run("capture frame drop fa")
        stata.stata_run("capture frame drop fb")


def test_verify_checks_on_real_data(stata):
    stata.stata_run("sysuse auto, clear")
    assert "74" in _ok(stata.stata_verify(check="count"))
    assert "22" in _ok(stata.stata_verify(check="count", condition="foreign == 1"))

    _ok(stata.stata_verify(check="isid", varlist="make"), "isid make: ")
    dup = _ok(stata.stata_verify(check="duplicates", varlist="foreign"))
    assert "duplicates" in dup.lower(), dup[:200]

    miss = _ok(stata.stata_verify(check="missing", varlist="rep78"))
    assert "rep78" in miss, miss[:200]

    _ok(stata.stata_verify(check="assert", expression="price > 0"))


def test_verify_isid_fails_on_non_unique_key(stata):
    stata.stata_run("sysuse auto, clear")
    result = stata.stata_verify(check="isid", varlist="foreign")
    assert getattr(result, "is_error", False), "foreign 不唯一，isid 应失败"


def test_verify_assert_fails_when_claim_is_false(stata):
    stata.stata_run("sysuse auto, clear")
    result = stata.stata_verify(check="assert", expression="price > 1e9")
    assert getattr(result, "is_error", False)


# --- 包搜索 -------------------------------------------------------------------


def test_find_package_locates_known_packages(stata):
    out = _ok(stata.stata_find_package("binscatter"))
    assert "binscatter" in out, out[:300]
    assert "packages found" in out or "package" in out.lower()


def test_find_package_no_match_is_not_an_error(stata):
    """搜不到东西本身不是错误 —— 默认返回普通文本。"""
    result = stata.stata_find_package("zzz_no_such_package_xyz")
    assert not getattr(result, "is_error", False)
    assert "no matches" in result_text(result)


def test_find_package_error_if_none_is_opt_in(stata):
    result = stata.stata_find_package("zzz_no_such_package_xyz", error_if_none=True)
    assert getattr(result, "is_error", False), "errnone 应把无匹配转成 rc=111"


def test_find_package_scope_toc_shrinks_output(stata):
    """实测宽泛查询默认 94K 字符，scope="toc" 收窄到 12K 量级。"""
    def _chars(result):
        text = result_text(result)
        m = re.search(r"共 ([\d,]+) 字符", text)
        return int(m.group(1).replace(",", "")) if m else len(text)

    wide = _chars(_ok(stata.stata_find_package("difference in differences")))
    narrow = _chars(_ok(stata.stata_find_package("difference in differences",
                                                 scope="toc")))
    assert narrow < wide / 2, f"toc 应显著收窄：{wide} → {narrow}"


def test_find_to_describe_to_install_workflow(stata):
    """找包 → 查详情 → （已装则跳过安装）的完整链路。"""
    found = _ok(stata.stata_find_package("estout", scope="pkg"))
    assert "estout" in found

    installed = _ok(stata.stata_list_packages())
    if "estout" not in installed:
        pytest.skip("本机未装 estout，跳过 describe 环节")
    detail = _ok(stata.stata_describe_package("estout"))
    assert "estout" in detail
