"""tool_modules.estimation 的单元测试。

断言重点是**生成的命令字符串**与**错误分支**。fake deps 的校验器恒返回 None
（放行），因此「必填缺失 / 非法值」类错误分支通过**打补丁的 deps** 触发：构造
返回错误文本的校验器、重新 register，断言返回值以 "错误" 开头且 run_stata_command
未被调用。工具自身的校验（mlogit 的 baseoutcome、qreg 的 quantile、mixed 的
random）不依赖 deps 校验器，直接用默认 deps 即可断言。
"""
import types

import pytest

from tool_modules.estimation import register

# ruff: noqa: N802  # _FakeMcp 等类名沿用既有测试模板


class _FakeMcp:
    def __init__(self):
        self.tools = []

    def tool(self, annotations=None, **kw):
        def deco(fn):
            self.tools.append(fn)
            return fn

        return deco


def _make_deps():
    calls = []

    def run_stata_command(cmd, timeout=60):
        calls.append((cmd, timeout))
        return cmd

    d = types.SimpleNamespace(
        ToolAnnotations=types.SimpleNamespace,
        ToolResult=str,
        run_stata_command=run_stata_command,
        make_error=lambda m: f"ERR:{m}",
        result_or_error=lambda err: err,
        validate_identifier=lambda v, label="变量名", required=False: None,
        validate_varlist=lambda v, label="varlist": None,
        validate_filter_expr=lambda v, label: None,
        validate_no_injection=lambda v, label="参数": None,
        filter_clause=lambda c, r: (
            " "
            + " ".join(
                x
                for x in (
                    f"if {c}" if c.strip() else "",
                    f"in {r}" if r.strip() else "",
                )
                if x
            )
        )
        if (c.strip() or r.strip())
        else "",
    )
    return d, calls


def _make_error_deps(validator_name, message):
    """构造一个指定校验器返回错误文本的 deps，用于测试错误分支。"""
    d, calls = _make_deps()

    def _bad(v, label="变量名", required=False):
        return message

    setattr(d, validator_name, _bad)
    return d, calls


def _register(mcp, deps):
    register(mcp, deps)
    return mcp


def _find_tool(mcp, name):
    return next(t for t in mcp.tools if t.__name__ == name)


def _call_env(mcp, name, **kw):
    fn = _find_tool(mcp, name)
    return fn(**kw)


@pytest.fixture
def env():
    mcp = _FakeMcp()
    deps, calls = _make_deps()
    register(mcp, deps)
    return mcp, deps, calls


# ============================================================================
# stata_logit
# ============================================================================


def test_logit_full_command_with_filter_and_options(env):
    mcp, deps, calls = env
    result = _call_env(
        mcp,
        "stata_logit",
        depvar="foreign",
        indepvars="mpg weight",
        options="robust",
        condition="mpg > 20",
        in_range="1/100",
    )
    assert result == "logit foreign mpg weight if mpg > 20 in 1/100, robust"
    assert calls == [("logit foreign mpg weight if mpg > 20 in 1/100, robust", 60)]


def test_logit_default_command(env):
    mcp, deps, calls = env
    result = _call_env(mcp, "stata_logit", depvar="foreign", indepvars="mpg weight")
    assert result == "logit foreign mpg weight"
    assert calls == [("logit foreign mpg weight", 60)]


def test_logit_options_or_appended(env):
    mcp, deps, calls = env
    result = _call_env(
        mcp,
        "stata_logit",
        depvar="foreign",
        indepvars="mpg",
        options="or",
    )
    assert result == "logit foreign mpg, or"


def test_logit_missing_depvar_reports_error():
    d, calls = _make_error_deps(
        "validate_identifier", "错误: depvar 不能为空（logit 必须指定因变量）"
    )
    mcp = _register(_FakeMcp(), d)
    result = _call_env(mcp, "stata_logit", depvar="", indepvars="mpg")
    assert result.startswith("错误")
    assert calls == []


def test_logit_invalid_varlist_reports_error():
    d, calls = _make_error_deps("validate_varlist", "错误: indepvars 包含非法字符")
    mcp = _register(_FakeMcp(), d)
    result = _call_env(mcp, "stata_logit", depvar="foreign", indepvars="mpg; drop")
    assert result.startswith("错误")
    assert calls == []


def test_logit_invalid_options_reports_error():
    d, calls = _make_error_deps("validate_no_injection", "错误: options 包含非法字符")
    mcp = _register(_FakeMcp(), d)
    result = _call_env(mcp, "stata_logit", depvar="foreign", indepvars="mpg", options="robust\nx")
    assert result.startswith("错误")
    assert calls == []


def test_logit_invalid_condition_reports_error():
    d, calls = _make_error_deps("validate_filter_expr", "错误: condition 包含非法字符")
    mcp = _register(_FakeMcp(), d)
    result = _call_env(mcp, "stata_logit", depvar="foreign", indepvars="mpg", condition="1 using x //")
    assert result.startswith("错误")
    assert calls == []


