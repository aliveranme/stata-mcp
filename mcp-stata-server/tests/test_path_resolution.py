"""Tests for Stata-cwd-aware path resolution (C1 sandbox bypass fix).

验证 use_dataset / run_do_file 的相对路径在锁内用 Stata 实际工作目录解析，
并经沙箱权威校验 —— 消除 Python cwd vs Stata cwd 不一致导致的沙箱绕过。
"""

import os
from unittest.mock import patch

import pytest

import server
from server import _check_abs_path_safety, _resolve_stata_path_locked, _run_stata_command


@pytest.fixture(autouse=True)
def _reset_sandbox_cache(monkeypatch):
    """每个测试前后清理沙箱缓存，避免相互污染。"""
    monkeypatch.setenv("STATA_ALLOWED_ROOTS", "")
    server._ALLOWED_ROOTS_CACHE = None
    yield
    server._ALLOWED_ROOTS_CACHE = None


class TestCheckAbsPathSafety:
    def test_unc_rejected_by_default(self):
        assert _check_abs_path_safety("//server/share/x.dta") is not None

    def test_no_sandbox_allows_any_abs_path(self):
        assert _check_abs_path_safety("C:/any/abs/path.dta") is None

    def test_sandbox_blocks_outside_root(self, monkeypatch):
        monkeypatch.setenv("STATA_ALLOWED_ROOTS", "C:/data")
        server._ALLOWED_ROOTS_CACHE = None
        assert _check_abs_path_safety("C:/Windows/evil.dta") is not None
        assert _check_abs_path_safety("C:/data/ok.dta") is None


class TestResolveStataPathLocked:
    def test_relative_path_uses_stata_cwd(self, monkeypatch):
        """相对路径应用 Stata cwd 解析，而非 Python cwd。"""
        monkeypatch.setenv("STATA_ALLOWED_ROOTS", "C:/statahome")
        server._ALLOWED_ROOTS_CACHE = None
        with patch("server._get_stata_cwd_locked", return_value="C:/statahome"):
            abs_path, err = _resolve_stata_path_locked("data.dta")
        assert err is None
        assert abs_path.replace("\\", "/") == "C:/statahome/data.dta"

    def test_relative_path_blocked_when_stata_cwd_outside_sandbox(self, monkeypatch):
        """Stata cwd 在沙箱外时，相对路径解析后应被权威校验拦截（C1 修复核心）。"""
        monkeypatch.setenv("STATA_ALLOWED_ROOTS", "C:/sandbox")
        server._ALLOWED_ROOTS_CACHE = None
        with patch("server._get_stata_cwd_locked", return_value="C:/sensitive"):
            abs_path, err = _resolve_stata_path_locked("evil.dta")
        assert err is not None
        assert "不在允许目录下" in err
        assert abs_path == ""

    def test_absolute_path_unaffected_by_stata_cwd(self, monkeypatch):
        monkeypatch.setenv("STATA_ALLOWED_ROOTS", "C:/sandbox")
        server._ALLOWED_ROOTS_CACHE = None
        with patch("server._get_stata_cwd_locked", return_value="C:/other"):
            abs_path, err = _resolve_stata_path_locked("C:/sandbox/file.dta")
        assert err is None
        assert abs_path.replace("\\", "/") == "C:/sandbox/file.dta"

    def test_falls_back_to_python_cwd_when_stata_cwd_unavailable(self, monkeypatch):
        """无法获取 Stata cwd 时回退到 Python cwd（向后兼容）。"""
        monkeypatch.setenv("STATA_ALLOWED_ROOTS", "")
        server._ALLOWED_ROOTS_CACHE = None
        with patch("server._get_stata_cwd_locked", return_value=""):
            abs_path, err = _resolve_stata_path_locked("rel.dta")
        assert err is None
        # 回退到 Python cwd，应为绝对路径且以 rel.dta 结尾
        assert os.path.isabs(abs_path)
        assert abs_path.replace("\\", "/").endswith("rel.dta")


class TestRunStataCommandSandboxBypass:
    """端到端验证 _run_stata_command 的 require_file 路径不再被沙箱绕过。"""

    def test_use_dataset_relative_path_blocked_outside_sandbox(self, monkeypatch):
        """相对路径 require_file 在 Stata cwd 沙箱外时被拦截，不执行 Stata 命令。"""
        monkeypatch.setenv("STATA_ALLOWED_ROOTS", "C:/sandbox")
        server._ALLOWED_ROOTS_CACHE = None
        with (
            patch("server._get_stata_cwd_locked", return_value="C:/sensitive"),
            patch("server._execute_safe") as exec_safe,
        ):
            result = _run_stata_command('use "evil.dta"', require_file="evil.dta")
        # 应返回错误（ToolResult），且未调用 _execute_safe
        assert getattr(result, "is_error", False)
        text = result.content[0].text if hasattr(result, "content") else result
        assert "不在允许目录下" in text
        exec_safe.assert_not_called()

    def test_use_dataset_absolute_path_in_sandbox_passes(self, monkeypatch, tmp_path):
        """沙箱内绝对路径文件存在时，正常执行。"""
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        data_file = sandbox / "data.dta"
        data_file.write_text("dummy")
        monkeypatch.setenv("STATA_ALLOWED_ROOTS", str(sandbox).replace("\\", "/"))
        server._ALLOWED_ROOTS_CACHE = None
        with (
            patch("server._get_stata_cwd_locked", return_value="C:/other"),
            patch("server._execute_safe", return_value=(0, "loaded")) as exec_safe,
        ):
            result = _run_stata_command(f'use "{data_file}"', require_file=str(data_file))
        exec_safe.assert_called_once()
        assert getattr(result, "is_error", False) is False
