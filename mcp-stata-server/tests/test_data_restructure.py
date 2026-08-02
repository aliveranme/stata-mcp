import types

import pytest

from tool_modules.data_restructure import register


class _FakeMcp:
    def __init__(self):
        self.tools = []
        self.annotations = {}

    def tool(self, annotations=None, **kw):
        def deco(fn):
            self.tools.append(fn)
            self.annotations[fn.__name__] = annotations
            return fn

        return deco


def _make_deps(**overrides):
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
            " " + " ".join(
                x for x in (f"if {c}" if c.strip() else "", f"in {r}" if r.strip() else "") if x
            )
        ) if (c.strip() or r.strip()) else "",
    )
    for name, fn in overrides.items():
        if fn is not None:
            setattr(d, name, fn)
    return d, calls


@pytest.fixture
def env():
    mcp = _FakeMcp()
    deps, calls = _make_deps()
    register(mcp, deps)
    return mcp, deps, calls


def _call(env, name, **kw):
    mcp, deps, calls = env
    fn = next(t for t in mcp.tools if t.__name__ == name)
    return fn(**kw), calls


# ---------- 校验器返回错误文本的 deps（用于错误分支测试） ----------

def _bad_identifier(value, label="变量名", required=False):
    if not (value or "").strip():
        if required:
            return f"错误: {label} 不能为空"
        return None
    return None


def _bad_varlist(value, label="varlist"):
    return f"错误: {label} 包含非法字符" if (value or "").strip() else None


def _bad_filter_expr(value, label):
    return f"错误: {label} 不能包含注释记号" if (value or "").strip() else None


def _bad_no_injection(value, label="参数"):
    return f"错误: {label} 包含非法字符" if (value or "").strip() else None


def _err_env(validate_identifier=None, validate_varlist=None, validate_filter_expr=None, validate_no_injection=None):
    deps, calls = _make_deps(
        validate_identifier=validate_identifier,
        validate_varlist=validate_varlist,
        validate_filter_expr=validate_filter_expr,
        validate_no_injection=validate_no_injection,
    )
    mcp = _FakeMcp()
    register(mcp, deps)
    return mcp, deps, calls


def _invoke(mcp, name, **kw):
    fn = next(t for t in mcp.tools if t.__name__ == name)
    return fn(**kw)


# ---------- 装配 ----------

def test_registers_six_tools(env):
    mcp, deps, calls = env
    names = [t.__name__ for t in mcp.tools]
    assert names == [
        "stata_replace",
        "stata_drop",
        "stata_keep",
        "stata_rename",
        "stata_recode",
        "stata_destring",
    ]


def test_all_tools_are_destructive(env):
    mcp, deps, calls = env
    assert set(mcp.annotations) == {
        "stata_replace",
        "stata_drop",
        "stata_keep",
        "stata_rename",
        "stata_recode",
        "stata_destring",
    }
    for name, ann in mcp.annotations.items():
        assert ann.readOnlyHint is False, name
        assert ann.destructiveHint is True, name


# ---------- stata_replace ----------

def test_replace_full_command(env):
    result, calls = _call(
        env, "stata_replace",
        varname="mpg", expression="weight/100",
        condition="foreign == 1", in_range="1/50", options="nopromote",
    )
    expected = "replace mpg = weight/100 if foreign == 1 in 1/50, nopromote"
    assert result == expected
    assert calls == [(expected, 60)]


def test_replace_minimal_command(env):
    result, calls = _call(env, "stata_replace", varname="mpg", expression="weight/100")
    assert result == "replace mpg = weight/100"
    assert calls == [("replace mpg = weight/100", 60)]


def test_replace_expression_is_free_text(env):
    result, _ = _call(env, "stata_replace", varname="price", expression="ln(price) + 0.5*weight")
    assert result == "replace price = ln(price) + 0.5*weight"


def test_replace_empty_expression_error(env):
    result, calls = _call(env, "stata_replace", varname="mpg", expression="   ")
    assert result.startswith("ERR:")
    assert "错误: " in result
    assert calls == []


def test_replace_required_varname_error():
    mcp, deps, calls = _err_env(validate_identifier=_bad_identifier)
    result = _invoke(mcp, "stata_replace", varname="", expression="1")
    assert result.startswith("错误")
    assert calls == []


def test_replace_condition_error():
    mcp, deps, calls = _err_env(validate_filter_expr=_bad_filter_expr)
    result = _invoke(mcp, "stata_replace", varname="mpg", expression="1", condition="foreign==1")
    assert result.startswith("错误")
    assert calls == []