# ============================================================================
# stata_mlogit
# ============================================================================


def test_mlogit_default_command(env):
    mcp, deps, calls = env
    result = _call_env(mcp, "stata_mlogit", depvar="rep78", indepvars="price mpg")
    assert result == "mlogit rep78 price mpg"
    assert calls == [("mlogit rep78 price mpg", 60)]


def test_mlogit_with_baseoutcome_and_options(env):
    mcp, deps, calls = env
    result = _call_env(
        mcp,
        "stata_mlogit",
        depvar="rep78",
        indepvars="price mpg",
        baseoutcome="2",
        options="robust",
    )
    # baseoutcome 拼在 options 之前
    assert result == "mlogit rep78 price mpg, baseoutcome(2) robust"
    assert calls == [("mlogit rep78 price mpg, baseoutcome(2) robust", 60)]


def test_mlogit_baseoutcome_only(env):
    mcp, deps, calls = env
    result = _call_env(
        mcp,
        "stata_mlogit",
        depvar="rep78",
        indepvars="price mpg",
        baseoutcome="3",
    )
    assert result == "mlogit rep78 price mpg, baseoutcome(3)"


def test_mlogit_baseoutcome_zero_accepted(env):
    """0/1/2 编码常见 —— baseoutcome 必须接受 0。"""
    mcp, deps, calls = env
    result = _call_env(mcp, "stata_mlogit", depvar="y", indepvars="x1", baseoutcome="0")
    assert result == "mlogit y x1, baseoutcome(0)"
    assert calls == [("mlogit y x1, baseoutcome(0)", 60)]


def test_mlogit_with_filter_before_baseoutcome(env):
    mcp, deps, calls = env
    result = _call_env(
        mcp,
        "stata_mlogit",
        depvar="rep78",
        indepvars="price mpg",
        baseoutcome="2",
        condition="price > 4000",
        in_range="1/50",
    )
    # if/in 拼在逗号之前，baseoutcome 在逗号之后
    assert result == "mlogit rep78 price mpg if price > 4000 in 1/50, baseoutcome(2)"


@pytest.mark.parametrize(
    "bad",
    ["01", "abc", "2.5", "-1", "2a"],
)
def test_mlogit_invalid_baseoutcome_reports_error(env, bad):
    mcp, deps, calls = env
    result = _call_env(
        mcp,
        "stata_mlogit",
        depvar="rep78",
        indepvars="price",
        baseoutcome=bad,
    )
    assert result.startswith("错误")
    assert "baseoutcome" in result
    assert calls == []


def test_mlogit_missing_depvar_reports_error():
    d, calls = _make_error_deps(
        "validate_identifier", "错误: depvar 不能为空（mlogit 必须指定因变量）"
    )
    mcp = _register(_FakeMcp(), d)
    result = _call_env(mcp, "stata_mlogit", depvar="", indepvars="price")
    assert result.startswith("错误")
    assert calls == []


# ============================================================================
# stata_nbreg
# ============================================================================


def test_nbreg_full_command_with_exposure(env):
    mcp, deps, calls = env
    result = _call_env(
        mcp,
        "stata_nbreg",
        depvar="deaths",
        indepvars="age smokes",
        options="exposure(pop)",
        condition="year >= 2000",
    )
    assert result == "nbreg deaths age smokes if year >= 2000, exposure(pop)"
    assert calls == [("nbreg deaths age smokes if year >= 2000, exposure(pop)", 60)]


def test_nbreg_default_command(env):
    mcp, deps, calls = env
    result = _call_env(mcp, "stata_nbreg", depvar="deaths", indepvars="age")
    assert result == "nbreg deaths age"
    assert calls == [("nbreg deaths age", 60)]


def test_nbreg_missing_depvar_reports_error():
    d, calls = _make_error_deps(
        "validate_identifier", "错误: depvar 不能为空（nbreg 必须指定因变量）"
    )
    mcp = _register(_FakeMcp(), d)
    result = _call_env(mcp, "stata_nbreg", depvar="", indepvars="age")
    assert result.startswith("错误")
    assert calls == []


# ============================================================================
# stata_qreg
# ============================================================================


def test_qreg_default_quantile_omitted(env):
    mcp, deps, calls = env
    result = _call_env(mcp, "stata_qreg", depvar="price", indepvars="mpg weight")
    # 默认 0.5 与 qreg 官方默认一致，不拼 quantile()
    assert result == "qreg price mpg weight"
    assert calls == [("qreg price mpg weight", 60)]


def test_qreg_explicit_default_quantile_omitted(env):
    mcp, deps, calls = env
    result = _call_env(
        mcp, "stata_qreg", depvar="price", indepvars="mpg", quantile=0.5
    )
    assert result == "qreg price mpg"


