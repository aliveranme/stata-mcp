"""后台任务管理（stata_background 及任务工具）的单元测试。

_bg_worker 同步调用（不依赖真实线程时序）；_execute_safe 用 patch 短路。
"""

from unittest.mock import patch

from fastmcp.tools.base import ToolResult

import server
from server import (
    _BackgroundTask,
    _bg_cancel,
    _bg_status_text,
    _bg_worker,
    _prune_bg_tasks,
    _submit_bg_task,
    stata_background,
    stata_task_cancel,
    stata_task_list,
    stata_task_result,
    stata_task_status,
)


def _text(result):
    if isinstance(result, ToolResult):
        return result.content[0].text
    return result


def _make_task(command="display 1", timeout=60):
    t = _BackgroundTask(task_id="testtask123456", command=command, timeout=timeout)
    with server._bg_lock:
        server._bg_tasks[t.task_id] = t
    return t


# ---------------------------------------------------------------------------
# _bg_worker 执行语义
# ---------------------------------------------------------------------------


def test_worker_simple_success():
    task = _make_task("display 1")
    with patch("server._execute_safe", return_value=(0, "1")) as mock_exec:
        _bg_worker(task)
    assert task.status == "done"
    assert not task.is_error
    assert task.result == "1"
    mock_exec.assert_called_once()
    assert mock_exec.call_args.args[0] == "display 1"
    assert mock_exec.call_args.kwargs.get("cancel_event") is task.cancel_event


def test_worker_multiblock_concatenates():
    task = _make_task("display 1\ndisplay 2")
    calls = []

    def fake_exec(cmd, timeout=60, full_output_path=None, cancel_event=None):
        calls.append(cmd)
        return (0, "1" if cmd == "display 1" else "2")

    with patch("server._execute_safe", side_effect=fake_exec):
        _bg_worker(task)
    assert task.status == "done"
    assert task.result == "1\n2"
    assert calls == ["display 1", "display 2"]


def test_worker_cancel_between_blocks():
    task = _make_task("display 1\ndisplay 2")

    def fake_exec(cmd, timeout=60, full_output_path=None, cancel_event=None):
        task.cancel_requested = True  # 模拟取消在第一条后到达
        return (0, "1")

    with patch("server._execute_safe", side_effect=fake_exec):
        _bg_worker(task)
    assert task.status == "cancelled"
    assert "已取消" in task.result
    assert "display 2" not in task.result  # 剩余块未执行


def test_worker_error_block_marks_failed():
    task = _make_task("use broken")
    with patch("server._execute_safe", return_value=(601, "file not found")):
        _bg_worker(task)
    assert task.status == "failed"
    assert task.is_error
    assert "[返回码: 601]" in task.result


def test_worker_998_aborts():
    task = _make_task("display 1\ndisplay 2")
    calls = []

    def fake_exec(cmd, timeout=60, full_output_path=None, cancel_event=None):
        calls.append(cmd)
        return (998, "DLL dead") if cmd == "display 1" else (0, "2")

    with patch("server._execute_safe", side_effect=fake_exec):
        _bg_worker(task)
    assert task.status == "failed"
    assert calls == ["display 1"]  # 998 后不继续


def test_worker_unbalanced_block_fails():
    task = _make_task("capture noisily {")
    _bg_worker(task)
    assert task.status == "failed"
    assert "错误" in task.result


def test_worker_empty_command_done():
    task = _make_task("   ")
    _bg_worker(task)
    assert task.status == "done"
    assert "无有效命令" in task.result


# ---------------------------------------------------------------------------
# 提交 / 取消 / 裁剪
# ---------------------------------------------------------------------------


def test_submit_bg_task_returns_id_and_registers(monkeypatch):
    started = {}

    def fake_start(self, *a, **k):
        started["thread"] = self.name

    monkeypatch.setattr(server.threading.Thread, "start", fake_start)
    task_id = _submit_bg_task("display 1", 120)
    assert task_id
    assert started["thread"].startswith("stata-bg-")
    assert _bg_status_text(_server_task(task_id))


