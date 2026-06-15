"""Tests for the core Stata execution engine (without a real Stata install)."""

from unittest.mock import patch, MagicMock

import pytest

from server import (
    _execute_safe,
    _execute_single,
    _ping_stata,
    STATA_RC_NO_OUTPUT,
)


def _fake_redirect_context():
    """Return a MagicMock usable as a context manager for RedirectOutput."""
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=None)
    ctx.__exit__ = MagicMock(return_value=None)
    return ctx


@pytest.fixture
def exec_mocks():
    """Common mocks for _execute_single tests."""
    with (
        patch("server.stout.RedirectOutput", return_value=_fake_redirect_context()) as redirect,
        patch("server.config.get_encode_str", return_value=b"display 42") as encode,
        patch("server.config.stlib.StataSO_Execute") as execute,
        patch("server._drain_output", return_value="") as drain,
        patch("server.config.get_output", return_value=None) as get_output,
    ):
        yield {
            "redirect": redirect,
            "encode": encode,
            "execute": execute,
            "drain": drain,
            "get_output": get_output,
        }


def test_execute_single_returns_rc_and_output(exec_mocks):
    exec_mocks["execute"].return_value = 0
    exec_mocks["get_output"].side_effect = ["hello\n", None, None, None]
    rc, out = _execute_single("display 42")
    assert rc == 0
    assert "hello" in out
    exec_mocks["execute"].assert_called_once()


def test_execute_single_treats_rc_3000_as_success(exec_mocks):
    exec_mocks["execute"].return_value = STATA_RC_NO_OUTPUT
    rc, out = _execute_single("ereturn list")
    assert rc == STATA_RC_NO_OUTPUT


def test_execute_single_catches_dll_crash_as_rc_999(exec_mocks):
    exec_mocks["execute"].side_effect = SystemError("DLL crashed")
    rc, out = _execute_single("bad command")
    assert rc == 999
    assert "崩溃" in out or "crashed" in out.lower()


def test_execute_single_triggers_watchdog_on_timeout(exec_mocks):
    """Simulate a command that never completes: StataSO_Execute blocks longer than timeout."""
    import time

    def slow_execute(*args, **kwargs):
        time.sleep(0.2)
        return 0

    exec_mocks["execute"].side_effect = slow_execute
    with patch("server._set_break") as set_break:
        rc, out = _execute_single("sleep", timeout=0.05)
    # The watchdog should have issued SetBreak.
    set_break.assert_called()
    assert rc == 0


def test_execute_safe_returns_error_when_ping_fails():
    """If ping says Stata is dead, _execute_safe should return RC=998 immediately."""
    with patch("server._ping_stata", return_value=False):
        rc, out = _execute_safe("summarize mpg")
    assert rc == 998
    assert "无响应" in out or "unresponsive" in out.lower()


def test_execute_safe_skips_ping_when_cache_valid():
    """Within PING_CACHE_SECONDS, _execute_safe should skip ping and run command."""
    import time

    with (
        patch("server._ping_stata") as ping,
        patch("server._execute_single", return_value=(0, "ok")) as execute,
        patch("server._last_ping_time", time.time()),
    ):
        rc, out = _execute_safe("summarize mpg")
    ping.assert_not_called()
    execute.assert_called_once()
    assert rc == 0


def test_execute_safe_upgrades_to_998_when_recovery_fails():
    """After a DLL crash (rc=999) if recovery ping fails, return code becomes 998."""
    # Sequence: initial ping succeeds with "42", main command crashes (999),
    # recovery ping returns output without "42" -> False.
    side_effect = [(0, "42"), (999, "boom"), (0, "pong")]
    with (
        patch("server._execute_single", side_effect=side_effect) as execute,
        patch("server._drain_output"),
        patch("server._set_break"),
    ):
        rc, out = _execute_safe(" dangerous ")
    # The main command crashed and recovery ping failed.
    assert rc == 998
    assert "无法自动恢复" in out


def test_ping_stata_uses_execute_single_and_checks_output():
    """_ping_stata should run through _execute_single and look for '42'."""
    with patch("server._execute_single", return_value=(0, "42")) as execute:
        result = _ping_stata()
    assert result is True
    execute.assert_called_once()
    assert execute.call_args[0][0] == "display 42"


def test_ping_stata_returns_false_when_no_output():
    with patch("server._execute_single", return_value=(0, "")), patch("server._drain_output"):
        assert _ping_stata() is False