def test_replace_expression_comment_rejected():
    """expression 里的 // 会把尾部保护性 if/in 注释掉，必须在入口拦下。"""
    mcp, deps, calls = _err_env(validate_filter_expr=_bad_filter_expr)
    result = _invoke(mcp, "stata_replace", varname="mpg", expression="weight // evil", condition="foreign==1")
    assert result.startswith("错误")
    assert calls == []


def test_replace_options_error():
    mcp, deps, calls = _err_env(validate_no_injection=_bad_no_injection)
    result = _invoke(mcp, "stata_replace", varname="mpg", expression="1", options=";evil")
    assert result.startswith("错误")
    assert calls == []


# ---------- stata_drop ----------

def test_drop_varlist_command(env):
    result, calls = _call(env, "stata_drop", varlist="price mpg")
    assert result == "drop price mpg"
    assert calls == [("drop price mpg", 60)]


def test_drop_all_command(env):
    result, _ = _call(env, "stata_drop", varlist="_all")
    assert result == "drop _all"


def test_drop_observations_command(env):
    result, _ = _call(env, "stata_drop", condition="foreign == 1", in_range="1/20")
    assert result == "drop if foreign == 1 in 1/20"


def test_drop_both_error(env):
    result, calls = _call(env, "stata_drop", varlist="price", condition="foreign == 1")
    assert result.startswith("ERR:")
    assert "二选一" in result
    assert calls == []


def test_drop_neither_error(env):
    result, calls = _call(env, "stata_drop")
    assert result.startswith("ERR:")
    assert "错误: " in result
    assert calls == []


def test_drop_invalid_varlist_error():
    mcp, deps, calls = _err_env(validate_varlist=_bad_varlist)
    result = _invoke(mcp, "stata_drop", varlist="price")
    assert result.startswith("错误")
    assert calls == []


# ---------- stata_keep ----------

def test_keep_varlist_command(env):
    result, calls = _call(env, "stata_keep", varlist="price mpg")
    assert result == "keep price mpg"
    assert calls == [("keep price mpg", 60)]


def test_keep_observations_command(env):
    result, _ = _call(env, "stata_keep", condition="foreign == 1", in_range="1/20")
    assert result == "keep if foreign == 1 in 1/20"


def test_keep_both_error(env):
    result, calls = _call(env, "stata_keep", varlist="price", in_range="1/20")
    assert result.startswith("ERR:")
    assert "二选一" in result
    assert calls == []


def test_keep_neither_error(env):
    result, calls = _call(env, "stata_keep")
    assert result.startswith("ERR:")
    assert "错误: " in result
    assert calls == []


# ---------- stata_rename ----------

def test_rename_single_command(env):
    result, calls = _call(env, "stata_rename", oldname="price", newname="price_new")
    assert result == "rename price price_new"
    assert calls == [("rename price price_new", 60)]


def test_rename_options_command(env):
    result, _ = _call(env, "stata_rename", oldname="price", newname="p", options="dryrun")
    assert result == "rename price p, dryrun"


def test_rename_batch_command(env):
    result, _ = _call(env, "stata_rename", oldname="(a b c)", newname="(x y z)")
    assert result == "rename (a b c) (x y z)"


def test_rename_batch_mismatch_error(env):
    """批量形式必须成对：一个带括号一个不带会拼出非法命令。"""
    result, calls = _call(env, "stata_rename", oldname="(a b)", newname="x")
    assert result.startswith("ERR:")
    assert "批量" in result
    assert calls == []

    result2, calls2 = _call(env, "stata_rename", oldname="a", newname="(x y)")
    assert result2.startswith("ERR:")
    assert calls2 == []


def test_rename_required_oldname_error():
    mcp, deps, calls = _err_env(validate_identifier=_bad_identifier)
    result = _invoke(mcp, "stata_rename", oldname="", newname="x")
    assert result.startswith("错误")
    assert calls == []


def test_rename_required_newname_error():
    mcp, deps, calls = _err_env(validate_identifier=_bad_identifier)
    result = _invoke(mcp, "stata_rename", oldname="a", newname="")
    assert result.startswith("错误")
    assert calls == []


def test_rename_options_error():
    mcp, deps, calls = _err_env(validate_no_injection=_bad_no_injection)
    result = _invoke(mcp, "stata_rename", oldname="a", newname="b", options=";x")
    assert result.startswith("错误")
    assert calls == []


