"""Tests for the core Stata execution engine (without a real Stata install)."""

from unittest.mock import MagicMock, patch

import pytest

from server import (
    _TRUNCATION_NOTICE,
    MAX_OUTPUT_CHARS,
    STATA_RC_NO_OUTPUT,
    _describe_empty_result,
    _execute_safe,
    _execute_single,
    _ping_stata,
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
        patch("server._execute_single", side_effect=side_effect),
        patch("server._drain_output"),
        patch("server._set_break"),
    ):
        rc, out = _execute_safe(" dangerous ")
    # The main command crashed and recovery ping failed.
    assert rc == 998
    assert "无法自动恢复" in out


def test_execute_safe_marks_recovered_when_recovery_succeeds():
    """After a DLL crash (rc=999) if recovery ping succeeds, rc becomes 997 (recovered).

    C3 修复：恢复成功不应保留 999（会被误报为致命「内部崩溃」），而应标记
    为 STATA_RC_RECOVERED(997)，使 _run_stata_command 视为非致命。
    """
    from server import STATA_RC_RECOVERED

    # Sequence: initial ping succeeds, main command crashes (999),
    # recovery ping succeeds with "42" -> True.
    side_effect = [(0, "42"), (999, "boom"), (0, "42")]
    with (
        patch("server._execute_single", side_effect=side_effect),
        patch("server._drain_output"),
        patch("server._set_break"),
        patch("time.sleep"),
    ):
        rc, out = _execute_safe(" dangerous ")
    assert rc == STATA_RC_RECOVERED
    assert "已自动恢复" in out


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


def test_run_stata_command_does_not_error_on_recovered_rc():
    """C3: rc=997（崩溃已恢复）不应被 _run_stata_command 标记为 isError 或显示「内部崩溃」。"""
    from server import STATA_RC_RECOVERED, _run_stata_command

    with patch(
        "server._execute_safe", return_value=(STATA_RC_RECOVERED, "(Stata 已自动恢复，请重试命令)")
    ):
        result = _run_stata_command("summarize mpg")
    text = result.content[0].text if hasattr(result, "content") else result
    assert getattr(result, "is_error", False) is False
    assert "内部崩溃" not in text
    assert "已自动恢复" in text


def test_run_stata_command_errors_on_unrecovered_999():
    """rc=999（崩溃未恢复）应标记为 isError 并显示「内部崩溃」。

    防御性测试：生产中 _execute_safe 总把 999 转为 997（恢复成功）或 998
    （恢复失败），不会向上返回 999。此处直接 mock 999 以确保 _run_stata_command
    对该码的「致命」判定不被意外削弱。
    """
    from server import _run_stata_command

    with patch("server._execute_safe", return_value=(999, "StataSO_Execute 崩溃: boom")):
        result = _run_stata_command("summarize mpg")
    text = result.content[0].text if hasattr(result, "content") else result
    assert getattr(result, "is_error", False) is True
    assert "内部崩溃" in text


def test_run_stata_command_breaks_chain_on_recovered_rc():
    """M1: 多命令链中某块返回 997（崩溃已恢复）应中止后续块，而非 continue。

    997 表示当前块未执行，后续块若继续会在陈旧状态运行。应 break 让用户
    整体重试整条命令链。
    """
    from server import STATA_RC_RECOVERED, _run_stata_command

    # 3 块：第 1 块成功，第 2 块崩溃已恢复(997)，第 3 块不应被执行
    side_effect = [
        (0, "block1 out"),
        (STATA_RC_RECOVERED, "(Stata 已自动恢复，请重试命令)"),
        (0, "block3 SHOULD NOT RUN"),
    ]
    with patch("server._execute_safe", side_effect=side_effect) as exec_safe:
        result = _run_stata_command("gen x=1\nuse data.dta\nsummarize x")
    text = result.content[0].text if hasattr(result, "content") else result
    # 第 3 块未执行
    assert "SHOULD NOT RUN" not in text
    # 仅调用 2 次（第 3 块未执行）
    assert exec_safe.call_count == 2
    # 非致命（997 不标记 isError）
    assert getattr(result, "is_error", False) is False
    assert "已自动恢复" in text


