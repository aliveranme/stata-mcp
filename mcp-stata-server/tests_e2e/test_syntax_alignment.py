"""工具参数与官方语法位置的对齐 —— 真实 Stata 端到端验证。

每个包装了具体 Stata 命令的工具，都应能表达该命令官方语法里的各个位置
（``[varlist] [if] [in] [, options]``）与存储类型。这里验证补齐的参数**真的
被 Stata 接受并生效**，而不只是拼进了命令串。

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


# --- [in] 观测范围 ------------------------------------------------------------


def _nobs(output: str) -> int:
    """从估计输出里取 `Number of obs`。

    不能用 `assert "40" in output` 这类宽松断言 —— 回归输出里到处是数字，
    即使 in 子句被整个丢掉也照样通过（变异测试实证）。
    """
    m = re.search(r"Number of obs\s*=\s*([\d,]+)", output)
    assert m, f"未找到 Number of obs：{output[:200]}"
    return int(m.group(1).replace(",", ""))


def test_in_range_actually_restricts_estimation_sample(auto_data):
    """in_range 不能只是拼进命令 —— 样本量必须真的变小。"""
    full = _ok(auto_data.stata_regress("price", "weight"))
    sub = _ok(auto_data.stata_regress("price", "weight", in_range="1/40"))
    assert _nobs(full) == 74
    assert _nobs(sub) == 40


def test_if_and_in_combine_on_estimation(auto_data):
    out = _ok(auto_data.stata_regress("price", "weight", condition="foreign == 0", in_range="1/40"))
    assert "Number of obs" in out


def test_in_range_restricts_exploration_tools(auto_data):
    """summarize/codebook/tabulate/correlate 都接受 [in]。"""
    out = _ok(auto_data.stata_summarize("price", in_range="1/40"))
    # summarize 输出的第一列就是 Obs；用 40 与全样本 74 区分，避免宽松匹配
    assert re.search(r"price\s*\|\s*40\b", out), out
    _ok(auto_data.stata_codebook("price", in_range="1/40"), "codebook: ")
    _ok(auto_data.stata_tabulate("rep78", in_range="1/40"), "tabulate: ")
    _ok(auto_data.stata_correlate("price mpg", in_range="1/40"), "correlate: ")


def test_in_range_restricts_variable_creation(auto_data):
    """generate 只对范围内观测赋值，其余为缺失。"""
    _ok(auto_data.stata_generate("flag", "1", in_range="1/40"))
    out = _ok(auto_data.stata_summarize("flag"))
    assert re.search(r"flag\s*\|\s*40\b", out), out


def test_margins_accepts_if_and_in(auto_data):
    auto_data.stata_run("regress price weight mpg")
    _ok(auto_data.stata_margins(dydx="weight", condition="foreign == 0"), "margins if: ")
    _ok(auto_data.stata_margins(dydx="weight", in_range="1/40"), "margins in: ")


def test_predict_and_ttest_accept_in_range(auto_data):
    auto_data.stata_run("regress price weight")
    _ok(auto_data.stata_predict("yhat", in_range="1/40"), "predict: ")
    _ok(auto_data.stata_ttest("price", compare_to="5000", in_range="1/40"), "ttest: ")


def test_ttest_all_four_official_forms(auto_data):
    """裸 `ttest price` 报 by() option required；四种合法形式都要能表达。"""
    _ok(auto_data.stata_ttest("price", compare_to="5000"), "单样本: ")
    _ok(auto_data.stata_ttest("price", byvar="foreign"), "按组: ")
    _ok(auto_data.stata_ttest("price", compare_to="mpg"), "配对: ")
    _ok(auto_data.stata_ttest("price", compare_to="mpg", options="unpaired"), "非配对: ")


def test_ttest_bare_form_is_refused_before_reaching_stata(auto_data):
    result = auto_data.stata_ttest("price")
    text = result_text(result)
    assert getattr(result, "is_error", False)
    assert "compare_to" in text
    assert "r(100)" not in text, "应在入口拦下，而不是把非法命令发给 Stata"


# --- 条件加载 ----------------------------------------------------------------


def test_use_dataset_loads_only_requested_subset(auto_data, stata):
    """`use [varlist] using file if ... in ...` 可只载入子集，省内存。"""
    dta = "/Volumes/ccc/Applications/StataNow/auto.dta"
    import os

    if not os.path.isfile(dta):
        pytest.skip("找不到随 Stata 分发的 auto.dta")

    _ok(stata.stata_use_dataset(dta, varlist="make price foreign", condition="foreign == 1"))
    desc = _ok(stata.stata_describe())
    assert "22" in desc, desc  # foreign==1 共 22 条
    assert "mpg" not in desc, "未请求的变量不该被载入"


# --- options 逃生舱 -----------------------------------------------------------


def test_options_escape_hatches_are_accepted_by_stata(auto_data):
    """长尾官方选项必须真能被 Stata 接受，而不只是拼进命令串。"""
    _ok(auto_data.stata_list("price", n=3, options="noobs clean"), "list: ")
    _ok(auto_data.stata_tabulate("rep78", options="missing"), "tabulate: ")
    _ok(auto_data.stata_summarize("price", options="separator(0)"), "summarize: ")
    _ok(auto_data.stata_codebook("price", options="compact"), "codebook: ")
    _ok(auto_data.stata_describe("price mpg", options="fullnames"), "describe: ")


def test_describe_simple_keeps_varlist(auto_data):
    """旧实现在 simple=True 时丢弃 varlist，用户拿到的是全部变量。"""
    out = _ok(auto_data.stata_describe("price mpg", simple=True))
    assert "price" in out and "mpg" in out
    assert "headroom" not in out, "simple 不该把 varlist 丢掉"


def test_test_tool_options(auto_data):
    auto_data.stata_run("regress price weight mpg")
    out = _ok(auto_data.stata_test("weight mpg", options="mtest"))
    assert "weight" in out


def test_save_and_use_options(auto_data, outdir):
    target = outdir / "t.dta"
    _ok(auto_data.stata_save_dataset(str(target), replace=True, options="orphans"))
    _ok(auto_data.stata_use_dataset(str(target), options="nolabel"))


# --- 存储类型 ----------------------------------------------------------------


def test_storage_type_is_applied(auto_data):
    """generate/egen 的 [type] 位置：默认 float 会损失精度。"""
    _ok(auto_data.stata_generate("dp", "price/3", vartype="double"))
    _ok(auto_data.stata_egen("dm", "mean(price)", vartype="double"))
    desc = _ok(auto_data.stata_describe("dp dm"))
    assert desc.count("double") >= 2, desc


def test_generate_options_position_variable(auto_data):
    """generate 的 before()/after() 控制新变量在数据集中的位置。"""
    _ok(auto_data.stata_generate("first", "1", options="before(make)"))
    out = _ok(auto_data.stata_describe(simple=True))
    assert out.strip().split()[0] == "first", out[:120]
