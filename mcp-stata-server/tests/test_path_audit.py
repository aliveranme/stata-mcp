"""自由文本路径审计的单元测试。

配置 STATA_ALLOWED_ROOTS 后，stata_run 等自由文本命令里的引号路径必须落在白名单内
（此前是文档化缺口：`stata_run('use "越界路径"')` 照常执行）。
"""

from unittest.mock import patch

import pytest

import server
from server import _audit_block_paths, _init_allowed_roots, stata_run


@pytest.fixture(autouse=True)
def _reset_roots(monkeypatch):
    """每个用例重置 ALLOWED_ROOTS 缓存与 env，消除用例间污染。"""
    server._ALLOWED_ROOTS_CACHE = None
    monkeypatch.delenv("STATA_ALLOWED_ROOTS", raising=False)
    yield
    server._ALLOWED_ROOTS_CACHE = None


@pytest.fixture
def roots(monkeypatch):
    """配置沙箱白名单为 /allowed。"""
    server._ALLOWED_ROOTS_CACHE = None
    monkeypatch.setenv("STATA_ALLOWED_ROOTS", "/allowed")
    return "/allowed"


def _audit(block, cwd="/allowed"):
    """调用 _audit_block_paths（持锁），patch 掉 Stata cwd 查询。"""
    with server._stata_lock:
        with patch("server._get_stata_cwd_locked", return_value=cwd):
            return _audit_block_paths(block)


# --- 基本拦截 ---


def test_audit_rejects_outside_absolute_path(roots):
    err = _audit('use "/evil/secret.dta"')
    assert err and "沙箱外" in err and "/evil/secret.dta" in err


def test_audit_allows_inside_path(roots):
    assert _audit('use "/allowed/data.dta"') is None


def test_audit_rejects_using_clause(roots):
    err = _audit('import excel using "/evil/out.xlsx", firstrow')
    assert err and "沙箱外" in err


def test_audit_rejects_merge_using(roots):
    err = _audit('merge 1:1 price using "/evil/master.dta"')
    assert err and "沙箱外" in err


def test_audit_rejects_graph_export(roots):
    err = _audit('graph export "/evil/chart.png", replace')
    assert err and "沙箱外" in err


def test_audit_rejects_save(roots):
    err = _audit('save "/evil/out.dta", replace')
    assert err and "沙箱外" in err


def test_audit_rejects_do_run_include(roots):
    for cmd in ('do "/evil/x.do"', 'run "/evil/x.do"', 'include "/evil/x.ado"'):
        assert _audit(cmd), f"应拦截 {cmd}"


def test_audit_rejects_cd(roots):
    err = _audit('cd "/evil/dir"')
    assert err and "沙箱外" in err


# --- 不误伤 ---


def test_audit_ignores_non_data_commands(roots):
    assert _audit('display "hello world"') is None
    assert _audit('summarize price if price > 5000') is None
    assert _audit('regress price mpg, robust') is None


def test_audit_ignores_webuse_and_sysuse(roots):
    assert _audit("webuse auto, clear") is None  # URL，非本地路径
    assert _audit("sysuse auto, clear") is None  # 本地库，非用户路径


def test_audit_ignores_macro_paths(roots):
    assert _audit('use "$mydir/data.dta"') is None  # 宏无法静态解析，fail-open
    assert _audit("use `dir'/data.dta") is None


def test_audit_ignores_unquoted_bare_token(roots):
    """裸单 token 可能是 varlist/选项，不审计以免误伤。"""
    assert _audit("use auto.dta, clear") is None
    assert _audit("save out.dta, replace") is None


def test_audit_ignores_url(roots):
    assert _audit('net install foo, from("https://example.com/x")') is None


def test_audit_relative_path_resolves_against_cwd(roots):
    # ../../ 从 /allowed/sub 解析到 /（沙箱外）
    err = _audit('use "../../escape.dta"', cwd="/allowed/sub")
    assert err and "沙箱外" in err
    # 同级 / 子目录解析后仍在沙箱内
    assert _audit('use "../escape.dta"', cwd="/allowed/sub") is None
    assert _audit('use "data.dta"', cwd="/allowed/sub") is None