def test_run_stata_command_breaks_chain_on_ordinary_error():
    """普通 Stata 错误也应中止命令链 —— 与 Stata 自身的 do 文件语义一致。

    此前只有 997/998 会 break，r(601)/r(198) 与看门狗超时（break 后 rc=1）都只
    设 had_error 并继续。CLAUDE.md 推荐把「加载→清洗→回归→诊断」批量成一次
    stata_run，于是 ``use`` 因路径错返回 r(601) 后，后续的 ``collapse`` 会在
    **上一个**内存数据集上聚合、``save ... , replace`` 把错误数据覆盖到磁盘 ——
    整体虽标 isError，磁盘破坏已不可逆。
    """
    from server import _run_stata_command

    side_effect = [
        (601, "file not found"),
        (0, "collapse SHOULD NOT RUN"),
        (0, "save SHOULD NOT RUN"),
    ]
    with patch("server._execute_safe", side_effect=side_effect) as exec_safe:
        result = _run_stata_command(
            'use "missing.dta", clear\ncollapse (mean) x, by(g)\nsave "out.dta", replace'
        )
    text = result.content[0].text if hasattr(result, "content") else result
    assert "SHOULD NOT RUN" not in text
    assert exec_safe.call_count == 1
    assert getattr(result, "is_error", False) is True
    assert "601" in text
    # 提示要说明「跳过了什么」以及 Stata 原生的继续执行方式
    assert "已跳过" in text
    assert "capture" in text


def test_run_stata_command_capture_keeps_chain_running():
    """``capture`` 是 Stata 原生的「继续执行」逃生舱：它让 rc=0，链条自然继续。"""
    from server import _run_stata_command

    with patch("server._execute_safe", side_effect=[(0, "a"), (0, "b")]) as exec_safe:
        result = _run_stata_command('capture use "missing.dta"\nsummarize price')
    text = result.content[0].text if hasattr(result, "content") else result
    assert exec_safe.call_count == 2
    assert getattr(result, "is_error", False) is False
    assert "b" in text


def test_prepare_ssc_installs_probe_holds_lock():
    """``which`` 探测必须在 ``_stata_lock`` 内 —— Stata DLL 非线程安全。

    文件里其余全部 DLL 访问都在锁内（_run_stata_command、estout 探测、
    stata_ping）。探测裸奔会与并发工具调用竞态：轻则 _drain_output 抢走对方
    输出，重则复现「已修复的崩溃历史」里那条 DLL 竞态崩溃。
    """
    import server

    held = []

    def fake_execute_safe(cmd, timeout=None):
        held.append(server._stata_lock.locked())
        return (0, "")

    with patch("server._execute_safe", side_effect=fake_execute_safe):
        server._prepare_ssc_installs([("estout", False)], timeout=30)

    assert held == [True]


def test_prepare_ssc_installs_aborts_when_dll_dead():
    """探测返回 998（DLL 无响应）时不得当成「未安装」继续逐个联网安装。"""
    import server

    with patch("server._execute_safe", return_value=(998, "Stata 无响应")):
        with patch("server._run_stata_command") as run_cmd:
            report = server._prepare_ssc_installs(
                [("estout", False), ("outreg2", False)], timeout=30
            )

    run_cmd.assert_not_called()
    assert any("无响应" in line or "中止" in line for line in report)


def test_prepare_ssc_installs_does_not_misreport_recovered_command():
    """997 的 install 命令未执行，不能被报告成「已安装」。"""
    import server

    recovered = "StataSO_Execute 崩溃: boom\n(Stata 已自动恢复，请重试命令)"
    with patch("server._execute_safe", return_value=(111, "not found")), \
         patch("server._run_stata_command", return_value=recovered):
        report = server._prepare_ssc_installs([("estout", False)], timeout=30)

    text = "\n".join(report)
    assert "安装未完成" in text
    assert "已安装" not in text
    assert "已自动恢复" in text


def test_execute_single_collects_consecutive_chunks():
    """L3/P1: 阶段 1 取到输出后应立即复取（continue），连续多块输出都收集。

    验证 P1 优化的 continue 复取不会漏收紧随其后的第二块输出。
    """
    from server import _execute_single

    def fake_redirect_ctx():
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=None)
        ctx.__exit__ = MagicMock(return_value=None)
        return ctx

    # chunk1 → chunk2（紧随其后）→ 3×None(clean_exit)
    seq = ["chunk1\n", "chunk2\n", None, None, None, None]
    seq_iter = iter(seq)
    with (
        patch("server.stout.RedirectOutput", return_value=fake_redirect_ctx()),
        patch("server.config.get_encode_str", return_value=b"x"),
        patch("server.config.stlib.StataSO_Execute", return_value=0),
        patch("server._drain_output", return_value=""),
        patch("server.config.get_output", side_effect=lambda: next(seq_iter, None)),
    ):
        rc, out = _execute_single("display 42")
    assert rc == 0
    assert "chunk1" in out
    assert "chunk2" in out, "连续的第二块输出不应因 continue 复取漏收"