# ---------- stata_recode ----------

def test_recode_command(env):
    result, calls = _call(env, "stata_recode", varlist="price", values="(1=0) (2/4=1)")
    expected = "recode price (1=0) (2/4=1)"
    assert result == expected
    assert calls == [(expected, 60)]


def test_recode_full_command(env):
    result, _ = _call(
        env, "stata_recode",
        varlist="price mpg", values="(1=0) (2/4=1)",
        condition="foreign == 1", in_range="1/100", options="generate(newvar)",
    )
    assert result == "recode price mpg (1=0) (2/4=1) if foreign == 1 in 1/100, generate(newvar)"


def test_recode_multivar_bare_values_error(env):
    """多变量 recode 必须给括号规则组（官方仅单变量可省括号）。"""
    result, calls = _call(env, "stata_recode", varlist="price mpg", values="nonmiss=1")
    assert result.startswith("ERR:")
    assert "括号" in result
    assert calls == []


def test_recode_single_var_bare_values_ok(env):
    """单变量允许裸规则（nonmiss=1 是官方合法形式）。"""
    result, calls = _call(env, "stata_recode", varlist="price", values="nonmiss=1")
    assert result == "recode price nonmiss=1"
    assert calls == [(result, 60)]


def test_recode_values_allow_slash(env):
    result, _ = _call(env, "stata_recode", varlist="x", values="(1/5=0)")
    assert result == "recode x (1/5=0)"


def test_recode_empty_varlist_error(env):
    result, calls = _call(env, "stata_recode", varlist="   ", values="(1=0)")
    assert result.startswith("ERR:")
    assert "错误: " in result
    assert calls == []


def test_recode_empty_values_error(env):
    result, calls = _call(env, "stata_recode", varlist="price", values="   ")
    assert result.startswith("ERR:")
    assert "错误: " in result
    assert calls == []


def test_recode_invalid_varlist_error():
    mcp, deps, calls = _err_env(validate_varlist=_bad_varlist)
    result = _invoke(mcp, "stata_recode", varlist="price", values="(1=0)")
    assert result.startswith("错误")
    assert calls == []


def test_recode_invalid_values_error():
    # values 走 filter_expr 级校验（// 会把尾部 if/in 注释掉），不再只查 no_injection
    mcp, deps, calls = _err_env(validate_filter_expr=_bad_filter_expr)
    result = _invoke(mcp, "stata_recode", varlist="price", values="(1=0) // x")
    assert result.startswith("错误")
    assert calls == []


# ---------- stata_destring ----------

def test_destring_replace_force_command(env):
    result, calls = _call(env, "stata_destring", varlist="price mpg", replace=True, force=True)
    expected = "destring price mpg, replace force"
    assert result == expected
    assert calls == [(expected, 60)]


def test_destring_generate_option_command(env):
    result, _ = _call(env, "stata_destring", varlist="price", options="generate(newvar)")
    assert result == "destring price, generate(newvar)"


def test_destring_ignore_command(env):
    result, _ = _call(env, "stata_destring", varlist="price", replace=True, ignore="-")
    assert result == 'destring price, replace ignore("-")'


def test_destring_all_variables_command(env):
    result, _ = _call(env, "stata_destring", replace=True, force=True)
    assert result == "destring, replace force"


def test_destring_no_output_error(env):
    result, calls = _call(env, "stata_destring", varlist="price")
    assert result.startswith("ERR:")
    assert "必须二选一" in result
    assert calls == []


def test_destring_replace_and_generate_conflict_error(env):
    """replace 与 generate() 互斥 —— 同时给会拼出非法命令。"""
    result, calls = _call(
        env, "stata_destring", varlist="price", replace=True, options="generate(newvar)"
    )
    assert result.startswith("ERR:")
    assert "互斥" in result
    assert calls == []


def test_destring_force_ignore_conflict_error(env):
    result, calls = _call(env, "stata_destring", varlist="price", replace=True, force=True, ignore="-")
    assert result.startswith("ERR:")
    assert "二选一" in result
    assert calls == []


def test_destring_invalid_ignore_error():
    mcp, deps, calls = _err_env(validate_no_injection=_bad_no_injection)
    result = _invoke(mcp, "stata_destring", varlist="price", replace=True, ignore=";x")
    assert result.startswith("错误")
    assert calls == []
