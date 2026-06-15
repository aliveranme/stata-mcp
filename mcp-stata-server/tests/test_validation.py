"""Tests for input validation helpers."""

import pytest

from server import (
    _has_dangerous_command_prefix,
    _validate_no_injection,
    _validate_varlist,
)


@pytest.mark.parametrize(
    "cmd",
    [
        "!dir",
        "! rm -rf /",  # purely a test payload string, never executed
        "shell notepad.exe",
        # Intentionally dangerous payload string for filter testing; never executed.
        "python: __import__('os').popen('whoami')",
        "python (print(1))",
        " summarize mpg\n!dir",
    ],
)
def test_dangerous_command_prefix_blocks(cmd):
    assert _has_dangerous_command_prefix(cmd) is not None


@pytest.mark.parametrize(
    "cmd",
    [
        "summarize mpg",
        "regress price mpg weight",
        "capture noisily { twoway scatter price weight }",
    ],
)
def test_dangerous_command_prefix_allows_safe(cmd):
    assert _has_dangerous_command_prefix(cmd) is None


def test_validate_no_injection_rejects_newline_and_semicolon():
    assert _validate_no_injection("a\nb", "x") is not None
    assert _validate_no_injection("a;b", "x") is not None
    assert _validate_no_injection("ok", "x") is None


def test_validate_varlist_allows_stata_extensions():
    assert _validate_varlist("i.foreign mpg L.price c.price##i.foreign [aw=weight]") is None
    assert _validate_varlist("x1-x10 mpg*") is None


def test_validate_varlist_rejects_dangerous_chars():
    assert _validate_varlist("mpg\nuse auto, clear") is not None
    assert _validate_varlist("mpg; use auto") is not None