# --- 空输出的原因解释 --------------------------------------------------------
# 内存中没有数据集时，summarize / tabulate 既不报错也不输出；
# 笼统回「执行成功，无文本输出」会让调用方去排查命令本身，而真因是没载入数据。


def test_describe_empty_result_explains_missing_dataset():
    with patch("server._execute_single", return_value=(0, "0")):
        msg = _describe_empty_result()
    assert "没有数据集" in msg
    assert "stata_use_dataset" in msg, "应给出可直接执行的下一步"


def test_describe_empty_result_stays_generic_when_data_loaded():
    """有数据却无输出（如 quietly 命令）时不应误报没有数据。"""
    with patch("server._execute_single", return_value=(0, "74")):
        assert _describe_empty_result() == "(命令执行成功，无文本输出)"


def test_describe_empty_result_accepts_no_output_rc():
    with patch("server._execute_single", return_value=(STATA_RC_NO_OUTPUT, "0")):
        assert "没有数据集" in _describe_empty_result()


def test_describe_empty_result_survives_probe_failure():
    """探测本身失败时退回通用文案，不能让辅助逻辑吃掉主命令的结果。"""
    with patch("server._execute_single", side_effect=RuntimeError("boom")):
        assert _describe_empty_result() == "(命令执行成功，无文本输出)"


def test_describe_empty_result_ignores_probe_error_rc():
    with patch("server._execute_single", return_value=(198, "")):
        assert _describe_empty_result() == "(命令执行成功，无文本输出)"


# --- 输出硬上限 --------------------------------------------------------------
# 上限一度形同虚设：旧实现先整块 write 再判断总长，只能停止继续收集，拦不住
# 已进入缓冲的部分。实测 19980 obs 的 list 单次返回 1,270,888 字符（超限 10.6 倍），
# 而 _last_output / stata_more(page=0) 会把这一整坨原样交给 MCP 客户端。


def test_execute_single_truncates_single_oversized_chunk(exec_mocks):
    """单次 get_output 就超上限时，必须在写入时裁剪，而不是只停止收集。"""
    huge = "x" * (MAX_OUTPUT_CHARS * 3)
    exec_mocks["execute"].return_value = 0
    exec_mocks["get_output"].side_effect = [huge, None, None, None]
    _rc, out = _execute_single("list")
    assert len(out) <= MAX_OUTPUT_CHARS + len(_TRUNCATION_NOTICE)
    assert "输出已截断" in out
    assert out.count("x") == MAX_OUTPUT_CHARS


