"""会话生命周期工具（stata_clear / stata_snapshot / stata_read_log）的单元测试。"""

from unittest.mock import patch

from fastmcp.tools.base import ToolResult

import server
from server import stata_clear, stata_read_log, stata_snapshot


def _text(result):
    if isinstance(result, ToolResult):
        return result.content[0].text
    return result


# ---------------------------------------------------------------------------
# stata_clear
# ---------------------------------------------------------------------------


def test_clear_rejects_bad_scope():
    result = stata_clear(scope="nukes")
    assert isinstance(result, ToolResult) and result.is_error
    assert "错误" in _text(result)


def test_clear_data_command():
    with patch("server._run_stata_command") as mock_run:
        mock_run.return_value = "ok"
        stata_clear(scope="data")
        assert mock_run.call_args.args[0] == ("clear all\ncapture frame drop _all")


def test_clear_all_command_and_state_reset(tmp_path):
    # 预置资源与翻页缓存，验证 scope="all" 一并清空
    p = tmp_path / "a.csv"
    p.write_text("x\n")
    server._register_resource(str(p), "test")
    with server._output_lock:
        server._last_output = "stale"
    with patch("server._run_stata_command") as mock_run:
        mock_run.return_value = "ok"
        stata_clear(scope="all")
        assert mock_run.call_args.args[0] == (
            "clear all\ncapture frame drop _all\n"
            "capture estimates clear\ncapture graph drop _all\n"
            "capture xtset, clear\ncapture tsset, clear"
        )
    assert server._resource_lookup(str(p)) is None
    with server._output_lock:
        assert server._last_output == ""


def test_clear_estimates_only_keeps_resources(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("x\n")
    server._register_resource(str(p), "test")
    with patch("server._run_stata_command") as mock_run:
        mock_run.return_value = "ok"
        stata_clear(scope="estimates")
        assert mock_run.call_args.args[0] == "capture estimates clear"
    # 非 all scope 不撤销资源登记
    assert server._resource_lookup(str(p)) is not None


# ---------------------------------------------------------------------------
# stata_snapshot
# ---------------------------------------------------------------------------


def test_snapshot_rejects_bad_action():
    result = stata_snapshot(action="revert")
    assert isinstance(result, ToolResult) and result.is_error


def test_snapshot_list_command():
    with patch("server._run_stata_command") as mock_run:
        mock_run.return_value = "snapshots"
        stata_snapshot(action="list")
    assert mock_run.call_args.args[0] == "snapshot list"


def test_snapshot_restore_requires_number():
    result = stata_snapshot(action="restore")
    assert isinstance(result, ToolResult) and result.is_error
    assert "number" in _text(result)


def test_snapshot_restore_command():
    with patch("server._run_stata_command") as mock_run:
        mock_run.return_value = "restored"
        stata_snapshot(action="restore", number=3)
    assert mock_run.call_args.args[0] == "snapshot restore 3"


def test_snapshot_erase_command():
    with patch("server._run_stata_command") as mock_run:
        mock_run.return_value = "erased"
        stata_snapshot(action="erase", number=2)
    assert mock_run.call_args.args[0] == "snapshot erase 2"


def test_snapshot_save_with_label():
    with patch("server._run_stata_command") as mock_run:
        mock_run.return_value = "ok"
        stata_snapshot(action="save", label="清洗后")
    cmd = mock_run.call_args.args[0]
    # save 输出本身已含创建确认，不再附加 snapshot list（避免重复打印）
    assert cmd == 'snapshot save, label("清洗后")'


def test_snapshot_save_no_label():
    with patch("server._run_stata_command") as mock_run:
        mock_run.return_value = "ok"
        stata_snapshot(action="save")
    cmd = mock_run.call_args.args[0]
    assert cmd == "snapshot save"


def test_snapshot_save_rejects_quotes_in_label():
    result = stata_snapshot(action="save", label='x" y')
    assert isinstance(result, ToolResult) and result.is_error


# ---------------------------------------------------------------------------
# stata_read_log
# ---------------------------------------------------------------------------


def test_read_log_path(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_LOG_FILE", str(tmp_path / "stata-mcp.log"))
    result = stata_read_log(action="path")
    assert "stata-mcp.log" in _text(result)


def test_read_log_tail(tmp_path, monkeypatch):
    log = tmp_path / "stata-mcp.log"
    log.write_text("line1\nline2\nline3\n", encoding="utf-8")
    monkeypatch.setattr(server, "_LOG_FILE", str(log))
    result = stata_read_log(action="tail", lines=2)
    assert _text(result) == "line2\nline3\n"


def test_read_log_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_LOG_FILE", str(tmp_path / "absent.log"))
    result = stata_read_log()
    assert isinstance(result, ToolResult) and result.is_error


def test_read_log_bad_action():
    result = stata_read_log(action="delete")
    assert isinstance(result, ToolResult) and result.is_error


def test_clear_returns_confirmation_not_empty_hint():
    """清空是故意的，不应误报「请先载入数据」（实战发现）。"""
    with patch("server._run_stata_command", return_value="(无输出：当前内存中没有数据集。)"):
        result = stata_clear(scope="all")
    assert isinstance(result, str)
    assert "已清理会话" in result
    assert "当前内存中没有数据集" not in result
