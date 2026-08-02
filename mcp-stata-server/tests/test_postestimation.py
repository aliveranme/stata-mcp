import types

import pytest

from tool_modules.postestimation import register


class _FakeMcp:
    """记录注册的工具及其 annotations，供断言工具名与只读注解。"""

    def __init__(self):
        self.tools = []
        self.annotations = {}

    def tool(self, annotations=None, **kw):
        def deco(fn):
            self.tools.append(fn)
            self.annotations[fn.__name__] = annotations
            return fn

        return deco


def _make_deps(overrides=None):
    calls = []

    def run_stata_command(cmd, timeout=60):
        calls.append((cmd, timeout))
        return cmd

    d = types.SimpleNamespace(
        ToolAnnotations=types.SimpleNamespace,
        ToolResult=str,
        run_stata_command=run_stata_command,
        # make_error 原样返回消息（消息本身以 "错误: " 开头），与真实 deps 一致
        make_error=lambda m: m,
        result_or_error=lambda err: err,
        validate_identifier=lambda v, label="变量名", required=False: None,
        validate_varlist=lambda v, label="varlist": None,
        validate_filter_expr=lambda v, label: None,
        validate_no_injection=lambda v, label="参数": None,
        filter_clause=lambda c, r: (
            (
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
            else ""
        ),
    )
    if overrides:
        for key, value in overrides.items():
            setattr(d, key, value)
    return d, calls


def _register_with(overrides=None):
    """用独立（可选覆盖校验器）的 deps 在全新 mcp 上注册，避免与默认工具串扰。"""
    mcp = _FakeMcp()
    deps, calls = _make_deps(overrides=overrides)
    register(mcp, deps)
    return mcp, deps, calls


@pytest.fixture
def env():
    mcp, deps, calls = _register_with()
    return mcp, deps, calls


def _call(env, name, **kw):
    mcp, deps, calls = env
    fn = next(t for t in mcp.tools if t.__name__ == name)
    return fn(**kw), calls


@pytest.fixture
def tool_names(env):
    mcp, _, _ = env
    return {t.__name__ for t in mcp.tools}


# ---------------------------------------------------------------------------
# 注册与注解
# ---------------------------------------------------------------------------


def test_registers_three_tools(tool_names):
    assert tool_names == {"stata_lincom", "stata_nlcom", "stata_hausman"}


def test_all_tools_readonly_non_destructive(env):
    mcp, _, _ = env
    for name, ann in mcp.annotations.items():
        assert ann.readOnlyHint is True, f"{name} 应为只读"
        assert ann.destructiveHint is False, f"{name} 不应具破坏性"


# ---------------------------------------------------------------------------
# stata_lincom
# ---------------------------------------------------------------------------


def test_lincom_full_command(env):
    result, calls = _call(
        env, "stata_lincom", expression="_b[mpg] + _b[weight]", options="level(95)"
    )
    assert result == "lincom _b[mpg] + _b[weight], level(95)"
    assert calls == [("lincom _b[mpg] + _b[weight], level(95)", 60)]


def test_lincom_without_options(env):
    result, calls = _call(env, "stata_lincom", expression="2*_b[mpg] - _b[weight]")
    assert result == "lincom 2*_b[mpg] - _b[weight]"
    assert calls == [("lincom 2*_b[mpg] - _b[weight]", 60)]


def test_lincom_trims_whitespace(env):
    result, calls = _call(env, "stata_lincom", expression="  _b[mpg] + _b[weight]  ")
    assert result == "lincom _b[mpg] + _b[weight]"
    assert calls == [("lincom _b[mpg] + _b[weight]", 60)]


def test_lincom_expression_required(env):
    result, calls = _call(env, "stata_lincom", expression="")
    assert result.startswith("错误")
    assert calls == []


def test_lincom_rejects_injection():
    bad = "错误: expression 含非法字符（不支持换行/分号等）"
    mcp, deps, calls = _register_with(
        overrides={"validate_no_injection": lambda v, label="参数": bad}
    )
    result, calls_after = _call((mcp, deps, calls), "stata_lincom", expression="_b[x] ; drop _all")
    assert result == bad
    assert calls_after == []


# ---------------------------------------------------------------------------
# stata_nlcom
# ---------------------------------------------------------------------------


def test_nlcom_full_command(env):
    result, calls = _call(
        env,
        "stata_nlcom",
        expression="(_b[x1]/_b[x2]) (exp(_b[x1]))",
        options="level(90)",
    )
    assert result == "nlcom (_b[x1]/_b[x2]) (exp(_b[x1])), level(90)"
    assert calls == [("nlcom (_b[x1]/_b[x2]) (exp(_b[x1])), level(90)", 60)]


def test_nlcom_without_options(env):
    result, calls = _call(env, "stata_nlcom", expression="exp(_b[mpg])")
    assert result == "nlcom exp(_b[mpg])"
    assert calls == [("nlcom exp(_b[mpg])", 60)]


def test_nlcom_expression_required(env):
    result, calls = _call(env, "stata_nlcom", expression="   ")
    assert result.startswith("错误")
    assert calls == []


def test_nlcom_rejects_injection():
    bad = "错误: expression 含非法字符"
    mcp, deps, calls = _register_with(
        overrides={"validate_no_injection": lambda v, label="参数": bad}
    )
    result, calls_after = _call(
        (mcp, deps, calls), "stata_nlcom", expression="_b[x] ; shell echo x"
    )
    assert result == bad
    assert calls_after == []


# ---------------------------------------------------------------------------
# stata_hausman
# ---------------------------------------------------------------------------


def test_hausman_full_command(env):
    result, calls = _call(
        env, "stata_hausman", consistent="fe", efficient="re", options="sigmamore"
    )
    assert result == "hausman fe re, sigmamore"
    assert calls == [("hausman fe re, sigmamore", 60)]


def test_hausman_efficient_omitted(env):
    result, calls = _call(env, "stata_hausman", consistent="fe")
    assert result == "hausman fe"
    assert calls == [("hausman fe", 60)]


def test_hausman_without_options(env):
    result, calls = _call(env, "stata_hausman", consistent="fe", efficient="re")
    assert result == "hausman fe re"
    assert calls == [("hausman fe re", 60)]


def test_hausman_consistent_required(env):
    result, calls = _call(env, "stata_hausman", consistent="")
    assert result.startswith("错误")
    assert calls == []


def test_hausman_rejects_bad_consistent():
    bad = "错误: consistent 必须是合法标识符（只含字母/数字/下划线）"
    mcp, deps, calls = _register_with(
        overrides={
            "validate_identifier": lambda v, label="变量名", required=False: (
                bad if label == "consistent" else None
            )
        }
    )
    result, calls_after = _call((mcp, deps, calls), "stata_hausman", consistent="fe; drop")
    assert result == bad
    assert calls_after == []


def test_hausman_rejects_bad_efficient():
    bad = "错误: efficient 必须是合法标识符（只含字母/数字/下划线）"
    mcp, deps, calls = _register_with(
        overrides={
            "validate_identifier": lambda v, label="变量名", required=False: (
                bad if label == "efficient" else None
            )
        }
    )
    result, calls_after = _call(
        (mcp, deps, calls), "stata_hausman", consistent="fe", efficient="re; drop"
    )
    assert result == bad
    assert calls_after == []


def test_hausman_rejects_injection_in_options():
    bad = "错误: options 含非法字符"
    mcp, deps, calls = _register_with(
        overrides={"validate_no_injection": lambda v, label="参数": bad}
    )
    result, calls_after = _call(
        (mcp, deps, calls), "stata_hausman", consistent="fe", options="sigmamore ; drop"
    )
    assert result == bad
    assert calls_after == []