def test_audit_allows_capture_prefix(roots):
    """通用前缀不改变被执行的命令，路径审计同样要先剥前缀。"""
    assert _audit('capture noisily use "/allowed/a.dta"') is None
    err = _audit('quietly use "/evil/a.dta"')
    assert err and "沙箱外" in err


# --- 启用门控 ---


def test_audit_disabled_without_roots():
    """未配置白名单时不启用审计（向后兼容）。"""
    with patch("server._get_stata_cwd_locked", return_value="/"):
        assert _audit_block_paths('use "/evil/secret.dta"') is None


def test_roots_cache_invalidation(monkeypatch):
    """切换 env 后缓存要失效。"""
    server._ALLOWED_ROOTS_CACHE = None
    assert _init_allowed_roots() == ()
    monkeypatch.setenv("STATA_ALLOWED_ROOTS", "/allowed")
    server._ALLOWED_ROOTS_CACHE = None
    assert "/allowed/" in _init_allowed_roots()


# --- 经 stata_run 集成（mock 执行）---


def test_stata_run_rejects_outside_path(roots, monkeypatch):
    monkeypatch.setattr(server, "_execute_safe", lambda *a, **k: (0, "ok"))
    result = stata_run('use "/evil/secret.dta"')
    assert getattr(result, "is_error", False)
    assert "沙箱外" in str(result)


def test_stata_run_allows_inside_path(roots, monkeypatch):
    calls = []

    def fake_exec(cmd, timeout=60, full_output_path=None, cancel_event=None):
        calls.append(cmd)
        return (0, "ok")

    monkeypatch.setattr(server, "_execute_safe", fake_exec)
    result = stata_run('use "/allowed/data.dta"')
    assert not getattr(result, "is_error", False)
    assert calls and "use" in calls[0]


def test_audit_matches_stata_abbreviations(roots):
    """官方缩写（sav/imp/cop 等）不得绕过路径审计（第二轮审查发现）。"""
    assert _audit('sav "/evil/out.dta", replace') is not None
    assert _audit('imp excel using "/evil/x.xlsx"') is not None
    assert _audit('cop "/evil/a.dta" "/allowed/b.dta"') is not None
    assert _audit('exp delimited "/evil/x.csv"') is not None
    assert _audit('lo using "/evil/x.log"') is not None
    # 精确 token：lowess 不应被误当 lo
    assert _audit("lowess price weight") is None


def test_audit_pkg_abbreviation(roots):
    """net ins / github ins 缩写不得绕过包管理拦截。"""
    from server import _flag_unmanaged_package_commands

    assert _flag_unmanaged_package_commands("net ins foo, from(x)") == ["net ins foo, from(x)"]
    assert _flag_unmanaged_package_commands("github ins bar") == ["github ins bar"]
    assert _flag_unmanaged_package_commands("version 15: net install foo, from(x)") == [
        "version 15: net install foo, from(x)"
    ]
    assert _flag_unmanaged_package_commands("ssc install estout") == []


def test_background_path_audit(roots, monkeypatch):
    """后台任务的自由文本同样受路径审计（第二轮审查发现此前跳过）。"""
    from unittest.mock import patch

    import server
    from server import _BackgroundTask, _bg_worker

    task = _BackgroundTask(task_id="audit1", command='use "/evil/x.dta"', timeout=60)
    monkeypatch.setattr(server, "_get_stata_cwd_locked", lambda: "/allowed")
    # 审计应在执行前拦截；若审计未触发会调用 _execute_safe（mock 会挂起），
    # 故防御性 patch 使其抛错证明未被调用。
    with patch("server._execute_safe", side_effect=AssertionError("不应执行")):
        _bg_worker(task)  # _bg_worker 内部自行持 _stata_lock（非重入，外层不能再包）
    assert task.status == "failed"
    assert "沙箱外" in task.result
