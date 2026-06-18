"""Tests for ALLOWED_ROOTS path sandbox."""

import os

import pytest

from server import (
    _canonicalize_path,
    _is_path_allowed,
    _validate_path,
)


def _reset_roots_cache():
    """Clear the ALLOWED_ROOTS cache between tests."""
    import server

    server._ALLOWED_ROOTS_CACHE = None
    server._STATA_ALLOWED_ROOTS_ENV = os.environ.get("STATA_ALLOWED_ROOTS", "")


@pytest.fixture(autouse=True)
def _no_sandbox_by_default(monkeypatch):
    """Default: no sandbox configured. Sandbox tests set their own env."""
    monkeypatch.setenv("STATA_ALLOWED_ROOTS", "")
    _reset_roots_cache()
    yield
    _reset_roots_cache()


class TestCanonicalizePath:
    def test_normalizes_backslashes(self):
        result = _canonicalize_path(r"C:\Users\test\data.dta")
        assert "/" in result
        assert "\\" not in result

    def test_resolves_relative_via_abspath(self):
        result = _canonicalize_path("relative/path.dta")
        # Windows paths start with drive letter + colon + slash
        assert len(result) > 3
        assert "/" in result


class TestIsPathAllowed:
    def test_returns_true_when_no_roots_configured(self):
        assert _is_path_allowed("C:/any/path.dta") is True

    def test_blocks_path_outside_allowed_root(self, monkeypatch):
        monkeypatch.setenv("STATA_ALLOWED_ROOTS", "C:/data")
        _reset_roots_cache()
        assert _is_path_allowed("C:/data/project/file.dta") is True
        assert _is_path_allowed("C:/Windows/evil.dta") is False

    def test_allows_multiple_roots(self, monkeypatch):
        monkeypatch.setenv("STATA_ALLOWED_ROOTS", "C:/data;D:/projects")
        _reset_roots_cache()
        assert _is_path_allowed("C:/data/file.dta") is True
        assert _is_path_allowed("D:/projects/analysis.dta") is True
        assert _is_path_allowed("E:/other/file.dta") is False


class TestValidatePathWithSandbox:
    def test_normal_path_without_sandbox_passes(self):
        assert _validate_path("C:/temp/test.dta") is None

    def test_path_outside_sandbox_fails(self, monkeypatch):
        monkeypatch.setenv("STATA_ALLOWED_ROOTS", "C:/data")
        _reset_roots_cache()
        result = _validate_path("C:/Windows/evil.dta")
        assert result is not None
        assert "不在允许目录下" in result

    def test_path_inside_sandbox_passes(self, monkeypatch):
        monkeypatch.setenv("STATA_ALLOWED_ROOTS", "C:/data")
        _reset_roots_cache()
        assert _validate_path("C:/data/project/file.dta") is None

    def test_injection_chars_still_rejected(self):
        assert _validate_path("file\x00.dta") is not None
        assert _validate_path('file".dta') is not None

    def test_relative_path_traversal_rejected(self):
        result = _validate_path("../etc/passwd")
        assert result is not None
        assert "相对路径不能超出" in result
