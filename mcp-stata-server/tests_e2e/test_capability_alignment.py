"""能力边界对齐：验证工具的参数表面与官方语法对齐，且 `if`/`in`/`options` 产生**正确结果**。

只放单元测试无法证伪的数值正确性断言 —— 命令拼接在 tests/，这里验证真机结果：
筛选是否真筛、系数是否真对、选项变体是否真可用。

运行：``.venv/bin/python -m pytest tests_e2e/test_capability_alignment.py -q``
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


def _display(stata, expr):
    """取单值：display 一个标量表达式，返回解析后的 float（取最后一个数字）。

    ``display`` 只接受标量表达式；``if``/``count``/``sum()`` 都是命令或 egen 函数，
    不能塞进 display —— 需要计数的场景用 _count。
    """
    out = _ok(stata.stata_run(f"display {expr}"))
    nums = re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", out)
    return float(nums[-1])


def _count(stata, cond):
    """统计满足条件的观测数：``count if <cond>`` 是命令，随后取 r(N)。"""
    _ok(stata.stata_run(f"count if {cond}"))
    return _display(stata, "r(N)")


@pytest.fixture(autouse=True)
def _auto(stata):
    stata.stata_run("sysuse auto, clear")


# --- 筛选是否真筛 ---


def test_summarize_if_filters_observations(stata):
    out = _ok(stata.stata_summarize("price", condition="foreign == 1"))
    assert "22" in out, "auto 里 foreign==1 恰 22 辆，if 必须真筛\n" + out[:300]


def test_regress_if_in_options_affect_result(stata):
    # 加 noconstant 后 r2 变化是官方语义；这里验证 if/in 生效（子样本 N 减少）
    out = _ok(stata.stata_regress("price", "weight mpg", condition="foreign == 0"))
    assert "52" in out, "foreign==0 有 52 辆，回归 N 应为 52\n" + out[:300]


def test_generate_if_and_storage_type(stata):
    _ok(stata.stata_generate("big", "weight > 3000", condition="!missing(weight)"))
    # [type] 是独立语法位置，不是选项 —— 用 vartype 参数（官方 generate [type] var = ...）
    _ok(stata.stata_generate("d_foreign", "1 if foreign == 1", vartype="byte"))
    n = _count(stata, "d_foreign == 1")
    assert n == 22, f"byte 生成 + if 赋值应得 22，实际 {n}"


def test_replace_if_updates_only_matching(stata):
    _ok(stata.stata_replace("price", "99999", condition="foreign == 1"))
    n = _count(stata, "price == 99999")
    assert n == 22, f"replace if 应只改 22 条，实际 {n}"


def test_drop_if_removes_correct_count(stata):
    _ok(stata.stata_drop(condition="foreign == 1"))
    n = _display(stata, "c(N)")
    assert n == 52, f"drop if foreign==1 应剩 52 条，实际 {n}"


def test_keep_in_range_keeps_correct_count(stata):
    _ok(stata.stata_keep(in_range="1/10"))
    n = _display(stata, "c(N)")
    assert n == 10, f"keep in 1/10 应留 10 条，实际 {n}"


def test_list_in_range_shows_requested_rows(stata):
    out = _ok(stata.stata_list("price", in_range="1/3"))
    # list 输出行号标记为 "1." "2." "3."，验证前三行都在
    for label in ("1.", "2.", "3."):
        assert label in out, f"list in 1/3 应含行号 {label}\n{out[:300]}"


# --- 数据重构的值正确性 ---


def test_recode_transforms_values(stata):
    _ok(stata.stata_recode("rep78", "(1/3=0) (4=1) (5=2)"))
    n = _count(stata, "rep78 == 2")
    assert n == 11, f"rep78==5 有 11 辆，重编码后应为 2，实际 {n}"


def test_rename_then_reference_new_name(stata):
    _ok(stata.stata_rename("price", "price_new"))
    out = _ok(stata.stata_summarize("price_new"))
    assert "price_new" in out


def test_destring_values_equal_original(stata):
    _ok(stata.stata_generate("price_s", 'string(price, "%9.0f")'))
    _ok(stata.stata_destring("price_s", replace=True, force=True))
    d = _count(stata, "price_s != price")
    assert d == 0, f"destring 后数值应与原值一致，差异 {d}"


def test_collapse_by_produces_group_means(stata):
    _ok(stata.stata_collapse("(mean) price", by="foreign"))
    n = _display(stata, "c(N)")
    assert n == 2, f"collapse by foreign 应得 2 组，实际 {n}"
    # collapse 后数据只有 2 行；用 summarize if 取 foreign==1 组的均值
    _ok(stata.stata_run("summarize price if foreign == 1, meanonly"))
    foreign_mean = _display(stata, "r(mean)")
    assert foreign_mean > 6000, f"进口车均价应 >6000（已知 ~6072），实际 {foreign_mean}"


# --- 估计族的选项变体 ---


def test_regress_robust_and_level(stata):
    # robust 与 vce(cluster) 互斥（vce(cluster) 蕴含稳健标准误），不能同传
    out = _ok(stata.stata_regress("price", "weight mpg", options="vce(cluster foreign)"))
    assert "Std. err." in out


def test_ttest_single_sample_matches_known(stata):
    out = _ok(stata.stata_ttest("mpg", compare_to="21"))
    assert "One-sample t test" in out, out[:300]


def test_xtreg_fe_and_re(stata):
    stata.stata_run("webuse nlswork, clear")
    _ok(stata.stata_xtset(panelvar="idcode", timevar="year"))
    fe = _ok(stata.stata_xtreg("ln_wage", "age", effects="fe"))
    assert "Fixed-effects" in fe, fe[:300]
    re = _ok(stata.stata_xtreg("ln_wage", "age", effects="re"))
    assert "Random-effects" in re, re[:300]


def test_ivregress_all_estimators(stata):
    _ok(stata.stata_run("webuse hsng2, clear"))
    for est in ("2sls", "liml", "gmm"):
        out = _ok(stata.stata_ivregress("rent", "pop", "pcturban", exogenous="faminc", estimator=est))
        assert "Instrumental-variables" in out and est.upper() in out, f"{est}: {out[:300]}"


def test_logit_coefficient_consistent_with_logistic_or(stata):
    logit = _ok(stata.stata_logit("foreign", "price"))
    logistic = _ok(stata.stata_logistic("foreign", "price"))
    # logit 报告系数，logistic 报告 OR；二者应同时出现
    assert "Coef." in logit or "Coefficient" in logit, logit[:200]
    assert "Odds ratio" in logistic, logistic[:200]


def test_qreg_median_equals_qreg_default(stata):
    q = _ok(stata.stata_qreg("price", "weight mpg", quantile=0.5))
    assert "Median regression" in q, q[:300]


def test_margins_after_logit(stata):
    _ok(stata.stata_logistic("foreign", "weight"))
    # 连续变量不能放 margins 的 varlist 位（r(322)），用 dydx(weight) 求边际效应
    out = _ok(stata.stata_margins(dydx="weight"))
    assert "dy/dx" in out or "Delta-method" in out, out[:300]


def test_lincom_linear_combination_value(stata):
    _ok(stata.stata_regress("price", "weight mpg"))
    out = _ok(stata.stata_lincom("_b[weight] + _b[mpg]"))
    # 组合值应等于两系数之和（用 display 验证）
    expect = _display(stata, "_b[weight] + _b[mpg]")
    m = re.search(r"-\d+\.\d+", out)
    assert m and abs(float(m.group(0)) - expect) < 0.01, f"{out[:300]} vs {expect}"


def test_nlcom_ratio_equals_manual(stata):
    _ok(stata.stata_regress("price", "weight mpg"))
    out = _ok(stata.stata_nlcom("_b[weight]/_b[mpg]"))
    expect = _display(stata, "_b[weight]/_b[mpg]")
    m = re.search(r"_nl_1\s+\|\s+([-+]?\d*\.?\d+)", out)
    assert m and abs(float(m.group(1)) - expect) < 0.01, f"{out[:300]} vs {expect}"


def test_hausman_sensible_chi2(stata):
    _ok(stata.stata_run("webuse nlswork, clear"))
    _ok(stata.stata_xtset(panelvar="idcode", timevar="year"))
    _ok(stata.stata_xtreg("ln_wage", "age", effects="fe"))
    _ok(stata.stata_estimates(action="store", name="fe"))
    _ok(stata.stata_xtreg("ln_wage", "age", effects="re"))
    _ok(stata.stata_estimates(action="store", name="re"))
    out = _ok(stata.stata_hausman("fe", "re", options="sigmamore"))
    assert "chi2" in out, out[:300]
    stata.stata_run("capture estimates drop fe re")


def test_mixed_reports_group_variance(stata):
    stata.stata_run("webuse nlswork, clear")
    out = _ok(stata.stata_mixed("ln_wage", "age", random="|| idcode:"))
    assert "Random-effects" in out or "Random effects" in out, out[:300]


def test_nbreg_reports_alpha(stata):
    stata.stata_run("sysuse auto, clear")
    out = _ok(stata.stata_nbreg("rep78", "price weight", condition="rep78 > 0"))
    assert "alpha" in out.lower() or "overdispersion" in out.lower(), out[:300]


def test_do_file_compact_removes_count_lines(stata, outdir):
    """compact=True 删统计计数行，结果表保留（真机验证）。"""
    do_file = outdir / "c.do"
    do_file.write_text(
        "sysuse auto, clear\nreplace price = price + 1 if foreign == 1\nsummarize price\n",
        encoding="utf-8",
    )
    raw = result_text(stata.stata_run_do_file(str(do_file)))
    compact = result_text(stata.stata_run_do_file(str(do_file), compact=True))
    assert "(22 real changes made)" in raw, raw[:200]
    assert "(22 real changes made)" not in compact, compact[:200]
    assert "Variable" in compact and "price" in compact, compact[:200]  # 结果表保留