def test_qreg_quantile_appended(env):
    mcp, deps, calls = env
    result = _call_env(
        mcp, "stata_qreg", depvar="price", indepvars="mpg", quantile=0.9
    )
    assert result == "qreg price mpg, quantile(0.9)"
    assert calls == [("qreg price mpg, quantile(0.9)", 60)]


def test_qreg_quantile_with_filter_and_options(env):
    mcp, deps, calls = env
    result = _call_env(
        mcp,
        "stata_qreg",
        depvar="price",
        indepvars="mpg",
        quantile=0.25,
        options="vce(bootstrap)",
        condition="foreign == 1",
        in_range="1/100",
    )
    assert result == "qreg price mpg if foreign == 1 in 1/100, quantile(0.25) vce(bootstrap)"
    assert calls == [
        ("qreg price mpg if foreign == 1 in 1/100, quantile(0.25) vce(bootstrap)", 60)
    ]


@pytest.mark.parametrize("bad", [0, 1, -0.5, 1.5, 2])
def test_qreg_invalid_quantile_reports_error(env, bad):
    mcp, deps, calls = env
    result = _call_env(
        mcp, "stata_qreg", depvar="price", indepvars="mpg", quantile=bad
    )
    assert result.startswith("错误")
    assert "quantile" in result
    assert calls == []


def test_qreg_non_numeric_quantile_reports_error(env):
    mcp, deps, calls = env
    result = _call_env(
        mcp, "stata_qreg", depvar="price", indepvars="mpg", quantile="median"
    )
    assert result.startswith("错误")
    assert calls == []


def test_qreg_missing_depvar_reports_error():
    d, calls = _make_error_deps(
        "validate_identifier", "错误: depvar 不能为空（qreg 必须指定因变量）"
    )
    mcp = _register(_FakeMcp(), d)
    result = _call_env(mcp, "stata_qreg", depvar="", indepvars="mpg")
    assert result.startswith("错误")
    assert calls == []


# ============================================================================
# stata_mixed
# ============================================================================


def test_mixed_with_random_intercept(env):
    mcp, deps, calls = env
    result = _call_env(
        mcp,
        "stata_mixed",
        depvar="y",
        indepvars="x1 x2",
        random="|| id:",
    )
    assert result == "mixed y x1 x2 || id:"
    assert calls == [("mixed y x1 x2 || id:", 60)]


def test_mixed_nested_random_with_options(env):
    mcp, deps, calls = env
    result = _call_env(
        mcp,
        "stata_mixed",
        depvar="y",
        indepvars="x1",
        random="|| id: || time:",
        options="vce(robust)",
    )
    assert result == "mixed y x1 || id: || time:, vce(robust)"
    assert calls == [("mixed y x1 || id: || time:, vce(robust)", 60)]


def test_mixed_without_random(env):
    mcp, deps, calls = env
    result = _call_env(mcp, "stata_mixed", depvar="y", indepvars="x1")
    assert result == "mixed y x1"
    assert calls == [("mixed y x1", 60)]


def test_mixed_with_filter_between_random_and_options(env):
    mcp, deps, calls = env
    result = _call_env(
        mcp,
        "stata_mixed",
        depvar="y",
        indepvars="x1",
        random="|| id:",
        condition="!missing(x2)",
        options="ml",
    )
    # [if] 属于固定效应方程，在 || 随机效应之前（官方语法）
    assert result == "mixed y x1 if !missing(x2) || id:, ml"


def test_mixed_random_must_start_with_pipes(env):
    mcp, deps, calls = env
    result = _call_env(
        mcp,
        "stata_mixed",
        depvar="y",
        indepvars="x1",
        random="id:",
    )
    assert result.startswith("错误")
    assert "||" in result
    assert calls == []


def test_mixed_injected_random_reports_error():
    d, calls = _make_error_deps("validate_no_injection", "错误: random 包含非法字符")
    mcp = _register(_FakeMcp(), d)
    result = _call_env(
        mcp, "stata_mixed", depvar="y", indepvars="x1", random="|| id:\n!shell"
    )
    assert result.startswith("错误")
    assert calls == []


def test_mixed_missing_depvar_reports_error():
    d, calls = _make_error_deps(
        "validate_identifier", "错误: depvar 不能为空（mixed 必须指定因变量）"
    )
    mcp = _register(_FakeMcp(), d)
    result = _call_env(mcp, "stata_mixed", depvar="", indepvars="x1", random="|| id:")
    assert result.startswith("错误")
    assert calls == []


# ============================================================================
# 注册完整性
# ============================================================================


def test_all_tools_registered(env):
    mcp, deps, calls = env
    names = {t.__name__ for t in mcp.tools}
    assert names == {
        "stata_logit",
        "stata_mlogit",
        "stata_nbreg",
        "stata_qreg",
        "stata_mixed",
    }