def test_execute_single_truncates_accumulated_chunks(exec_mocks):
    """多次小块累积到上限时同样在边界处裁剪。"""
    chunk = "y" * (MAX_OUTPUT_CHARS // 2)
    exec_mocks["execute"].return_value = 0
    exec_mocks["get_output"].side_effect = [chunk, chunk, chunk, None, None, None]
    _rc, out = _execute_single("list")
    assert out.count("y") == MAX_OUTPUT_CHARS
    assert "输出已截断" in out


def test_execute_single_truncates_drain_tail(exec_mocks):
    """阶段 2 清尾拿回的残留同样受上限约束。"""
    exec_mocks["execute"].return_value = 0
    exec_mocks["get_output"].side_effect = ["head\n", None, None, None]
    exec_mocks["drain"].return_value = "z" * (MAX_OUTPUT_CHARS * 2)
    _rc, out = _execute_single("list")
    assert len(out) <= MAX_OUTPUT_CHARS + len(_TRUNCATION_NOTICE)
    assert "输出已截断" in out


def test_truncation_notice_is_actionable():
    """只说「已截断」会让调用方反复翻页找不存在的后半段。"""
    assert "缩小范围" in _TRUNCATION_NOTICE
    assert str(MAX_OUTPUT_CHARS) in _TRUNCATION_NOTICE


def test_execute_single_under_limit_has_no_notice(exec_mocks):
    exec_mocks["execute"].return_value = 0
    exec_mocks["get_output"].side_effect = ["short output", None, None, None]
    _rc, out = _execute_single("display 1")
    assert "输出已截断" not in out


# --- 超时提示 ----------------------------------------------------------------


def test_execute_single_timeout_message_names_the_timeout(exec_mocks):
    """看门狗 break 后 Stata 只给通用 rc=1，单看返回码会让人去查语法。"""
    import time

    def slow(*_a, **_kw):
        time.sleep(0.3)
        return 1

    exec_mocks["execute"].side_effect = slow
    _rc, out = _execute_single("forvalues i=1/1e9 {", timeout=0)
    assert "超过 0s 上限已被中断" in out
    assert "timeout" in out


def test_extract_ssc_installs_skips_block_interior():
    """`{ }` 块内的 ssc install 不得被拆出预装 —— 它是**有条件**执行的。

    docstring 与 CLAUDE.md 都声称块内安装「不特殊处理，仍随块内联执行」，但
    此前代码没有任何块跟踪：``if _rc != 0 { ssc install foo }`` 会被提到脚本
    之前**无条件**安装，改变了 do 文件的语义。文档描述的才是安全行为。
    """
    from server import _extract_ssc_installs

    text = 'sysuse auto, clear\nif 1 {\n    ssc install estout\n}\nsummarize price'
    cleaned, installs = _extract_ssc_installs(text)
    assert installs == []
    assert cleaned == text


def test_extract_ssc_installs_still_hoists_top_level():
    from server import _extract_ssc_installs

    text = "ssc install estout\nqui ssc install fre, replace\nsysuse auto, clear"
    cleaned, installs = _extract_ssc_installs(text)
    assert installs == [("estout", False), ("fre", True)]
    assert "已移出单独安装" in cleaned
    assert cleaned.count("\n") == text.count("\n")


def test_extract_ssc_installs_resumes_after_block_closes():
    from server import _extract_ssc_installs

    text = "forvalues i=1/2 {\n    display `i'\n}\nssc install estout"
    _, installs = _extract_ssc_installs(text)
    assert installs == [("estout", False)]


# ---------------------------------------------------------------------------
# 不受控第三方包安装拦截（net/github install、adoupdate、update all）
# ---------------------------------------------------------------------------


def test_flag_unmanaged_package_commands_detects_variants():
    from server import _flag_unmanaged_package_commands

    text = (
        "sysuse auto, clear\n"
        "net install foo, from(https://example.com)\n"
        "qui github install bar\n"
        "cap adoupdate, update\n"
        "update all\n"
        "ssc install estout\n"  # ssc 是受控预装路径，不拦
    )
    blocked = _flag_unmanaged_package_commands(text)
    assert blocked == [
        "net install foo, from(https://example.com)",
        "qui github install bar",
        "cap adoupdate, update",
        "update all",
    ]


def test_flag_unmanaged_package_ignores_ssc_and_normal():
    from server import _flag_unmanaged_package_commands

    assert _flag_unmanaged_package_commands("ssc install estout\nsummarize price") == []


def test_run_do_file_rejects_unmanaged_package_install(tmp_path):
    """do 文件含 net/github install → 拒绝执行并重定向到受控工具。"""
    from unittest.mock import patch

    from fastmcp.tools.base import ToolResult

    from server import stata_run_do_file

    do_file = tmp_path / "bad.do"
    do_file.write_text(
        'net install foo, from("https://example.com/x")\nsummarize price\n',
        encoding="utf-8",
    )
    with patch("server._run_stata_command") as mock_run:
        result = stata_run_do_file(str(do_file))
    assert isinstance(result, ToolResult) and result.is_error
    text = result.content[0].text
    assert "不受控的包安装" in text
    assert "stata_install_package" in text
    mock_run.assert_not_called()  # 未执行任何命令


def test_run_do_file_allows_ssc_install(tmp_path):
    """ssc install 走受控预装路径，不被拦截。"""
    from unittest.mock import patch

    from server import stata_run_do_file

    do_file = tmp_path / "ok.do"
    do_file.write_text("ssc install estout\nsummarize price\n", encoding="utf-8")
    with patch("server._prepare_ssc_installs", return_value=["已安装 estout"]):
        with patch("server._run_stata_command") as mock_run:
            mock_run.return_value = "ok"
            result = stata_run_do_file(str(do_file))
    assert not getattr(result, "is_error", False)
    mock_run.assert_called()


def test_watchdog_does_not_break_after_command_completes(exec_mocks):
    """命令完成后不得再发 break —— 晚到的 break 会打断下一条无辜命令。

    主线程在 StataSO_Execute 返回后，还要走完 RedirectOutput.__exit__ 与临时
    文件清理（多行块时含一次磁盘 unlink）才置位事件；看门狗恰在这段间隙做二次
    确认时仍读到「未完成」，于是 break 落在命令结束之后。它不会被任何代码消费，
    而是被下一次 StataSO_Execute 吃掉，表现为一条无关命令的 rc=1「已中断」。

    本测试让**清理**慢于 timeout：事件若在清理前置位就不会 break，置位在清理
    之后则必然 break。
    """
    import time

    exec_mocks["execute"].return_value = 0
    with (
        patch("server._cleanup_temp_block", side_effect=lambda p: time.sleep(0.3)),
        patch("server._set_break") as set_break,
    ):
        _execute_single("display 42", timeout=0.05)

    set_break.assert_not_called()


def test_watchdog_timeout_note_reaches_caller(exec_mocks):
    """真超时时，did_break 必须对主线程可见 —— 否则超时说明整条丢失。

    `did_break = True` 此前写在 `_set_break()` **之后**，主线程可能先读到
    False，于是既不清 break 残渣也不追加超时说明，调用方只看到一个通用 rc=1。
    """
    import time

    def slow_execute(*args, **kwargs):
        time.sleep(0.3)
        return 1

    exec_mocks["execute"].side_effect = slow_execute
    with patch("server._set_break") as set_break:
        _rc, out = _execute_single("sleep", timeout=0.05)

    set_break.assert_called()
    assert "已被中断" in out
    assert "0.05s" in out
    assert "timeout" in out


def test_run_do_file_rejects_dangerous_command(tmp_path):
    """do 文件内容含 shell-out 必须被拒（真机确认此前可执行主机命令）。"""
    from unittest.mock import patch

    from fastmcp.tools.base import ToolResult

    from server import stata_run_do_file

    do_file = tmp_path / "evil.do"
    do_file.write_text("display 1\nshell whoami\ndisplay 2\n", encoding="utf-8")
    with patch("server._run_stata_command") as mock_run:
        result = stata_run_do_file(str(do_file))
    assert isinstance(result, ToolResult) and result.is_error
    assert "危险命令" in result.content[0].text
    mock_run.assert_not_called()


def test_run_do_file_rejects_macro_obfuscation(tmp_path):
    """do 文件含宏间接调用危险命令必须被拒。"""
    from unittest.mock import patch

    from fastmcp.tools.base import ToolResult

    from server import stata_run_do_file

    do_file = tmp_path / "macro.do"
    do_file.write_text('local c "shell whoami"\n`c\'\n', encoding="utf-8")
    with patch("server._run_stata_command") as mock_run:
        result = stata_run_do_file(str(do_file))
    assert isinstance(result, ToolResult) and result.is_error
    assert "宏间接" in result.content[0].text
    mock_run.assert_not_called()


def test_run_do_file_rejects_comment_obfuscation(tmp_path):
    """do 文件 `sh/*x*/ell` 注释混淆必须被解析后护栏拦截。"""
    from unittest.mock import patch

    from fastmcp.tools.base import ToolResult

    from server import stata_run_do_file

    do_file = tmp_path / "obfus.do"
    do_file.write_text("display 1\nsh/*x*/ell echo hi\ndisplay 2\n", encoding="utf-8")
    with patch("server._run_stata_command") as mock_run:
        result = stata_run_do_file(str(do_file))
    assert isinstance(result, ToolResult) and result.is_error
    assert "危险命令" in result.content[0].text
    mock_run.assert_not_called()


def test_run_do_file_rejects_macro_equals_form(tmp_path):
    from unittest.mock import patch

    from fastmcp.tools.base import ToolResult

    from server import stata_run_do_file

    do_file = tmp_path / "m2.do"
    do_file.write_text('local c = "shell whoami"\n`c\'\n', encoding="utf-8")
    with patch("server._run_stata_command") as mock_run:
        result = stata_run_do_file(str(do_file))
    assert isinstance(result, ToolResult) and result.is_error
    assert "宏间接" in result.content[0].text
    mock_run.assert_not_called()