def _server_task(task_id):
    with server._bg_lock:
        return server._bg_tasks.get(task_id)


def test_cancel_unknown_task():
    found, msg = _bg_cancel("nope1234")
    assert not found and "未找到" in msg


def test_cancel_finished_task():
    task = _make_task()
    task.status = "done"
    task.finished_at = 1.0
    found, msg = _bg_cancel(task.task_id)
    assert found and "已结束" in msg


def test_cancel_running_task_sets_event_not_direct_break():
    """取消通过 cancel_event 交给看门狗（锁内二次确认），不再直接跨线程 SetBreak。"""
    task = _make_task("display 1")
    task.in_execute = True
    with patch("server._set_break") as mock_break:
        found, msg = _bg_cancel(task.task_id)
    assert found
    assert task.cancel_requested
    assert task.cancel_event.is_set()
    mock_break.assert_not_called()


def test_worker_passes_cancel_event_to_execute_safe():
    task = _make_task("display 1\ndisplay 2")
    received = {}

    def fake_exec(cmd, timeout=60, full_output_path=None, cancel_event=None):
        received["event"] = cancel_event
        return (0, "1")

    with patch("server._execute_safe", side_effect=fake_exec):
        _bg_worker(task)
    assert received["event"] is task.cancel_event


def test_prune_keeps_under_cap():
    for i in range(5):
        t = _BackgroundTask(task_id=f"t{i:012d}", command="display 1", timeout=60)
        t.status = "done"
        t.created_at = float(i)
        with server._bg_lock:
            server._bg_tasks[t.task_id] = t
    _prune_bg_tasks()
    assert len(server._bg_tasks) == 5  # 未超上限不裁剪


# ---------------------------------------------------------------------------
# stata_background 工具
# ---------------------------------------------------------------------------


def test_background_validates_dangerous_prefix():
    result = stata_background("!touch /tmp/evil")
    assert isinstance(result, ToolResult) and result.is_error


def test_background_rejects_null_byte():
    result = stata_background("display 1\x00")
    assert isinstance(result, ToolResult) and result.is_error


def test_background_clamps_timeout_and_submits():
    with patch("server._submit_bg_task", return_value="abc123") as mock_submit:
        result = stata_background("display 1", timeout=1)
    assert "abc123" in _text(result)
    # 1s 被钳制到下限 10s
    mock_submit.assert_called_once_with("display 1", 10)


def test_background_clamps_timeout_upper():
    with patch("server._submit_bg_task", return_value="abc123") as mock_submit:
        stata_background("display 1", timeout=99999)
    mock_submit.assert_called_once_with("display 1", 3600)


def test_task_status_unknown():
    result = stata_task_status("nope")
    assert isinstance(result, ToolResult) and result.is_error


def test_task_status_running_shows_progress():
    task = _make_task("a\nb")
    task.status = "running"
    task.blocks = ["a", "b"]
    task.block_index = 0
    task.current_block = "a"
    text = _text(stata_task_status(task.task_id))
    assert "running" in text and "1/2" in text


def test_task_result_done():
    task = _make_task("display 1")
    task.status = "done"
    task.result = "42"
    assert _text(stata_task_result(task.task_id)) == "42"


def test_task_result_running_tells_retry():
    task = _make_task("display 1")
    task.status = "running"
    text = _text(stata_task_result(task.task_id))
    assert "仍在运行" in text


def test_task_result_error():
    task = _make_task("use x")
    task.status = "failed"
    task.is_error = True
    task.result = "[返回码: 601] x"
    result = stata_task_result(task.task_id)
    assert isinstance(result, ToolResult) and result.is_error


def test_task_cancel_tool_unknown():
    result = stata_task_cancel("nope")
    assert isinstance(result, ToolResult) and result.is_error


def test_task_list_empty_and_populated():
    assert "没有" in _text(stata_task_list())
    task = _make_task("display 1")
    task.status = "running"
    task.blocks = ["display 1"]
    listing = _text(stata_task_list())
    assert task.task_id in listing and "running" in listing
