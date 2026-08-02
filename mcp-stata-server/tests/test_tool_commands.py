from unittest.mock import patch

import pytest
from conftest import abs_path
from fastmcp.tools.base import ToolResult

from server import (
    _ESTOUT_PROBE_CMD,
    _HELP_TOPIC_RE,
    _make_error_result,
    _normalize_path,
    stata_codebook,
    stata_correlate,
    stata_describe,
    stata_describe_package,
    stata_egen,
    stata_export_excel,
    stata_find_package,
    stata_generate,
    stata_graph,
    stata_help,
    stata_install_package,
    stata_ivregress,
    stata_list,
    stata_logistic,
    stata_margins,
    stata_ping,
    stata_poisson,
    stata_predict,
    stata_probit,
    stata_regress,
    stata_run,
    stata_save_dataset,
    stata_summarize,
    stata_tabulate,
    stata_test,
    stata_ttest,
    stata_uninstall_package,
    stata_use_dataset,
    stata_xtreg,
)


def _result_text(result):
    """统一提取 str / ToolResult 的文本内容。"""
    if isinstance(result, ToolResult):
        return result.content[0].text
    return result


def test_regress_accepts_factor_and_timeseries_varlist():
    with patch("server._run_stata_command") as mock_run:
        result = stata_regress("y", "i.group x1 L.x2 c.price##i.foreign [aw=weight]")
        assert "错误" not in result
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "i.group" in cmd
        assert "L.x2" in cmd
        assert "[aw=weight]" in cmd


def test_regress_builds_command_with_condition_and_options():
    with patch("server._run_stata_command") as mock_run:
        stata_regress("price", "mpg weight", condition="foreign == 1", options="robust")
        cmd = mock_run.call_args[0][0]
        assert cmd == "regress price mpg weight if foreign == 1, robust"


def test_logistic_omits_condition_when_empty():
    with patch("server._run_stata_command") as mock_run:
        stata_logistic("foreign", "mpg weight", options="or")
        cmd = mock_run.call_args[0][0]
        assert cmd == "logistic foreign mpg weight, or"


def test_ttest_with_byvar_and_condition():
    with patch("server._run_stata_command") as mock_run:
        stata_ttest("price", byvar="foreign", condition="!missing(price)", options="unequal")
        cmd = mock_run.call_args[0][0]
        assert cmd == "ttest price if !missing(price), by(foreign) unequal"


def test_ttest_one_sample_with_options():
    """单样本形式必须带 `== #` —— 实测裸 `ttest price, level(90)` 会 r(100)。"""
    with patch("server._run_stata_command") as mock_run:
        stata_ttest("price", compare_to="5000", options="level(90)")
        cmd = mock_run.call_args[0][0]
        assert cmd == "ttest price == 5000, level(90)"


def test_summarize_with_condition_and_detail():
    with patch("server._run_stata_command") as mock_run:
        stata_summarize("price", condition="foreign == 1", detail=True)
        cmd = mock_run.call_args[0][0]
        assert cmd == "summarize price if foreign == 1, detail"


def test_list_with_condition_and_in_range():
    with patch("server._run_stata_command") as mock_run:
        stata_list("mpg price", condition="foreign == 1", in_range="1/20")
        cmd = mock_run.call_args[0][0]
        assert "list mpg price" in cmd
        assert "if foreign == 1" in cmd
        assert "in 1/20" in cmd


def test_codebook_with_condition():
    with patch("server._run_stata_command") as mock_run:
        stata_codebook("price", condition="foreign == 1", compact=True)
        cmd = mock_run.call_args[0][0]
        assert cmd == "codebook price if foreign == 1, compact"


def test_tabulate_with_byvar_condition_and_chi2():
    with patch("server._run_stata_command") as mock_run:
        stata_tabulate("rep78", byvar="foreign", condition="!missing(rep78)", chi2=True)
        cmd = mock_run.call_args[0][0]
        assert cmd == "tabulate rep78 foreign if !missing(rep78), chi2"


def test_tabulate_without_byvar():
    with patch("server._run_stata_command") as mock_run:
        stata_tabulate("foreign")
        cmd = mock_run.call_args[0][0]
        assert cmd == "tabulate foreign"


def test_install_package_ssc():
    with patch("server._run_stata_command") as mock_run:
        stata_install_package("outreg2", source="ssc", replace=True)
        cmd = mock_run.call_args[0][0]
        assert cmd == "ssc install outreg2, replace"


def test_install_package_requires_name():
    with patch("server._run_stata_command") as mock_run:
        result = stata_install_package("")
    assert getattr(result, "is_error", False)
    assert "不能为空" in _result_text(result)
    mock_run.assert_not_called()


def test_install_package_net_url():
    with patch("server._run_stata_command") as mock_run:
        stata_install_package("somepkg", source="https://example.com/pkg")
        cmd = mock_run.call_args[0][0]
        assert cmd == "net install somepkg, from(https://example.com/pkg)"


def test_install_package_default_timeout():
    with patch("server._run_stata_command") as mock_run:
        stata_install_package("estout", source="ssc")
        assert mock_run.call_args.kwargs.get("timeout") == 300


def test_install_package_custom_timeout():
    """用户可自定义安装超时；实测超时会被看门狗干净中断，不卡死。"""
    with patch("server._run_stata_command") as mock_run:
        stata_install_package("estout", source="ssc", timeout=120)
        assert mock_run.call_args.kwargs.get("timeout") == 120


# 以下 estout 探测返回值取自 Stata 19.5 MP 实测（见 _ESTOUT_PROBE_CMD 注释）：
# 已装 `which estout` → rc=0 + ado 路径；未装 → rc=111 + not found 文本。
_PROBE_INSTALLED = (0, "/Users/x/Library/Application Support/Stata/ado/plus/e/estout.ado")
_PROBE_MISSING = (111, "command estout not found as either built-in or ado-file")


def test_export_excel_results_probe_must_not_use_capture():
    """探测必须用裸 which：capture 会吞掉错误使 rc 恒为 0，探测静默失效。

    这条断言防止「正确修复被无声回退」——若有人加回 capture，功能测试
    仍会全绿（mock 的 rc 是手工给的），只有这里会失败。
    """
    assert _ESTOUT_PROBE_CMD == "which estout"
    assert "capture" not in _ESTOUT_PROBE_CMD


def test_export_excel_results_forces_csv():
    with (
        patch("server._run_stata_command") as mock_run,
        patch("server._execute_safe", return_value=_PROBE_INSTALLED) as mock_probe,
        patch("server.os.path.isfile", return_value=True),
        patch("server.os.path.getsize", return_value=1024),
    ):
        result = stata_export_excel(abs_path("output", "results.xlsx"), results=True)
        assert mock_probe.call_args[0][0] == _ESTOUT_PROBE_CMD
        cmd = mock_run.call_args[0][0]
        assert f'esttab using "{abs_path("output", "results.csv")}"' in cmd
        assert ", csv " in cmd
        assert "提示：" in result


def test_export_excel_results_aborts_when_estout_missing():
    """estout 未安装时不自动装，而给出可恢复的单条安装指令（含 timeout）+ 重试提示。"""
    with (
        patch("server._run_stata_command") as mock_run,
        patch("server._execute_safe", return_value=_PROBE_MISSING) as mock_probe,
    ):
        result = stata_export_excel(abs_path("output", "results.csv"), results=True)
        assert mock_probe.call_args[0][0] == _ESTOUT_PROBE_CMD
        text = _result_text(result)
        assert getattr(result, "is_error", False)
        assert "未安装 estout" in text
        # 可执行的单条安装命令 + 显式 timeout + 装后重试的指引
        assert 'stata_install_package("estout", source="ssc", timeout=120)' in text
        assert "重试" in text
        mock_run.assert_not_called()


def test_export_excel_results_aborts_on_dll_dead():
    """DLL 无响应（rc=998）时透传原始诊断，不得误报为「未安装 estout」。

    误报会让用户去装包，而错过真正需要的「重启 MCP Server」恢复步骤。
    """
    diag = "[错误] Stata DLL 无响应\n建议: 重启 MCP Server"
    with (
        patch("server._run_stata_command") as mock_run,
        patch("server._execute_safe", return_value=(998, diag)),
    ):
        result = stata_export_excel(abs_path("output", "results.csv"), results=True)
        text = _result_text(result)
        assert getattr(result, "is_error", False)
        assert "重启 MCP Server" in text
        assert "未安装 estout" not in text
        mock_run.assert_not_called()


def test_export_excel_results_recovered_is_non_fatal():
    """rc=997（崩溃已恢复）按契约为非致命：提示重试，不标 isError、不报未安装。"""
    recovered = "StataSO_Execute 崩溃: boom\n(Stata 已自动恢复，请重试命令)"
    with (
        patch("server._run_stata_command") as mock_run,
        patch("server._execute_safe", return_value=(997, recovered)),
    ):
        result = stata_export_excel(abs_path("output", "results.csv"), results=True)
        text = _result_text(result)
        assert not getattr(result, "is_error", False)
        assert "请重试命令" in text
        assert "未安装 estout" not in text
        mock_run.assert_not_called()


def test_export_excel_results_probe_runs_under_stata_lock():
    """探测必须在 _stata_lock 内执行：DLL 非线程安全，且 drain 会抢并发命令的输出。"""
    import server

    seen = {}

    def _probe(cmd, timeout=60):
        seen["locked"] = server._stata_lock.locked()
        return _PROBE_INSTALLED

    with (
        patch("server._run_stata_command"),
        patch("server._execute_safe", side_effect=_probe),
        patch("server.os.path.isfile", return_value=True),
        patch("server.os.path.getsize", return_value=1024),
    ):
        stata_export_excel("C:/output/results.xlsx", results=True)

    assert seen["locked"] is True


def test_export_excel_dataset_uses_excel():
    with (
        patch("server._run_stata_command") as mock_run,
        patch("server.os.path.isfile", return_value=True),
        patch("server.os.path.getsize", return_value=1024),
    ):
        target = abs_path("output", "data.xlsx")
        stata_export_excel(target, varlist="mpg price", sheet="Data", replace=True)
        cmd = mock_run.call_args[0][0]
        assert cmd == (
            f'export excel mpg price using "{target}", '
            'replace firstrow(variables) sheet("Data")'
        )


def test_graph_export_includes_height_when_set():
    with (
        patch("server._run_stata_command") as mock_run,
        patch("server.os.path.isfile", return_value=True),
        patch("server.os.path.getsize", return_value=1024),
    ):
        from server import stata_graph

        target = abs_path("output", "fig.png")
        stata_graph("scatter price weight", export=target, width=1200, height=800)
        cmd = mock_run.call_args[0][0]
        assert f'graph export "{target}"' in cmd
        assert "width(1200)" in cmd
        assert "height(800)" in cmd


def test_graph_rejects_unsafe_brace_in_export_mode():
    with patch("server._run_stata_command") as mock_run:
        from server import stata_graph

        result = stata_graph(
            "twoway scatter price weight }",  # 字符串外的 } 会破坏复合块
            export="C:/output/fig.png",
        )
        if isinstance(result, ToolResult):
            assert result.is_error is True
            assert "会破坏复合块的" in result.content[0].text
        else:
            assert "错误" in result
            assert "会破坏复合块的" in result
        mock_run.assert_not_called()


def test_graph_allows_brace_inside_string():
    with (
        patch("server._run_stata_command") as mock_run,
        patch("server.os.path.isfile", return_value=True),
        patch("server.os.path.getsize", return_value=1024),
    ):
        from server import stata_graph

        stata_graph(
            'twoway scatter price weight, title("a} b")',
            export=abs_path("output", "fig.png"),
        )
        cmd = mock_run.call_args[0][0]
        assert "capture noisily {" in cmd


def test_graph_rejects_injected_scheme():
    with patch("server._run_stata_command") as mock_run:
        result = stata_graph("scatter price weight", scheme="s2color; shell evil")
        assert "错误" in _result_text(result)
        mock_run.assert_not_called()


def test_graph_accepts_valid_scheme():
    with patch("server._run_stata_command") as mock_run:
        stata_graph("scatter price weight", scheme="economist")
        cmd = mock_run.call_args[0][0]
        assert "set scheme economist" in cmd


def test_graph_accepts_numeric_and_hyphenated_schemes():
    """M2: scheme 名允许数字开头（如 538）与连字符（如 s1color-asterisk）。"""
    for ok_scheme in ["538", "s1color-asterisk", "s2color", "cleanplots"]:
        with patch("server._run_stata_command") as mock_run:
            stata_graph("scatter price weight", scheme=ok_scheme)
            cmd = mock_run.call_args[0][0]
            assert f"set scheme {ok_scheme}" in cmd, f"应放行 scheme: {ok_scheme}"


def test_save_dataset_rejects_path_with_illegal_chars():
    with patch("server._run_stata_command") as mock_run:
        result = stata_save_dataset("C:/out;evil.dta")
        assert "错误" in _result_text(result)
        mock_run.assert_not_called()


def test_save_dataset_builds_command_for_valid_path():
    with patch("server._run_stata_command") as mock_run:
        target = abs_path("output", "data.dta")
        stata_save_dataset(target, replace=True)
        cmd = mock_run.call_args[0][0]
        assert cmd == f'save "{target}", replace'


def test_install_package_rejects_source_with_closing_paren():
    """C4: source 含 ) 可提前闭合 from() 注入 net install 参数，应被拦截。"""
    from server import stata_install_package

    for injected in [
        "https://evil.com) net install bad",
        "https://evil.com/, from(bad)",
        'https://evil.com"',
        "https://x.com/path;more",
        "https://x.com/path with space",
    ]:
        with patch("server._run_stata_command") as mock_run:
            result = stata_install_package("pkg", source=injected)
            assert "错误" in _result_text(result), f"应拦截 source: {injected}"
            mock_run.assert_not_called()


def test_install_package_accepts_valid_sources():
    """C4: 合法 ssc 与 HTTPS URL 仍应放行。"""
    from server import stata_install_package

    for ok_source, expect_cmd_part in [
        ("ssc", "ssc install outreg2"),
        ("https://fmwww.bc.edu/RePEc/bocode/o", "net install outreg2, from("),
    ]:
        with patch("server._run_stata_command") as mock_run:
            stata_install_package("outreg2", source=ok_source)
            cmd = mock_run.call_args[0][0]
            assert expect_cmd_part in cmd


def test_export_excel_rejects_sheet_with_injection_chars():
    """C5: sheet 含破坏引号语法/注入的字符（双引号、分号、换行）应被拦截。

    注意：) 在引号包裹 sheet("...") 内是安全的，故合法工作表名如
    "Q1 (2024)" 应放行（见 test_export_excel_allows_parens_in_sheet）。
    """
    for injected in ['foo"bar', "a;b", "a\nb"]:
        with patch("server._run_stata_command") as mock_run:
            result = stata_export_excel("C:/o.xlsx", sheet=injected)
            assert "错误" in _result_text(result), f"应拦截 sheet: {injected}"
            mock_run.assert_not_called()


def test_export_excel_allows_parens_in_sheet():
    """C5: 引号包裹后，含括号的合法工作表名应放行。"""
    with (
        patch("server._run_stata_command") as mock_run,
        patch("server.os.path.isfile", return_value=True),
        patch("server.os.path.getsize", return_value=1024),
    ):
        stata_export_excel("C:/o.xlsx", sheet="Q1 (2024)")
        cmd = mock_run.call_args[0][0]
        assert 'sheet("Q1 (2024)")' in cmd


def test_export_excel_wraps_sheet_in_quotes():
    """C5: sheet 名应用双引号包裹，允许含空格/中文的合法名。"""
    with (
        patch("server._run_stata_command") as mock_run,
        patch("server.os.path.isfile", return_value=True),
        patch("server.os.path.getsize", return_value=1024),
    ):
        stata_export_excel("C:/o.xlsx", sheet="My Sheet")
        cmd = mock_run.call_args[0][0]
        assert 'sheet("My Sheet")' in cmd


def test_stata_run_clamps_timeout_to_range():
    """safe_timeout 应钳位到 [10, 1800]。"""
    with patch("server._run_stata_command") as mock_run:
        stata_run("summarize mpg", timeout=5)
        assert mock_run.call_args.kwargs["timeout"] == 10
    with patch("server._run_stata_command") as mock_run:
        stata_run("summarize mpg", timeout=9999)
        assert mock_run.call_args.kwargs["timeout"] == 1800


def test_stata_run_rejects_null_byte():
    with patch("server._run_stata_command") as mock_run:
        result = stata_run("ok\x00bad")
        assert getattr(result, "is_error", False) or "错误" in _result_text(result)
        mock_run.assert_not_called()


def test_stata_use_dataset_builds_command_with_clear():
    with patch("server._run_stata_command") as mock_run:
        target = abs_path("data", "auto.dta")
        stata_use_dataset(target, clear=True)
        cmd = mock_run.call_args[0][0]
        assert cmd == f'use "{target}", clear'
        assert mock_run.call_args.kwargs["require_file"] == target


def test_stata_use_dataset_without_clear():
    with patch("server._run_stata_command") as mock_run:
        target = abs_path("data", "auto.dta")
        stata_use_dataset(target, clear=False)
        cmd = mock_run.call_args[0][0]
        assert cmd == f'use "{target}"'


def test_stata_ping_alive_when_42_in_output():
    with patch("server._execute_single", return_value=(0, "42")):
        result = stata_ping()
    assert "alive" in result
    assert not getattr(result, "is_error", False)


def test_stata_ping_writes_cache_on_success():
    """回写 ping 缓存曾因漏写 global 而写进函数局部变量，优化从未生效。"""
    import server

    server._last_ping_time = 0.0
    with patch("server._execute_single", return_value=(0, "42")):
        stata_ping()
    assert server._last_ping_time > 0, "成功心跳必须回写模块级缓存"


def test_stata_ping_reports_error_when_dll_unresponsive():
    """DLL 不可用时必须标 isError：以 'pong' 开头的普通串会让调用方以为一切正常。"""
    with patch("server._execute_single", return_value=(999, "StataSO_Execute 崩溃: boom")):
        result = stata_ping()
    text = _result_text(result)
    assert getattr(result, "is_error", False)
    assert not text.startswith("pong")
    assert "崩溃" in text, "必须带出探测的原始诊断，而不是丢弃"


def test_stata_ping_clears_cache_on_failure():
    """失败时必须清空缓存，否则后续 _execute_safe 会跳过心跳继续打死掉的 DLL。"""
    import server

    server._last_ping_time = 1.0
    with patch("server._execute_single", return_value=(0, "")):
        stata_ping()
    assert server._last_ping_time == 0.0


def test_stata_ping_returns_error_on_exception():
    with patch("server._execute_single", side_effect=RuntimeError("boom")):
        result = stata_ping()
    assert getattr(result, "is_error", False)


# --- 图形导出失败必须可见 ----------------------------------------------------
# 复合块用 capture noisily 包裹，Stata 的 rc 恒为 0；若只看 rc，导出失败会被
# 报告为成功（实测：PDF 传像素 width、变量名写错、拒绝覆盖，三者全部静默）。


def test_graph_export_reports_failure_when_file_not_created():
    with (
        patch(
            "server._run_stata_command",
            return_value="variable nonexistent not found\nr(111);",
        ),
        patch("server.os.path.isfile", return_value=False),
    ):
        result = stata_graph(
            "twoway scatter nonexistent weight",
            export=abs_path("out", "fig.png"),
            replace=True,
        )
    text = _result_text(result)
    assert getattr(result, "is_error", False)
    assert "未生成文件" in text
    assert "r(111)" in text, "应保留 Stata 原始错误，便于定位"


def test_graph_export_detects_refused_overwrite(tmp_path):
    """replace=False 且目标已存在时 Stata 拒绝写入，文件却仍在——不能算成功。"""
    target = tmp_path / "fig.png"
    target.write_bytes(b"old content")
    with patch(
        "server._run_stata_command",
        return_value="file fig.png already exists\nr(602);",
    ):
        result = stata_graph(
            "twoway scatter price weight", export=str(target), replace=False
        )
    text = _result_text(result)
    assert getattr(result, "is_error", False)
    assert "replace=True" in text, "应给出可操作的下一步"


def test_graph_export_succeeds_when_file_freshly_written(tmp_path):
    target = tmp_path / "fig.png"

    def _write_file(*_a, **_kw):
        target.write_bytes(b"png data")
        return "file written in PNG format"

    with patch("server._run_stata_command", side_effect=_write_file):
        result = stata_graph(
            "twoway scatter price weight", export=str(target), replace=True
        )
    assert not getattr(result, "is_error", False)
    assert "图形已导出" in _result_text(result)


def test_graph_export_rejects_zero_byte_file(tmp_path):
    """capture 会吞掉图形转换器错误，0 字节产物不能算导出成功。"""
    target = tmp_path / "empty.png"

    def _write_empty(*_a, **_kw):
        target.write_bytes(b"")
        return "graph export translator failed\nr(5100);"

    with patch("server._run_stata_command", side_effect=_write_empty):
        result = stata_graph(
            "twoway scatter price weight", export=str(target), replace=True
        )
    assert getattr(result, "is_error", False)
    text = _result_text(result)
    assert "文件为空" in text
    assert "图形已导出" not in text


def test_graph_pdf_export_notes_dropped_pixel_size(tmp_path):
    target = tmp_path / "fig.pdf"

    def _write_file(*_a, **_kw):
        target.write_bytes(b"pdf data")
        return "saved as PDF format"

    with patch("server._run_stata_command", side_effect=_write_file) as mock_run:
        result = stata_graph(
            "twoway scatter price weight", export=str(target), replace=True
        )
    cmd = mock_run.call_args[0][0]
    assert "width(800)" not in cmd, "矢量格式不能收到像素宽度，否则 r(198)"
    assert "英寸" in _result_text(result), "参数被丢弃必须告知调用方"


# --- 包搜索 ------------------------------------------------------------------


def test_find_package_uses_net_search():
    """ssc 没有 search 子命令，实测报 r(198) invalid subcommand。"""
    with patch("server._run_stata_command") as mock_run:
        stata_find_package("binscatter")
        assert mock_run.call_args[0][0] == "net search binscatter"


def test_find_package_rejects_blank_keyword():
    with patch("server._run_stata_command") as mock_run:
        result = stata_find_package("   ")
        assert getattr(result, "is_error", False)
        mock_run.assert_not_called()


# --- 会话状态查询不得有副作用 ------------------------------------------------


def test_status_uses_pwd_query_not_bare_cd():
    """裸 cd 会切换到 home 并打印新目录，看似查询实为修改。

    该工具标注 readOnlyHint=True，用裸 cd 会悄悄重置用户 set_cwd 的结果，
    使后续相对路径全部指向 home。
    """
    from server import stata_status

    with patch("server._run_stata_command") as mock_run:
        stata_status()
        cmd = mock_run.call_args[0][0]
    lines = [ln.strip() for ln in cmd.split("\n")]
    assert "display c(pwd)" in lines
    assert "cd" not in lines, "裸 cd 会修改工作目录，不能出现在只读工具里"


def test_list_packages_uses_ado_dir():
    """ado describe 会吐出每个包的全文（实测 49516 字符），ado dir 只需 4330。"""
    from server import stata_list_packages

    with patch("server._run_stata_command") as mock_run:
        stata_list_packages()
        assert mock_run.call_args[0][0] == "ado dir"


# --- stata_graph 的 command 是自由文本，须与 stata_run 同层护栏 -----------------
# 实测 stata_graph(command='!touch /tmp/x') 曾真实创建文件：该参数被原样拼进
# 执行串（导出模式下还进入临时 do 文件），而危险前缀检查此前只在 stata_run 里做。


def test_graph_rejects_shell_out_in_command():
    with patch("server._run_stata_command") as mock_run:
        result = stata_graph("!touch /tmp/pwned")
    assert getattr(result, "is_error", False)
    assert "危险前缀" in _result_text(result)
    mock_run.assert_not_called()


def test_graph_rejects_shell_out_in_export_mode():
    """导出模式会把 command 放进复合块 → 临时 do 文件，同样必须拦。"""
    with patch("server._run_stata_command") as mock_run:
        result = stata_graph("!touch /tmp/pwned", export=abs_path("out", "f.png"), replace=True)
    assert getattr(result, "is_error", False)
    assert "危险前缀" in _result_text(result)
    mock_run.assert_not_called()


def test_graph_rejects_mata_in_command():
    with patch("server._run_stata_command") as mock_run:
        result = stata_graph('mata: _stata("!rm -rf /")')
    assert getattr(result, "is_error", False)
    assert "Mata" in _result_text(result)
    mock_run.assert_not_called()


def test_graph_still_accepts_normal_plot_command():
    with patch("server._run_stata_command") as mock_run:
        stata_graph("twoway scatter price weight")
        assert "twoway scatter price weight" in mock_run.call_args[0][0]


# --- 导出成败以文件是否被写入为准 ---------------------------------------------


def test_export_excel_detects_stale_file_on_recovered_crash(tmp_path):
    """rc=997（崩溃已恢复、命令未执行）时，上次留下的同名文件不能算成功。"""
    target = tmp_path / "out.xlsx"
    target.write_text("STALE DATA FROM PREVIOUS RUN")
    crash_out = "StataSO_Execute 崩溃: boom\n(Stata 已自动恢复，请重试命令)"
    with patch("server._run_stata_command", return_value=crash_out):
        result = stata_export_excel(str(target), replace=True)
    text = _result_text(result)
    assert getattr(result, "is_error", False)
    assert "未写入文件" in text
    assert "已导出" not in text
    assert target.read_text() == "STALE DATA FROM PREVIOUS RUN"


def test_export_excel_detects_refused_overwrite(tmp_path):
    target = tmp_path / "out.xlsx"
    target.write_text("existing")
    with patch("server._run_stata_command", return_value="file already exists\nr(602);"):
        result = stata_export_excel(str(target), replace=False)
    text = _result_text(result)
    assert getattr(result, "is_error", False)
    assert "replace=True" in text


def test_export_excel_succeeds_when_file_freshly_written(tmp_path):
    target = tmp_path / "out.xlsx"

    def _write(*_a, **_kw):
        target.write_bytes(b"x" * 2048)
        return "file saved"

    with patch("server._run_stata_command", side_effect=_write):
        result = stata_export_excel(str(target), replace=True)
    assert not getattr(result, "is_error", False)
    assert "已导出 2.0 KB" in _result_text(result)


def test_export_excel_extensionless_path_uses_stata_xlsx_default(tmp_path):
    target = tmp_path / "data"
    actual = target.with_suffix(".xlsx")

    def _write(*_a, **_kw):
        actual.write_bytes(b"xlsx")
        return "file saved"

    with patch("server._run_stata_command", side_effect=_write) as mock_run:
        result = stata_export_excel(str(target), replace=True)
    assert not getattr(result, "is_error", False)
    assert f'using "{actual}"' in mock_run.call_args[0][0]
    assert str(actual) in _result_text(result)


def test_export_excel_rejects_varlist_path_injection(tmp_path):
    """varlist 注入应在拼命令之前就被拦下。"""
    with patch("server._run_stata_command") as mock_run:
        result = stata_export_excel(
            str(tmp_path / "safe.xlsx"),
            varlist="mpg using /tmp/evil/out.xlsx, replace //",
        )
    assert getattr(result, "is_error", False)
    mock_run.assert_not_called()


def test_run_blocks_comment_masked_shell_out():
    """stata_run 必须按解析后的块校验：sh/*x*/ell 在原文里不含 shell 一词。"""
    with patch("server._run_stata_command") as mock_run:
        result = stata_run("sh/*x*/ell echo pwned")
    assert getattr(result, "is_error", False)
    mock_run.assert_not_called()


def test_graph_blocks_comment_masked_shell_out():
    with patch("server._run_stata_command") as mock_run:
        result = stata_graph("/* x */ !touch /tmp/pwned")
    assert getattr(result, "is_error", False)
    mock_run.assert_not_called()


# --- 此前零行为覆盖的 5 个工具 -------------------------------------------------


def test_describe_default_lists_all_variables():
    with patch("server._run_stata_command") as mock_run:
        stata_describe()
        assert mock_run.call_args[0][0] == "describe"


def test_describe_with_varlist():
    with patch("server._run_stata_command") as mock_run:
        stata_describe("price mpg")
        assert mock_run.call_args[0][0] == "describe price mpg"


def test_describe_simple_keeps_varlist():
    """官方语法是 `describe [varlist] [, options]`，二者本就可共存。

    实测 `describe price mpg, simple` 只列这两个变量；旧实现在 simple=True 时
    丢弃 varlist，用户拿到的是**全部**变量清单，与请求不符。
    """
    with patch("server._run_stata_command") as mock_run:
        stata_describe("price mpg", simple=True)
        assert mock_run.call_args[0][0] == "describe price mpg, simple"


def test_describe_simple_without_varlist():
    with patch("server._run_stata_command") as mock_run:
        stata_describe(simple=True)
        assert mock_run.call_args[0][0] == "describe, simple"


def test_describe_rejects_injected_varlist():
    with patch("server._run_stata_command") as mock_run:
        result = stata_describe("price; !ls")
        assert getattr(result, "is_error", False)
        mock_run.assert_not_called()


def test_display_builds_expression():
    from server import stata_display

    with patch("server._run_stata_command") as mock_run:
        stata_display("r(mean)")
        assert mock_run.call_args[0][0] == "display r(mean)"


def test_display_rejects_injection():
    from server import stata_display

    with patch("server._run_stata_command") as mock_run:
        result = stata_display("1; !ls")
        assert getattr(result, "is_error", False)
        mock_run.assert_not_called()


def test_set_cwd_quotes_and_normalizes_path():
    from server import stata_set_cwd

    target = abs_path("data", "project")
    with patch("server._run_stata_command") as mock_run:
        stata_set_cwd(target)
        assert mock_run.call_args[0][0] == f'cd "{target}"'


def test_set_cwd_rejects_illegal_path():
    from server import stata_set_cwd

    with patch("server._run_stata_command") as mock_run:
        result = stata_set_cwd('/tmp/x";!ls')
        assert getattr(result, "is_error", False)
        mock_run.assert_not_called()


def test_run_do_file_passes_require_file_and_timeout():
    from server import stata_run_do_file

    target = abs_path("scripts", "run.do")
    with (
        patch("server._run_stata_command") as mock_run,
        patch("server.os.path.isfile", return_value=True),
    ):
        stata_run_do_file(target, timeout=900)
    assert mock_run.call_args[0][0] == f'do "{target}"'
    assert mock_run.call_args.kwargs["require_file"] == target
    assert mock_run.call_args.kwargs["timeout"] == 900


def test_run_do_file_falls_back_on_non_utf8_file(tmp_path):
    """非 UTF-8 的 do 文件应退回原样执行，而不是抛 Python 栈异常。

    ``UnicodeDecodeError`` 继承自 ``ValueError`` 而非 ``OSError``，此前的
    ``except OSError`` 兜不住它。中文 Windows 的 Stata do 编辑器默认不是 UTF-8，
    GBK/Big5 的 do 文件很常见 —— 而这类文件交给 Stata 自己执行本来完全正常。
    """
    from server import stata_run_do_file

    target = tmp_path / "gbk.do"
    target.write_bytes("* 中文注释\nsysuse auto, clear\n".encode("gbk"))

    with patch("server._run_stata_command") as mock_run:
        stata_run_do_file(str(target))

    assert mock_run.call_args[0][0] == f'do "{str(target)}"'
    assert mock_run.call_args.kwargs["require_file"] == str(target)


def test_run_do_file_clamps_timeout():
    """与 stata_run 一致地夹在 10–1800 之间，避免 0/负值让看门狗立即触发。"""
    from server import stata_run_do_file

    target = abs_path("scripts", "run.do")
    for given, expected in ((0, 10), (99999, 1800)):
        with (
            patch("server._run_stata_command") as mock_run,
            patch("server.os.path.isfile", return_value=True),
        ):
            stata_run_do_file(target, timeout=given)
        assert mock_run.call_args.kwargs["timeout"] == expected


def test_more_without_cache_gives_actionable_hint():
    import server
    from server import stata_more

    with server._output_lock:
        server._last_output = ""
    result = stata_more(page=1)
    assert "没有缓存" in _result_text(result)


def test_more_paginates_cached_output():
    import server
    from server import stata_more

    with server._output_lock:
        server._last_output = "L" * 10_000
    page1 = _result_text(stata_more(page=1, page_size=1000))
    assert page1.startswith("── 第 1/10 页")
    full = _result_text(stata_more(page=0))
    assert len(full) == 10_000, "page=0 应返回完整缓存"


# --- 未闭合块与数值边界 --------------------------------------------------------


def test_run_rejects_unclosed_block_with_actionable_message():
    """未闭合的块送去执行会挂死会话（实测 `capture noisily {` 单行即可复现）。"""
    with patch("server._run_stata_command") as mock_run:
        result = stata_run("capture noisily {")
    text = _result_text(result)
    assert getattr(result, "is_error", False)
    assert "未闭合" in text
    assert "}" in text
    mock_run.assert_not_called()


def test_run_still_blocks_danger_inside_unclosed_block():
    """危险命令恰是最易未闭合的一类，护栏不能因解析失败而放行。"""
    with patch("server._run_stata_command") as mock_run:
        result = stata_run("/**/python: import os")
    assert getattr(result, "is_error", False)
    assert "危险前缀" in _result_text(result)
    mock_run.assert_not_called()


def test_graph_rejects_negative_size():
    for kwargs in ({"width": -100}, {"height": -1}):
        with patch("server._run_stata_command") as mock_run:
            result = stata_graph("histogram price", **kwargs)
        assert getattr(result, "is_error", False)
        assert "不能为负数" in _result_text(result)
        mock_run.assert_not_called()


def test_graph_accepts_zero_size_as_unset():
    """0 表示「不指定」，是默认值，不能被负值校验误伤。"""
    with patch("server._run_stata_command") as mock_run:
        stata_graph("histogram price", width=0, height=0)
        mock_run.assert_called_once()


def test_regress_rejects_empty_depvar():
    """空 depvar 会拼出 `regress  weight`，Stata 把 weight 当因变量静默算错。"""
    with patch("server._run_stata_command") as mock_run:
        result = stata_regress("", "weight")
    assert getattr(result, "is_error", False)
    mock_run.assert_not_called()


def test_logistic_rejects_empty_depvar():
    with patch("server._run_stata_command") as mock_run:
        result = stata_logistic("", "weight")
    assert getattr(result, "is_error", False)
    mock_run.assert_not_called()


def test_ttest_rejects_empty_varname():
    with patch("server._run_stata_command") as mock_run:
        result = stata_ttest("")
    assert getattr(result, "is_error", False)
    mock_run.assert_not_called()


def test_ttest_bare_form_is_refused_not_emitted():
    """旧实现在只给 varname 时发出 `ttest price` —— 实测 r(100) by() required。

    单元测试当年只比对字符串，命令是否合法完全没验，直到真机 E2E 才暴露。
    """
    with patch("server._run_stata_command") as mock_run:
        result = stata_ttest("price")
        assert getattr(result, "is_error", False)
        mock_run.assert_not_called()


# ============================================================================
# stata_help — 全量内置命令覆盖（按需查帮助）
# ============================================================================


def test_help_builds_help_command():
    with patch("server._run_stata_command") as mock_run:
        stata_help("regress")
        assert mock_run.call_args[0][0] == "help regress"


def test_help_passes_page_through():
    with patch("server._run_stata_command") as mock_run:
        stata_help("regress", page=3)
        assert mock_run.call_args.kwargs.get("page") == 3


def test_help_allows_multiword_subtopic():
    with patch("server._run_stata_command") as mock_run:
        stata_help("regress postestimation")
        assert mock_run.call_args[0][0] == "help regress postestimation"


def test_help_rejects_empty():
    with patch("server._run_stata_command") as mock_run:
        result = stata_help("   ")
        assert getattr(result, "is_error", False)
        mock_run.assert_not_called()


def test_help_rejects_injection():
    """帮助主题不能夹带第二条命令。"""
    for bad in ["regress\nmata:", "regress; shell ls", "regress`x'", "reg $var", 'reg "x"']:
        with patch("server._run_stata_command") as mock_run:
            result = stata_help(bad)
            assert getattr(result, "is_error", False), f"应拒绝: {bad!r}"
            mock_run.assert_not_called()


def test_help_topic_regex_accepts_common_commands():
    for ok in ["regress", "xtreg", "reghdfe", "estat firststage", "regress postestimation", "_n"]:
        assert _HELP_TOPIC_RE.match(ok), f"应接受: {ok!r}"


# ============================================================================
# 估计 wrapper：命令构造
# ============================================================================


def test_probit_builds_command():
    with patch("server._run_stata_command") as mock_run:
        stata_probit("foreign", "price mpg", options="robust")
        assert mock_run.call_args[0][0] == "probit foreign price mpg, robust"


def test_probit_appends_marginal_effects():
    with patch("server._run_stata_command") as mock_run:
        stata_probit("foreign", "price mpg", marginal_effects=True)
        cmd = mock_run.call_args[0][0]
        assert cmd == "probit foreign price mpg\nmargins, dydx(*)"


def test_poisson_irr_option():
    with patch("server._run_stata_command") as mock_run:
        stata_poisson("count", "x1 x2", irr=True, options="robust")
        assert mock_run.call_args[0][0] == "poisson count x1 x2, irr robust"


def test_xtreg_effects_appended_as_option():
    with patch("server._run_stata_command") as mock_run:
        stata_xtreg("y", "x1 x2", effects="fe", options="robust")
        assert mock_run.call_args[0][0] == "xtreg y x1 x2, fe robust"


def test_xtreg_rejects_bad_effects():
    with patch("server._run_stata_command") as mock_run:
        result = stata_xtreg("y", "x1", effects="xyz")
        assert getattr(result, "is_error", False)
        mock_run.assert_not_called()


def test_ivregress_builds_endog_syntax():
    with patch("server._run_stata_command") as mock_run:
        stata_ivregress("y", "x", "z1 z2", exogenous="w1", options="robust")
        cmd = mock_run.call_args[0][0]
        assert cmd == "ivregress 2sls y w1 (x = z1 z2), robust"


def test_ivregress_rejects_bad_estimator():
    with patch("server._run_stata_command") as mock_run:
        result = stata_ivregress("y", "x", "z", estimator="ols")
        assert getattr(result, "is_error", False)
        mock_run.assert_not_called()


def test_ivregress_requires_endog_and_instruments():
    with patch("server._run_stata_command") as mock_run:
        assert getattr(stata_ivregress("y", "", "z"), "is_error", False)
        assert getattr(stata_ivregress("y", "x", ""), "is_error", False)
        mock_run.assert_not_called()


def test_correlate_vs_pwcorr():
    with patch("server._run_stata_command") as mock_run:
        stata_correlate("price weight")
        assert mock_run.call_args[0][0] == "correlate price weight"
    with patch("server._run_stata_command") as mock_run:
        stata_correlate("price weight", pairwise=True, options="sig")
        assert mock_run.call_args[0][0] == "pwcorr price weight, sig"


def test_margins_builds_dydx_and_at():
    with patch("server._run_stata_command") as mock_run:
        stata_margins(dydx="price", at="(mean) _all", options="atmeans")
        cmd = mock_run.call_args[0][0]
        assert cmd == "margins, dydx(price) at((mean) _all) atmeans"


def test_test_builds_command():
    with patch("server._run_stata_command") as mock_run:
        stata_test("weight = mpg")
        assert mock_run.call_args[0][0] == "test weight = mpg"


def test_test_rejects_empty():
    with patch("server._run_stata_command") as mock_run:
        assert getattr(stata_test("  "), "is_error", False)
        mock_run.assert_not_called()


# ============================================================================
# 改数据集 wrapper：命令构造
# ============================================================================


def test_generate_builds_command():
    with patch("server._run_stata_command") as mock_run:
        stata_generate("lprice", "ln(price)", condition="price > 0")
        assert mock_run.call_args[0][0] == "generate lprice = ln(price) if price > 0"


def test_generate_rejects_empty_expression():
    with patch("server._run_stata_command") as mock_run:
        assert getattr(stata_generate("x", "  "), "is_error", False)
        mock_run.assert_not_called()


def test_egen_with_by_prefix():
    with patch("server._run_stata_command") as mock_run:
        stata_egen("mp", "mean(price)", by="foreign")
        assert mock_run.call_args[0][0] == "bysort foreign: egen mp = mean(price)"


def test_egen_without_by():
    with patch("server._run_stata_command") as mock_run:
        stata_egen("rm", "rowmean(x1 x2)")
        assert mock_run.call_args[0][0] == "egen rm = rowmean(x1 x2)"


def test_predict_builds_command():
    with patch("server._run_stata_command") as mock_run:
        stata_predict("resid", options="residuals")
        assert mock_run.call_args[0][0] == "predict resid, residuals"


def test_predict_default_no_options():
    with patch("server._run_stata_command") as mock_run:
        stata_predict("yhat")
        assert mock_run.call_args[0][0] == "predict yhat"


def test_new_wrappers_reject_injection_in_identifier():
    """newvar/depvar 走标识符校验，注入字符必被拒。"""
    with patch("server._run_stata_command") as mock_run:
        assert getattr(stata_generate("x;drop", "1"), "is_error", False)
        assert getattr(stata_probit("y;shell ls", "x"), "is_error", False)
        mock_run.assert_not_called()


# ============================================================================
# 包管理补全：uninstall / describe
# ============================================================================


def test_uninstall_package_builds_command():
    with patch("server._run_stata_command") as mock_run:
        stata_uninstall_package("winsor2")
        assert mock_run.call_args[0][0] == "ado uninstall winsor2"


def test_uninstall_package_rejects_empty():
    with patch("server._run_stata_command") as mock_run:
        assert getattr(stata_uninstall_package("  "), "is_error", False)
        mock_run.assert_not_called()


def test_uninstall_package_rejects_injection():
    with patch("server._run_stata_command") as mock_run:
        assert getattr(stata_uninstall_package("pkg; shell ls"), "is_error", False)
        mock_run.assert_not_called()


def test_describe_package_installed_uses_ado_describe():
    """默认本地：ado describe，无网络。"""
    with patch("server._run_stata_command") as mock_run:
        stata_describe_package("estout")
        assert mock_run.call_args[0][0] == "ado describe estout"


def test_describe_package_ssc_uses_network():
    with patch("server._run_stata_command") as mock_run:
        stata_describe_package("estout", source="ssc")
        assert mock_run.call_args[0][0] == "ssc describe estout"


def test_describe_package_rejects_bad_source():
    with patch("server._run_stata_command") as mock_run:
        result = stata_describe_package("estout", source="pypi")
        assert getattr(result, "is_error", False)
        mock_run.assert_not_called()


def test_describe_package_rejects_empty():
    with patch("server._run_stata_command") as mock_run:
        assert getattr(stata_describe_package(""), "is_error", False)
        mock_run.assert_not_called()


# ============================================================================
# 图形绘制与导出 — 命令形状、清理动作、失败透传
# ============================================================================


def _writes(target, output="file written in PNG format"):
    """让 mock 的 _run_stata_command 真的落盘，以通过 mtime 写入判定。"""

    def _run(*_a, **_kw):
        target.write_bytes(b"binary payload")
        return output

    return _run


# --- 输入护栏 ----------------------------------------------------------------


def test_graph_rejects_newline_in_command():
    """换行会把第二条命令带进复合块；且复合块是原子执行，出错难定位。"""
    with patch("server._run_stata_command") as mock_run:
        result = stata_graph("scatter price weight\nhistogram price")
    assert getattr(result, "is_error", False)
    assert "非法控制字符" in _result_text(result)
    mock_run.assert_not_called()


def test_graph_rejects_null_byte_in_command():
    with patch("server._run_stata_command") as mock_run:
        result = stata_graph("scatter price\x00 weight")
    assert getattr(result, "is_error", False)
    assert "非法控制字符" in _result_text(result)
    mock_run.assert_not_called()


def test_graph_rejects_illegal_export_path():
    """export 路径直接进 graph export "..."，分号可提前闭合并追加命令。"""
    with patch("server._run_stata_command") as mock_run:
        result = stata_graph(
            "scatter price weight", export=abs_path("out", "fig.png; shell evil")
        )
    assert getattr(result, "is_error", False)
    mock_run.assert_not_called()


def test_graph_surfaces_internal_exception_instead_of_crashing():
    """工具内部异常必须变成 isError 结果，不能让 MCP 连接收到裸 traceback。"""
    with patch("server._normalize_path", side_effect=RuntimeError("boom")):
        result = stata_graph("scatter price weight", export=abs_path("out", "fig.png"))
    assert getattr(result, "is_error", False)
    text = _result_text(result)
    assert "图形生成失败" in text
    assert "RuntimeError" in text, "应保留异常类型，便于定位"


# --- 命令形状 ----------------------------------------------------------------


def test_graph_without_export_only_sets_scheme_then_plots():
    with patch("server._run_stata_command") as mock_run:
        stata_graph("histogram price", scheme="s2mono")
    cmd = mock_run.call_args[0][0]
    assert cmd == "set scheme s2mono\nhistogram price"
    assert "capture noisily" not in cmd, "无导出时不该套复合块，错误定位更清晰"


def test_graph_uses_extended_timeout():
    """图形渲染比普通命令慢，默认 60s 容易误判超时。"""
    for kwargs in ({}, {"export": abs_path("out", "fig.png")}):
        with patch("server._run_stata_command", return_value="ok") as mock_run:
            stata_graph("scatter price weight", **kwargs)
        assert mock_run.call_args[1]["timeout"] == 120


def test_graph_export_block_disables_graphics_for_headless():
    """headless 下第三方绘图包会试图开图窗并挂起，必须先关图形显示。"""
    with patch("server._run_stata_command", return_value="ok") as mock_run:
        stata_graph("scatter price weight", export=abs_path("out", "fig.png"))
    cmd = mock_run.call_args[0][0]
    assert "set graphics off" in cmd
    assert cmd.index("set graphics off") < cmd.index("scatter price weight")


def test_graph_export_drops_cached_graphs_outside_the_block():
    """graph drop 必须在复合块外：块内命令出错时它会被一起跳过，图形对象泄漏。

    drop 的目标是匿名图 `Graph` 而非 `_all` —— 后者会连用户具名的图一起摧毁，
    多面板工作流因此不可用（见 test_graph_export_preserves_named_graphs）。
    """
    with patch("server._run_stata_command", return_value="ok") as mock_run:
        stata_graph("scatter price weight", export=abs_path("out", "fig.png"))
    cmd = mock_run.call_args[0][0]
    assert cmd.rstrip().endswith("capture noisily graph drop Graph")
    assert cmd.index("}") < cmd.index("graph drop Graph")


def test_graph_export_omits_replace_by_default():
    """replace 默认 False：安全优先，不能悄悄覆盖用户已有的图。"""
    with patch("server._run_stata_command", return_value="ok") as mock_run:
        stata_graph("scatter price weight", export=abs_path("out", "fig.png"))
    assert "replace" not in mock_run.call_args[0][0]


def test_graph_export_omits_size_options_when_unset():
    with patch("server._run_stata_command", return_value="ok") as mock_run:
        stata_graph(
            "scatter price weight", export=abs_path("out", "fig.png"), width=0, height=0
        )
    cmd = mock_run.call_args[0][0]
    assert "width(" not in cmd
    assert "height(" not in cmd


def test_graph_export_resolves_relative_path_to_absolute():
    """相对路径必须先规范化，否则 Stata cwd 与 Python cwd 不一致时写错地方。"""
    with patch("server._run_stata_command", return_value="ok") as mock_run:
        stata_graph("scatter price weight", export="out/fig.png")
    cmd = mock_run.call_args[0][0]
    assert f'graph export "{_normalize_path("out/fig.png")}"' in cmd


def test_graph_export_success_message_reports_file_size(tmp_path):
    target = tmp_path / "fig.png"
    with patch("server._run_stata_command", side_effect=_writes(target)):
        result = stata_graph(
            "scatter price weight", export=str(target), replace=True
        )
    text = _result_text(result)
    assert not getattr(result, "is_error", False)
    assert str(target) in text
    assert "B)" in text, "导出确认要带体积，0 字节的失败才看得出来"


def test_graph_export_passes_through_upstream_error(tmp_path):
    """_run_stata_command 已判错时直接透传，不能再叠一句「图形已导出」。"""
    target = tmp_path / "fig.png"
    target.write_bytes(b"stale")
    with patch(
        "server._run_stata_command",
        return_value=_make_error_result("Stata 无响应，请重启 MCP Server"),
    ):
        result = stata_graph("scatter price weight", export=str(target), replace=True)
    text = _result_text(result)
    assert getattr(result, "is_error", False)
    assert "重启 MCP Server" in text
    assert "图形已导出" not in text


# --- 数据导出 ----------------------------------------------------------------


def test_export_excel_rejects_illegal_filepath():
    """filepath 直接进 export excel using "..."，分号可提前闭合并追加命令。"""
    with patch("server._run_stata_command") as mock_run:
        result = stata_export_excel(abs_path("out", "d.xlsx; shell evil"))
    assert getattr(result, "is_error", False)
    mock_run.assert_not_called()


def test_export_excel_without_varlist_exports_all_columns(tmp_path):
    target = tmp_path / "data.xlsx"
    with patch("server._run_stata_command", side_effect=_writes(target)) as mock_run:
        stata_export_excel(str(target), replace=True)
    cmd = mock_run.call_args[0][0]
    assert cmd.startswith(f'export excel using "{target}"')
    assert "firstrow(variables)" in cmd
    assert 'sheet("Sheet1")' in cmd


def test_export_excel_success_message_reports_size_and_path(tmp_path):
    target = tmp_path / "data.xlsx"
    with patch("server._run_stata_command", side_effect=_writes(target)):
        result = stata_export_excel(str(target), replace=True)
    text = _result_text(result)
    assert not getattr(result, "is_error", False)
    assert f"-> {target}" in text


def test_export_excel_passes_through_upstream_error(tmp_path):
    target = tmp_path / "data.xlsx"
    target.write_bytes(b"stale")
    with patch(
        "server._run_stata_command",
        return_value=_make_error_result("Stata 无响应，请重启 MCP Server"),
    ):
        result = stata_export_excel(str(target), replace=True)
    text = _result_text(result)
    assert getattr(result, "is_error", False)
    assert "已导出" not in text


# --- 回归结果导出 -------------------------------------------------------------


def test_export_results_builds_esttab_command(tmp_path):
    target = tmp_path / "res.csv"
    with (
        patch("server._execute_safe", return_value=(0, "estout installed")),
        patch("server._run_stata_command", side_effect=_writes(target)) as mock_run,
    ):
        stata_export_excel(str(target), results=True, replace=True)
    cmd = mock_run.call_args[0][0]
    assert cmd.startswith(f'esttab using "{target}", csv replace')
    assert "plain nogaps nomtitles nonumber" in cmd


def test_export_results_keeps_csv_path_without_extra_notice(tmp_path):
    """路径本就是 .csv 时不该冒出「已改用 CSV」的提示，那会让人以为改了参数。"""
    target = tmp_path / "res.csv"
    with (
        patch("server._execute_safe", return_value=(0, "estout installed")),
        patch("server._run_stata_command", side_effect=_writes(target)),
    ):
        result = stata_export_excel(str(target), results=True, replace=True)
    assert "已自动改用" not in _result_text(result)


def test_export_results_rewrites_non_csv_extension(tmp_path):
    """esttab 只出 CSV；.txt 同样要改扩展名并说明，不能静默写成 .txt。"""
    csv_target = tmp_path / "res.csv"
    with (
        patch("server._execute_safe", return_value=(0, "estout installed")),
        patch("server._run_stata_command", side_effect=_writes(csv_target)) as mock_run,
    ):
        result = stata_export_excel(
            str(tmp_path / "res.txt"), results=True, replace=True
        )
    assert f'esttab using "{csv_target}"' in mock_run.call_args[0][0]
    assert "已导出为 CSV" in _result_text(result)


def test_export_results_never_leaks_varlist_into_esttab(tmp_path):
    """varlist 只对数据导出有效；漏进 esttab 串会把 using 路径整段挪位。"""
    target = tmp_path / "res.csv"
    with (
        patch("server._execute_safe", return_value=(0, "estout installed")),
        patch("server._run_stata_command", side_effect=_writes(target)) as mock_run,
    ):
        stata_export_excel(str(target), varlist="mpg price", results=True, replace=True)
    cmd = mock_run.call_args[0][0]
    assert "mpg" not in cmd
    assert cmd.startswith(f'esttab using "{target}"')


# ============================================================================
# 与官方能力边界对齐 —— scheme 主题 / 格式选项
# ============================================================================


def test_graph_does_not_override_user_scheme_by_default():
    """不传 scheme 就不该动主题。

    Stata 19 的默认 scheme 是 stcolor（实测 c(scheme)）；旧实现硬编码
    scheme="s2color" 并每次执行 `set scheme s2color`，等于每次绘图都把用户的
    主题悄悄改回老配色，且调用结束后不还原 —— 这是覆盖，不是设定。
    """
    with patch("server._run_stata_command") as mock_run:
        stata_graph("scatter price weight")
    cmd = mock_run.call_args[0][0]
    assert "set scheme" not in cmd
    assert cmd == "scatter price weight"


def test_graph_export_does_not_override_user_scheme_by_default():
    with patch("server._run_stata_command", return_value="ok") as mock_run:
        stata_graph("scatter price weight", export=abs_path("out", "f.png"))
    cmd = mock_run.call_args[0][0]
    assert "set scheme" not in cmd
    assert "set graphics off" in cmd, "headless 防挂起不受影响"


def test_graph_still_applies_scheme_when_asked():
    with patch("server._run_stata_command", return_value="ok") as mock_run:
        stata_graph("scatter price weight", scheme="economist",
                    export=abs_path("out", "f.png"))
    cmd = mock_run.call_args[0][0]
    assert "set scheme economist" in cmd
    assert cmd.index("set scheme") < cmd.index("scatter price weight")


# --- 主题设定 stata_scheme ---------------------------------------------------


def test_scheme_list_uses_graph_query():
    """列出可用方案走官方的 graph query, schemes（本机实测 26 个内置）。"""
    from server import stata_scheme

    with patch("server._run_stata_command") as mock_run:
        stata_scheme()
    assert mock_run.call_args[0][0] == "graph query, schemes"


def test_scheme_get_reads_creturn_not_bare_set():
    """查当前方案用 c(scheme)；裸 `set scheme` 不是查询命令。"""
    from server import stata_scheme

    with patch("server._run_stata_command") as mock_run:
        stata_scheme(action="get")
    assert mock_run.call_args[0][0] == "display c(scheme)"


def test_scheme_set_applies_named_scheme():
    from server import stata_scheme

    with patch("server._run_stata_command") as mock_run:
        stata_scheme(action="set", scheme="economist")
    assert mock_run.call_args[0][0] == "set scheme economist"


def test_scheme_set_supports_permanently():
    from server import stata_scheme

    with patch("server._run_stata_command") as mock_run:
        stata_scheme(action="set", scheme="stcolor", permanently=True)
    assert mock_run.call_args[0][0] == "set scheme stcolor, permanently"


def test_scheme_set_requires_a_name():
    """action=set 却不给名字会拼出裸 `set scheme`，改变语义而非报错。"""
    from server import stata_scheme

    with patch("server._run_stata_command") as mock_run:
        result = stata_scheme(action="set")
    assert getattr(result, "is_error", False)
    mock_run.assert_not_called()


def test_scheme_rejects_injected_name():
    from server import stata_scheme

    with patch("server._run_stata_command") as mock_run:
        result = stata_scheme(action="set", scheme="stcolor, permanently")
    assert getattr(result, "is_error", False)
    mock_run.assert_not_called()


def test_scheme_rejects_unknown_action():
    from server import stata_scheme

    with patch("server._run_stata_command") as mock_run:
        result = stata_scheme(action="delete", scheme="stcolor")
    assert getattr(result, "is_error", False)
    mock_run.assert_not_called()


# --- fontface 校验 -----------------------------------------------------------


def test_graph_accepts_fontface_with_spaces():
    with patch("server._run_stata_command", return_value="ok") as mock_run:
        stata_graph("scatter price weight", export=abs_path("out", "f.pdf"),
                    width=0, fontface="Times New Roman")
    assert 'fontface("Times New Roman")' in mock_run.call_args[0][0]


def test_graph_rejects_fontface_closing_the_option():
    """fontface 被双引号包裹后拼入；`"` 与 `)` 能提前闭合并追加选项。"""
    for bad in ['Helvetica") shell evil //', "Helvetica) x", "He;llo", "He`v", "He$v"]:
        with patch("server._run_stata_command") as mock_run:
            result = stata_graph("scatter price weight",
                                 export=abs_path("out", "f.pdf"), fontface=bad)
        assert getattr(result, "is_error", False), f"应拒绝: {bad}"
        mock_run.assert_not_called()


def test_graph_drops_quality_for_non_jpg_and_says_so():
    with patch("server._run_stata_command", return_value="ok") as mock_run:
        stata_graph("scatter price weight", export=abs_path("out", "f.png"), quality=60)
    assert "quality(" not in mock_run.call_args[0][0], "png 传 quality 会 r(198)"


def test_graph_applies_quality_for_jpg():
    with patch("server._run_stata_command", return_value="ok") as mock_run:
        stata_graph("scatter price weight", export=abs_path("out", "f.jpg"), quality=60)
    assert "quality(60)" in mock_run.call_args[0][0]


def test_graph_rejects_negative_quality_and_mag():
    for kwargs in ({"quality": -1}, {"mag": -5}):
        with patch("server._run_stata_command") as mock_run:
            result = stata_graph("histogram price", **kwargs)
        assert getattr(result, "is_error", False)
        mock_run.assert_not_called()


# ============================================================================
# 数据导出 —— 对齐 export excel / export delimited 的官方选项
# ============================================================================


def test_export_excel_sheet_mode_resolves_worksheet_conflict():
    """官方对 worksheet 已存在的解法就是 sheet(..., modify|replace)。

    实测拒绝覆盖时 Stata 提示 "specify option sheet(..., modify) or
    option sheet(..., replace)" —— 旧实现没暴露这个选项，用户无路可走。

    注意不能叠加文件级 replace，二者互斥（见
    test_export_excel_rejects_sheet_mode_combined_with_file_replace）。
    """
    with patch("server._run_stata_command", return_value="ok") as mock_run:
        stata_export_excel(abs_path("out", "d.xlsx"), sheet="Data",
                           sheet_mode="replace")
    assert 'sheet("Data", replace)' in mock_run.call_args[0][0]


def test_export_excel_rejects_unknown_sheet_mode():
    with patch("server._run_stata_command") as mock_run:
        result = stata_export_excel(abs_path("out", "d.xlsx"), sheet_mode="clobber")
    assert getattr(result, "is_error", False)
    mock_run.assert_not_called()


def test_export_excel_applies_if_and_in_before_comma():
    """[if] [in] 属于命令的另一个语法位置，必须在逗号之前。"""
    with patch("server._run_stata_command", return_value="ok") as mock_run:
        stata_export_excel(abs_path("out", "d.xlsx"), condition="foreign == 1",
                           in_range="1/20", replace=True)
    cmd = mock_run.call_args[0][0]
    assert "if foreign == 1 in 1/20," in cmd
    assert cmd.index("if foreign") < cmd.index(",")


def test_export_excel_firstrow_varlabels():
    with patch("server._run_stata_command", return_value="ok") as mock_run:
        stata_export_excel(abs_path("out", "d.xlsx"), firstrow="varlabels", replace=True)
    assert "firstrow(varlabels)" in mock_run.call_args[0][0]


def test_export_excel_firstrow_none_omits_option():
    with patch("server._run_stata_command", return_value="ok") as mock_run:
        stata_export_excel(abs_path("out", "d.xlsx"), firstrow="none", replace=True)
    assert "firstrow(" not in mock_run.call_args[0][0]


def test_export_excel_rejects_unknown_firstrow():
    with patch("server._run_stata_command") as mock_run:
        result = stata_export_excel(abs_path("out", "d.xlsx"), firstrow="header")
    assert getattr(result, "is_error", False)
    mock_run.assert_not_called()


def test_export_excel_supports_cell_and_nolabel():
    with patch("server._run_stata_command", return_value="ok") as mock_run:
        stata_export_excel(abs_path("out", "d.xlsx"), cell="B3", nolabel=True,
                           replace=True)
    cmd = mock_run.call_args[0][0]
    assert "cell(B3)" in cmd
    assert "nolabel" in cmd


def test_export_excel_rejects_invalid_cell_reference():
    with patch("server._run_stata_command") as mock_run:
        result = stata_export_excel(abs_path("out", "d.xlsx"), cell="B3) replace")
    assert getattr(result, "is_error", False)
    assert "单元格引用" in _result_text(result)
    mock_run.assert_not_called()


def test_export_excel_options_escape_hatch_covers_long_tail():
    """keepcellfmt / datestring() / locale() 等长尾选项走 options 自由文本。"""
    with patch("server._run_stata_command", return_value="ok") as mock_run:
        stata_export_excel(abs_path("out", "d.xlsx"), replace=True,
                           options='keepcellfmt missing("NA")')
    cmd = mock_run.call_args[0][0]
    assert 'keepcellfmt missing("NA")' in cmd


def test_export_excel_rejects_injected_options():
    with patch("server._run_stata_command") as mock_run:
        result = stata_export_excel(abs_path("out", "d.xlsx"), options="replace; shell evil")
    assert getattr(result, "is_error", False)
    mock_run.assert_not_called()


# --- export delimited --------------------------------------------------------


def test_export_delimited_defaults_to_comma_separated():
    from server import stata_export_delimited

    with patch("server._run_stata_command", return_value="ok") as mock_run:
        stata_export_delimited(abs_path("out", "d.csv"), replace=True)
    cmd = mock_run.call_args[0][0]
    assert cmd.startswith(f'export delimited using "{abs_path("out", "d.csv")}"')
    assert "replace" in cmd
    assert "delimiter(" not in cmd, "默认逗号，不必显式指定"


def test_export_delimited_tab_uses_keyword_not_literal():
    """官方语法是 delimiter(tab) —— tab 是关键字，不能写成引号里的字面量。"""
    from server import stata_export_delimited

    with patch("server._run_stata_command", return_value="ok") as mock_run:
        stata_export_delimited(abs_path("out", "d.txt"), delimiter="tab", replace=True)
    assert "delimiter(tab)" in mock_run.call_args[0][0]


def test_export_delimited_custom_char_is_quoted():
    from server import stata_export_delimited

    with patch("server._run_stata_command", return_value="ok") as mock_run:
        stata_export_delimited(abs_path("out", "d.txt"), delimiter=";", replace=True)
    assert 'delimiter(";")' in mock_run.call_args[0][0]


def test_export_delimited_rejects_multichar_delimiter():
    from server import stata_export_delimited

    with patch("server._run_stata_command") as mock_run:
        result = stata_export_delimited(abs_path("out", "d.txt"), delimiter="||")
    assert getattr(result, "is_error", False)
    mock_run.assert_not_called()


def test_export_delimited_supports_official_flags():
    from server import stata_export_delimited

    with patch("server._run_stata_command", return_value="ok") as mock_run:
        stata_export_delimited(abs_path("out", "d.csv"), novarnames=True, nolabel=True,
                               datafmt=True, quote=True, replace=True)
    cmd = mock_run.call_args[0][0]
    for flag in ("novarnames", "nolabel", "datafmt", "quote", "replace"):
        assert flag in cmd, flag


def test_export_delimited_applies_varlist_and_filters():
    from server import stata_export_delimited

    with patch("server._run_stata_command", return_value="ok") as mock_run:
        stata_export_delimited(abs_path("out", "d.csv"), varlist="make price",
                               condition="foreign == 1", in_range="1/10", replace=True)
    cmd = mock_run.call_args[0][0]
    assert cmd.startswith("export delimited make price using")
    assert "if foreign == 1 in 1/10," in cmd


def test_export_delimited_reports_failure_when_not_written(tmp_path):
    """与其他导出一致：以文件是否被本次调用写入为准，不看返回码。"""
    from server import stata_export_delimited

    target = tmp_path / "d.csv"
    target.write_bytes(b"stale")
    with patch("server._run_stata_command", return_value="file already exists\nr(602);"):
        result = stata_export_delimited(str(target))
    text = _result_text(result)
    assert getattr(result, "is_error", False)
    assert "replace=True" in text


def test_export_delimited_extensionless_path_uses_csv_default(tmp_path):
    from server import stata_export_delimited

    target = tmp_path / "data"
    actual = target.with_suffix(".csv")

    def _write(*_a, **_kw):
        actual.write_bytes(b"a,b\n1,2\n")
        return "file saved"

    with patch("server._run_stata_command", side_effect=_write) as mock_run:
        result = stata_export_delimited(str(target), replace=True)
    assert not getattr(result, "is_error", False)
    assert f'using "{actual}"' in mock_run.call_args[0][0]
    assert str(actual) in _result_text(result)


def test_export_excel_rejects_sheet_mode_combined_with_file_replace():
    """实测 Stata：option sheet(...,replace) may not be combined with option replace。

    二者语义冲突：文件级 replace 重建整个文件（不可能有工作表冲突），
    sheet_mode 则是针对已存在文件里的某张工作表。
    """
    with patch("server._run_stata_command") as mock_run:
        result = stata_export_excel(abs_path("out", "d.xlsx"),
                                    sheet_mode="replace", replace=True)
    text = _result_text(result)
    assert getattr(result, "is_error", False)
    assert "不能同时" in text
    mock_run.assert_not_called()


def test_export_excel_allows_sheet_mode_without_file_replace():
    with patch("server._run_stata_command", return_value="ok") as mock_run:
        stata_export_excel(abs_path("out", "d.xlsx"), sheet="Data", sheet_mode="modify")
    cmd = mock_run.call_args[0][0]
    assert 'sheet("Data", modify)' in cmd
    assert not cmd.rstrip().endswith("replace")


def test_export_excel_explains_empty_selection():
    """筛选后 0 条观测时 Stata 报的是 Excel 行数上限，与真实原因无关。

    实测 `if foreign == 1 in 1/10`（auto 前 10 条全为国产车）→
    "observations must be between 1 and 1048576"，用户完全看不出是筛选没命中。
    """
    stata_err = _make_error_result(
        "[返回码: 198] 命令语法错误 — export excel ...\n"
        "observations must be between 1 and 1048576\nr(198);"
    )
    with patch("server._run_stata_command", return_value=stata_err):
        result = stata_export_excel(abs_path("out", "d.xlsx"),
                                    condition="foreign == 1", in_range="1/10")
    text = _result_text(result)
    assert getattr(result, "is_error", False)
    assert "未匹配到任何观测" in text
    assert "前 n 条观测里" in text, "if+in 叠加的语义陷阱要点明"


def test_export_excel_empty_selection_hint_only_when_filtered():
    """没传筛选条件时不该冒出「筛选未命中」的猜测。"""
    stata_err = _make_error_result("observations must be between 1 and 1048576\nr(198);")
    with patch("server._run_stata_command", return_value=stata_err):
        result = stata_export_excel(abs_path("out", "d.xlsx"))
    assert "未匹配到任何观测" not in _result_text(result)


def test_export_delimited_explains_empty_selection():
    from server import stata_export_delimited

    stata_err = _make_error_result("observations must be between 1 and 1048576\nr(198);")
    with patch("server._run_stata_command", return_value=stata_err):
        result = stata_export_delimited(abs_path("out", "d.csv"), condition="price > 1e9")
    assert "未匹配到任何观测" in _result_text(result)


# ============================================================================
# 语法位置对齐 —— 官方支持 [in] / [if] / 选项的工具都要能表达
# ============================================================================
# 实测确认下列命令均接受 [in]（`test` 例外：它作用于已估计模型，本就不接受）。


def test_estimation_tools_support_in_range():
    """估计命令官方语法是 `cmd depvar indepvars [if] [in] [weight] [, options]`。"""
    cases = [
        (stata_regress, ("price", "weight"), "regress"),
        (stata_logistic, ("foreign", "weight"), "logistic"),
        (stata_probit, ("foreign", "weight"), "probit"),
        (stata_poisson, ("rep78", "weight"), "poisson"),
        (stata_xtreg, ("price", "weight"), "xtreg"),
    ]
    for fn, args, name in cases:
        with patch("server._run_stata_command") as mock_run:
            fn(*args, in_range="1/40")
        cmd = mock_run.call_args[0][0]
        assert " in 1/40" in cmd, f"{name} 应支持 in_range: {cmd}"


def test_in_range_follows_if_and_precedes_comma():
    with patch("server._run_stata_command") as mock_run:
        stata_regress("price", "weight", condition="foreign == 1",
                      in_range="1/40", options="robust")
    cmd = mock_run.call_args[0][0]
    assert cmd == "regress price weight if foreign == 1 in 1/40, robust"


def test_exploration_tools_support_in_range():
    cases = [
        (stata_summarize, ("price",), "summarize"),
        (stata_codebook, ("price",), "codebook"),
        (stata_tabulate, ("rep78",), "tabulate"),
        (stata_correlate, ("price mpg",), "correlate"),
    ]
    for fn, args, name in cases:
        with patch("server._run_stata_command") as mock_run:
            fn(*args, in_range="1/40")
        cmd = mock_run.call_args[0][0]
        assert " in 1/40" in cmd, f"{name} 应支持 in_range: {cmd}"


def test_data_creation_tools_support_in_range():
    """generate/egen/predict 官方都接受 [in]，用于只对部分观测赋值。"""
    with patch("server._run_stata_command") as mock_run:
        stata_generate("flag", "1", in_range="1/40")
    assert " in 1/40" in mock_run.call_args[0][0]

    with patch("server._run_stata_command") as mock_run:
        stata_egen("grp_mean", "mean(price)", in_range="1/40")
    assert " in 1/40" in mock_run.call_args[0][0]

    with patch("server._run_stata_command") as mock_run:
        stata_predict("yhat", in_range="1/40")
    assert " in 1/40" in mock_run.call_args[0][0]


def test_margins_supports_if_and_in():
    """margins 官方语法含 [if] [in]，旧实现连 condition 都没有。"""
    with patch("server._run_stata_command") as mock_run:
        stata_margins("foreign", condition="price > 5000", in_range="1/40")
    cmd = mock_run.call_args[0][0]
    assert "if price > 5000" in cmd
    assert "in 1/40" in cmd


def test_ttest_and_ivregress_support_in_range():
    with patch("server._run_stata_command") as mock_run:
        stata_ttest("price", compare_to="5000", in_range="1/40")
    assert " in 1/40" in mock_run.call_args[0][0]

    with patch("server._run_stata_command") as mock_run:
        stata_ivregress("price", "weight", "length", in_range="1/40")
    assert " in 1/40" in mock_run.call_args[0][0]


def test_use_dataset_supports_conditional_load():
    """`use file if exp in range, clear` 是官方语法，可只载入子集。"""
    with patch("server._run_stata_command") as mock_run, \
         patch("server.os.path.isfile", return_value=True):
        stata_use_dataset(abs_path("data", "auto.dta"), condition="foreign == 1",
                          in_range="1/40")
    cmd = mock_run.call_args[0][0]
    assert "if foreign == 1 in 1/40" in cmd
    assert cmd.index("if foreign") < cmd.index(", clear")


def test_exploration_tools_have_options_escape_hatch():
    """长尾官方选项（noobs/clean/separator()/missing/row/column…）需有出口。"""
    with patch("server._run_stata_command") as mock_run:
        stata_list("price", options="noobs clean")
    assert "noobs clean" in mock_run.call_args[0][0]

    with patch("server._run_stata_command") as mock_run:
        stata_tabulate("rep78", options="missing nolabel")
    assert "missing nolabel" in mock_run.call_args[0][0]

    with patch("server._run_stata_command") as mock_run:
        stata_summarize("price", options="separator(0)")
    assert "separator(0)" in mock_run.call_args[0][0]

    with patch("server._run_stata_command") as mock_run:
        stata_codebook("price", options="tabulate(5)")
    assert "tabulate(5)" in mock_run.call_args[0][0]

    with patch("server._run_stata_command") as mock_run:
        stata_describe("price", options="fullnames")
    assert "fullnames" in mock_run.call_args[0][0]


def test_options_escape_hatch_rejects_injection():
    for fn, args in ((stata_list, ("price",)), (stata_tabulate, ("rep78",)),
                     (stata_summarize, ("price",)), (stata_describe, ("price",))):
        with patch("server._run_stata_command") as mock_run:
            result = fn(*args, options="clean; shell evil")
        assert getattr(result, "is_error", False), fn.__name__
        mock_run.assert_not_called()


def test_in_range_is_validated_against_injection():
    with patch("server._run_stata_command") as mock_run:
        result = stata_regress("price", "weight", in_range="1/40; shell evil")
    assert getattr(result, "is_error", False)
    mock_run.assert_not_called()


def test_list_emits_in_clause_exactly_once():
    """stata_list 自带 in/n 逻辑，不能与通用的 _filter_clause 叠加出两个 in。

    旧断言只查 "in 1/20" 是否出现，`list … in 1/20 in 1/20` 照样通过 ——
    这里改为计数。
    """
    with patch("server._run_stata_command") as mock_run:
        stata_list("price", n=10, in_range="1/20", condition="foreign==1")
    cmd = mock_run.call_args[0][0]
    assert cmd == "list price if foreign==1 in 1/20", cmd
    assert cmd.count(" in ") == 1


def test_list_falls_back_to_n_when_no_in_range():
    with patch("server._run_stata_command") as mock_run:
        stata_list("price", n=5)
    assert mock_run.call_args[0][0] == "list price in 1/5"


def test_generate_and_egen_support_storage_type():
    """官方语法是 `generate [type] newvar = exp`；float 默认会损失精度。"""
    with patch("server._run_stata_command") as mock_run:
        stata_generate("logp", "ln(price)", vartype="double")
    assert mock_run.call_args[0][0].startswith("generate double logp =")

    with patch("server._run_stata_command") as mock_run:
        stata_egen("m", "mean(price)", vartype="double")
    assert "egen double m = mean(price)" in mock_run.call_args[0][0]


def test_generate_rejects_unknown_storage_type():
    with patch("server._run_stata_command") as mock_run:
        result = stata_generate("x", "1", vartype="decimal")
    assert getattr(result, "is_error", False)
    mock_run.assert_not_called()


def test_generate_and_egen_have_options_escape_hatch():
    with patch("server._run_stata_command") as mock_run:
        stata_generate("x", "1", options="before(price)")
    assert mock_run.call_args[0][0].endswith(", before(price)")

    with patch("server._run_stata_command") as mock_run:
        stata_egen("r", "rank(price)", options="field")
    assert mock_run.call_args[0][0].endswith(", field")


def test_test_tool_has_options_escape_hatch():
    """test 的官方选项：mtest / accumulate / notest / common / df()。"""
    with patch("server._run_stata_command") as mock_run:
        stata_test("weight mpg", options="mtest")
    assert mock_run.call_args[0][0] == "test weight mpg, mtest"


def test_use_and_save_have_options_escape_hatch():
    with patch("server._run_stata_command") as mock_run, \
         patch("server.os.path.isfile", return_value=True):
        stata_use_dataset(abs_path("d", "a.dta"), options="nolabel")
    assert "nolabel" in mock_run.call_args[0][0]

    with patch("server._run_stata_command") as mock_run:
        stata_save_dataset(abs_path("d", "a.dta"), replace=True, options="orphans")
    cmd = mock_run.call_args[0][0]
    assert "replace" in cmd and "orphans" in cmd


def test_new_options_are_validated_against_injection():
    with patch("server._run_stata_command") as mock_run:
        assert getattr(stata_test("weight", options="mtest; shell x"), "is_error", False)
        assert getattr(stata_generate("x", "1", options="a; shell x"), "is_error", False)
        assert getattr(stata_egen("y", "mean(p)", options="a; shell x"), "is_error", False)
        mock_run.assert_not_called()


# --- ttest 的四种官方形式 -----------------------------------------------------
# 实测：裸 `ttest price` 报 by() option required → r(100)。旧实现在不传 byvar
# 时正是生成这种非法命令，而单元测试只比对字符串，完全没发现。


def test_ttest_one_sample_against_value():
    """单样本：`ttest varname == # [if] [in]`。"""
    with patch("server._run_stata_command") as mock_run:
        stata_ttest("price", compare_to="5000")
    assert mock_run.call_args[0][0] == "ttest price == 5000"


def test_ttest_paired_against_variable():
    """配对：`ttest varname1 == varname2`。"""
    with patch("server._run_stata_command") as mock_run:
        stata_ttest("price", compare_to="mpg")
    assert mock_run.call_args[0][0] == "ttest price == mpg"


def test_ttest_unpaired_two_sample():
    with patch("server._run_stata_command") as mock_run:
        stata_ttest("price", compare_to="mpg", options="unpaired")
    assert mock_run.call_args[0][0] == "ttest price == mpg, unpaired"


def test_ttest_by_group_still_works():
    with patch("server._run_stata_command") as mock_run:
        stata_ttest("price", byvar="foreign", in_range="1/40")
    assert mock_run.call_args[0][0] == "ttest price in 1/40, by(foreign)"


def test_ttest_requires_byvar_or_compare_to():
    """两者都不给会拼出 `ttest price` —— Stata 报 by() option required。

    与其把非法命令发出去，不如在入口说明该给什么。
    """
    with patch("server._run_stata_command") as mock_run:
        result = stata_ttest("price")
    text = _result_text(result)
    assert getattr(result, "is_error", False)
    assert "compare_to" in text and "byvar" in text
    mock_run.assert_not_called()


def test_ttest_rejects_byvar_with_compare_to():
    """两种形式互斥，同时给出会拼出无效语法。"""
    with patch("server._run_stata_command") as mock_run:
        result = stata_ttest("price", byvar="foreign", compare_to="5000")
    assert getattr(result, "is_error", False)
    mock_run.assert_not_called()


def test_ttest_rejects_injected_compare_to():
    with patch("server._run_stata_command") as mock_run:
        result = stata_ttest("price", compare_to="5000; shell evil")
    assert getattr(result, "is_error", False)
    mock_run.assert_not_called()


# ============================================================================
# 会话状态感知 —— stata_status 的覆盖面
# ============================================================================


def _status_cmd():
    from server import stata_status

    with patch("server._run_stata_command") as mock_run:
        stata_status()
    return mock_run.call_args[0][0]


def test_status_reports_panel_and_timeseries_setting():
    """stata_xtreg 要求先 xtset，Agent 必须能查到设定状态。

    裸 `xtset` 在未设定时报 r(459)，故必须 capture —— 但要 noisily 保留
    "panel variable not set" 这句诊断，它本身就是有用的状态信息。

    只发 xtset：实测它对纯时序数据也照报 "Time variable: …"，与 tsset 的输出
    逐字相同，两条都发只会把同一段打两遍。
    """
    cmd = _status_cmd()
    assert "capture noisily xtset" in cmd
    assert "tsset" not in cmd, "与 xtset 输出重复，不该同时发"


def test_status_reports_frames():
    """Stata 16+ 可同时持有多个数据集；只报「当前数据集」会漏掉其余。"""
    cmd = _status_cmd()
    assert "frame dir" in cmd
    assert "c(frame)" in cmd


def test_status_reports_stored_estimates():
    """margins / test / predict 都依赖已存在的估计结果。"""
    cmd = _status_cmd()
    assert "estimates dir" in cmd
    assert "e(cmd)" in cmd


def test_status_still_reports_dataset_and_cwd():
    cmd = _status_cmd()
    assert "describe, short" in cmd
    assert "c(pwd)" in cmd


def test_status_never_uses_bare_cd():
    """裸 cd 会切到 home 目录 —— 标注只读的工具不能悄悄改工作目录。"""
    cmd = _status_cmd()
    for line in cmd.splitlines():
        assert line.strip() != "cd", f"出现裸 cd: {cmd}"


# ============================================================================
# stata_import —— 与 export 对称的导入命令族
# ============================================================================


def _import(**kw):
    from server import stata_import

    with patch("server._run_stata_command", return_value="ok") as mock_run, \
         patch("server.os.path.isfile", return_value=True):
        result = stata_import(**kw)
    return result, (mock_run.call_args[0][0] if mock_run.call_args else None)


def test_import_detects_format_from_extension():
    """扩展名 → 官方命令的映射（[D] import 的方法表）。"""
    cases = [
        ("data.xlsx", "import excel"), ("data.xls", "import excel"),
        ("data.csv", "import delimited"), ("data.tsv", "import delimited"),
        ("data.txt", "import delimited"),
        ("data.sas7bdat", "import sas"), ("data.sav", "import spss"),
        ("data.zsav", "import spss"), ("data.dbf", "import dbase"),
        ("data.parquet", "import parquet"),
    ]
    for fname, expected in cases:
        _r, cmd = _import(filepath=abs_path("d", fname))
        assert cmd.startswith(expected), f"{fname} 应走 {expected}，实际 {cmd}"


def test_import_dta_points_to_use_dataset():
    """.dta 不属于 import 命令族 —— 该用 use，明确指路而不是拼个错命令。"""
    result, cmd = _import(filepath=abs_path("d", "a.dta"))
    text = _result_text(result)
    assert getattr(result, "is_error", False)
    assert "stata_use_dataset" in text
    assert cmd is None


def test_import_rejects_unknown_extension():
    result, cmd = _import(filepath=abs_path("d", "a.zzz"))
    assert getattr(result, "is_error", False)
    assert cmd is None


def test_import_explicit_format_overrides_extension():
    _r, cmd = _import(filepath=abs_path("d", "weird.dat"), format="delimited")
    assert cmd.startswith("import delimited")


def test_import_explicit_format_resolves_default_extension(tmp_path):
    from server import stata_import

    base = tmp_path / "data"
    actual = base.with_suffix(".csv")
    actual.write_text("id\n1\n", encoding="utf-8")
    with patch("server._run_stata_command", return_value="ok") as mock_run:
        stata_import(filepath=str(base), format="delimited")
    cmd = mock_run.call_args[0][0]
    assert f'using "{actual}"' in cmd
    assert mock_run.call_args.kwargs["require_file"] == str(actual)


def test_import_explicit_delimited_resolves_dat_extension(tmp_path):
    from server import stata_import

    base = tmp_path / "data"
    actual = base.with_suffix(".dat")
    actual.write_text("id\n1\n", encoding="utf-8")
    with patch("server._run_stata_command", return_value="ok") as mock_run:
        stata_import(filepath=str(base), format="delimited")
    cmd = mock_run.call_args[0][0]
    assert f'using "{actual}"' in cmd
    assert mock_run.call_args.kwargs["require_file"] == str(actual)


def test_import_excel_options():
    _r, cmd = _import(filepath=abs_path("d", "a.xlsx"), sheet="Q1",
                      cellrange="A1:C10", firstrow=True, case="lower")
    assert 'sheet("Q1")' in cmd
    assert "cellrange(A1:C10)" in cmd
    assert "firstrow" in cmd
    assert "case(lower)" in cmd


def test_import_delimited_options():
    _r, cmd = _import(filepath=abs_path("d", "a.csv"), delimiter=";",
                      varnames="1", encoding="utf-8")
    assert 'delimiters(";")' in cmd
    assert "varnames(1)" in cmd
    assert 'encoding("utf-8")' in cmd


def test_import_sas_and_spss_forward_encoding():
    for fname, fmt in (("a.sas7bdat", "sas"), ("a.sav", "spss")):
        _r, cmd = _import(filepath=abs_path("d", fname), format=fmt, encoding="gbk")
        assert 'encoding("gbk")' in cmd


def test_import_delimited_tab_keyword():
    _r, cmd = _import(filepath=abs_path("d", "a.tsv"), delimiter="tab")
    assert "delimiters(tab)" in cmd


def test_import_rejects_control_character_delimiter():
    result, cmd = _import(filepath=abs_path("d", "a.csv"), delimiter="\n")
    assert getattr(result, "is_error", False)
    assert cmd is None


def test_import_drops_options_not_applicable_to_format():
    """firstrow 只属于 excel，delimiters 只属于 delimited —— 传错会 r(198)。"""
    result, cmd = _import(filepath=abs_path("d", "a.csv"), firstrow=True)
    assert "firstrow" not in cmd
    assert "firstrow" in _result_text(result) and "不支持" in _result_text(result)

    result2, cmd2 = _import(filepath=abs_path("d", "a.xlsx"), delimiter=";")
    assert "delimiters" not in cmd2
    assert "delimiter" in _result_text(result2)


def test_import_clear_defaults_true_and_can_be_disabled():
    _r, cmd = _import(filepath=abs_path("d", "a.csv"))
    assert "clear" in cmd
    _r2, cmd2 = _import(filepath=abs_path("d", "a.csv"), clear=False)
    assert "clear" not in cmd2


def test_import_sas_and_spss_support_namelist_and_filters():
    """官方语法：`import sas [namelist] [if] [in] using filename`。

    注意 [if] [in] 在 **using 之前** —— 与 export 命令族相反。
    """
    for fname, head in (("a.sas7bdat", "import sas"), ("a.sav", "import spss")):
        _r, cmd = _import(filepath=abs_path("d", fname), varlist="id wage",
                          condition="wage > 0", in_range="1/100")
        assert cmd.startswith(f"{head} id wage if wage > 0 in 1/100 using"), cmd


def test_import_varlist_is_not_applied_to_excel_or_delimited():
    """同一语法位置在 excel/delimited 是 extvarlist（给列**命名**），不是筛选。

    当筛选用会静默导入错的数据，故丢弃并说明语义差异。
    """
    for fname in ("a.xlsx", "a.csv"):
        result, cmd = _import(filepath=abs_path("d", fname), varlist="id wage")
        assert "id wage" not in cmd, cmd
        assert "extvarlist" in _result_text(result)


def test_import_validates_path_and_options():
    result, cmd = _import(filepath=abs_path("d", "a.csv; shell evil"))
    assert getattr(result, "is_error", False)
    assert cmd is None

    result2, cmd2 = _import(filepath=abs_path("d", "a.csv"), options="clear; shell evil")
    assert getattr(result2, "is_error", False)
    assert cmd2 is None


def test_import_rejects_bad_case_and_missing_file():
    result, _cmd = _import(filepath=abs_path("d", "a.csv"), case="Title")
    assert getattr(result, "is_error", False)

    # 文件存在性交给 require_file 在锁内用 Stata cwd 权威解析，
    # 与 stata_use_dataset / stata_run_do_file 同一机制。
    from server import stata_import

    with patch("server._run_stata_command", return_value="ok") as mock_run, \
         patch("server.os.path.isfile", return_value=True):
        stata_import(filepath=abs_path("d", "a.csv"))
    assert mock_run.call_args.kwargs.get("require_file") == abs_path("d", "a.csv")


# ============================================================================
# stata_xtset —— stata_xtreg 的前提条件
# ============================================================================


def _xtset(**kw):
    from server import stata_xtset

    with patch("server._run_stata_command", return_value="ok") as mock_run:
        result = stata_xtset(**kw)
    return result, (mock_run.call_args[0][0] if mock_run.call_args else None)


def test_xtset_declares_panel():
    _r, cmd = _xtset(panelvar="idcode", timevar="year")
    assert cmd == "xtset idcode year"


def test_xtset_panel_without_time_is_valid():
    """只声明面板维度（不含时间）也是官方允许的形式。"""
    _r, cmd = _xtset(panelvar="idcode")
    assert cmd == "xtset idcode"


def test_tsset_when_only_timevar_given():
    """纯时序数据用 tsset —— 没有面板维度时 xtset 语义不对。"""
    _r, cmd = _xtset(timevar="date")
    assert cmd == "tsset date"


def test_xtset_show_queries_current_setting():
    """裸 xtset 是查询；未设定时报 r(459)，故须 capture noisily 保留诊断。"""
    _r, cmd = _xtset(action="show")
    assert cmd == "capture noisily xtset"


def test_xtset_clear_removes_declaration():
    _r, cmd = _xtset(action="clear")
    assert cmd == "xtset, clear"


def test_xtset_supports_delta_and_format():
    _r, cmd = _xtset(panelvar="id", timevar="t", options="delta(1) format(%ty)")
    assert cmd == "xtset id t, delta(1) format(%ty)"


def test_xtset_requires_at_least_one_variable():
    result, cmd = _xtset()
    text = _result_text(result)
    assert getattr(result, "is_error", False)
    assert "panelvar" in text and "timevar" in text
    assert cmd is None


def test_xtset_rejects_bad_identifiers_and_action():
    for kw in ({"panelvar": "id; shell x"}, {"timevar": "t | evil"},
               {"panelvar": "id", "options": "delta(1); shell x"}):
        result, cmd = _xtset(**kw)
        assert getattr(result, "is_error", False), kw
        assert cmd is None
    result, cmd = _xtset(action="destroy")
    assert getattr(result, "is_error", False)
    assert cmd is None


# ============================================================================
# P1 —— estat 诊断族 / estimates 结果管理 / 示例数据集
# ============================================================================


def _call(tool_name, **kw):
    import server

    fn = getattr(server, tool_name)
    with patch("server._run_stata_command", return_value="ok") as mock_run:
        result = fn(**kw)
    return result, (mock_run.call_args[0][0] if mock_run.call_args else None)


def test_estat_builds_subcommand():
    for sub in ("vif", "hettest", "ovtest", "ic", "summarize", "firststage"):
        _r, cmd = _call("stata_estat", subcommand=sub)
        assert cmd == f"estat {sub}"


def test_estat_passes_options():
    _r, cmd = _call("stata_estat", subcommand="hettest", options="rhs iid")
    assert cmd == "estat hettest, rhs iid"


def test_estat_rejects_empty_and_injected_subcommand():
    for sub in ("", "vif; shell evil", "vif | x"):
        result, cmd = _call("stata_estat", subcommand=sub)
        assert getattr(result, "is_error", False), sub
        assert cmd is None


def test_estimates_store_and_restore():
    _r, cmd = _call("stata_estimates", action="store", name="m1")
    assert cmd == "estimates store m1"
    _r, cmd = _call("stata_estimates", action="restore", name="m1")
    assert cmd == "estimates restore m1"


def test_estimates_table_accepts_multiple_names():
    _r, cmd = _call("stata_estimates", action="table", name="m1 m2 m3",
                    options="star stats(N r2)")
    assert cmd == "estimates table m1 m2 m3, star stats(N r2)"


def test_estimates_dir_and_clear_take_no_name():
    _r, cmd = _call("stata_estimates", action="dir")
    assert cmd == "estimates dir"
    _r, cmd = _call("stata_estimates", action="clear")
    assert cmd == "estimates clear"


def test_estimates_store_requires_name():
    result, cmd = _call("stata_estimates", action="store")
    assert getattr(result, "is_error", False)
    assert cmd is None


def test_estimates_rejects_unknown_action():
    result, cmd = _call("stata_estimates", action="publish", name="m1")
    assert getattr(result, "is_error", False)
    assert cmd is None


def test_use_example_sysuse_and_webuse():
    _r, cmd = _call("stata_use_example", name="auto")
    assert cmd == "sysuse auto, clear"
    _r, cmd = _call("stata_use_example", name="nlswork", source="webuse")
    assert cmd == "webuse nlswork, clear"


def test_use_example_can_keep_existing_data():
    _r, cmd = _call("stata_use_example", name="auto", clear=False)
    assert cmd == "sysuse auto"


def test_use_example_lists_available_datasets():
    """sysuse dir 列出随 Stata 分发的数据集；webuse 没有 dir 子命令。"""
    _r, cmd = _call("stata_use_example", action="list")
    assert cmd == "sysuse dir"


def test_use_example_rejects_bad_source_and_name():
    result, cmd = _call("stata_use_example", name="auto", source="ftp")
    assert getattr(result, "is_error", False)
    assert cmd is None
    result, cmd = _call("stata_use_example", name="auto; shell evil")
    assert getattr(result, "is_error", False)
    assert cmd is None


# ============================================================================
# P2 —— 数据重构四大件 / 返回值列表
# ============================================================================


def test_merge_builds_official_syntax():
    """官方：`merge 1:1|m:1|1:m|m:m varlist using filename [, options]`。"""
    _r, cmd = _call("stata_merge", kind="1:1", keyvars="id year",
                    using=abs_path("d", "b.dta"))
    assert cmd == f'merge 1:1 id year using "{abs_path("d", "b.dta")}"'


def test_merge_supports_all_official_kinds():
    for kind in ("1:1", "m:1", "1:m", "m:m"):
        _r, cmd = _call("stata_merge", kind=kind, keyvars="id",
                        using=abs_path("d", "b.dta"))
        assert cmd.startswith(f"merge {kind} id using")


def test_merge_by_observation_uses_underscore_n():
    """官方还支持按观测号合并：`merge 1:1 _n using filename`。"""
    _r, cmd = _call("stata_merge", kind="1:1", keyvars="_n",
                    using=abs_path("d", "b.dta"))
    assert "merge 1:1 _n using" in cmd


def test_merge_options_and_keep_filter():
    _r, cmd = _call("stata_merge", kind="m:1", keyvars="id",
                    using=abs_path("d", "b.dta"),
                    keepusing="wage educ", options="keep(match master) nogenerate")
    assert "keepusing(wage educ)" in cmd
    assert "keep(match master) nogenerate" in cmd


def test_merge_rejects_unsupported_if_and_in_clauses():
    for kw in (
        {"condition": "foreign == 1"},
        {"in_range": "1/100"},
    ):
        result, cmd = _call(
            "stata_merge",
            kind="1:1",
            keyvars="id",
            using=abs_path("d", "b.dta"),
            **kw,
        )
        assert getattr(result, "is_error", False), kw
        assert "不支持" in _result_text(result)
        assert cmd is None


def test_merge_rejects_unknown_kind():
    result, cmd = _call("stata_merge", kind="1:n", keyvars="id",
                        using=abs_path("d", "b.dta"))
    assert getattr(result, "is_error", False)
    assert cmd is None


def test_merge_requires_keyvars_and_using():
    for kw in ({"kind": "1:1", "keyvars": "", "using": abs_path("d", "b.dta")},
               {"kind": "1:1", "keyvars": "id", "using": ""}):
        result, cmd = _call("stata_merge", **kw)
        assert getattr(result, "is_error", False), kw
        assert cmd is None


def test_append_accepts_multiple_files():
    a, b = abs_path("d", "a.dta"), abs_path("d", "b.dta")
    with patch("server.os.path.isfile", return_value=True):
        _r, cmd = _call("stata_append", using=f"{a} {b}", options="generate(src)")
    assert f'append using "{a}" "{b}"' in cmd
    assert "generate(src)" in cmd


def test_append_requires_using():
    result, cmd = _call("stata_append", using="")
    assert getattr(result, "is_error", False)
    assert cmd is None


def test_reshape_long_and_wide():
    _r, cmd = _call("stata_reshape", direction="long", stub="inc",
                    i="id", j="year")
    assert cmd == "reshape long inc, i(id) j(year)"
    _r, cmd = _call("stata_reshape", direction="wide", stub="inc",
                    i="id", j="year")
    assert cmd == "reshape wide inc, i(id) j(year)"


def test_reshape_j_optional_for_wide_when_string():
    _r, cmd = _call("stata_reshape", direction="long", stub="inc", i="id",
                    j="year", options="string")
    assert cmd == "reshape long inc, i(id) j(year) string"


def test_reshape_requires_direction_stub_and_i():
    for kw in ({"direction": "sideways", "stub": "inc", "i": "id"},
               {"direction": "long", "stub": "", "i": "id"},
               {"direction": "long", "stub": "inc", "i": ""}):
        result, cmd = _call("stata_reshape", **kw)
        assert getattr(result, "is_error", False), kw
        assert cmd is None


def test_reshape_wide_requires_j_variable():
    result, cmd = _call("stata_reshape", direction="wide", stub="inc", i="id")
    assert getattr(result, "is_error", False)
    assert "必须提供 j" in _result_text(result)
    assert cmd is None


def test_collapse_builds_stat_list():
    _r, cmd = _call("stata_collapse", clist="(mean) price (sd) mpg", by="foreign")
    assert cmd == "collapse (mean) price (sd) mpg, by(foreign)"


def test_collapse_supports_filters_and_options():
    _r, cmd = _call("stata_collapse", clist="(sum) sales", by="firm year",
                    condition="year >= 2000", in_range="1/500", options="cw")
    assert cmd == ("collapse (sum) sales if year >= 2000 in 1/500, "
                   "by(firm year) cw")


def test_collapse_requires_clist():
    result, cmd = _call("stata_collapse", clist="")
    assert getattr(result, "is_error", False)
    assert cmd is None


def test_return_list_covers_three_namespaces():
    for kind, expected in (("r", "return list"), ("e", "ereturn list"),
                           ("c", "creturn list")):
        _r, cmd = _call("stata_return_list", kind=kind)
        assert cmd == expected


def test_return_list_defaults_to_r_and_rejects_unknown():
    _r, cmd = _call("stata_return_list")
    assert cmd == "return list"
    result, cmd = _call("stata_return_list", kind="x")
    assert getattr(result, "is_error", False)
    assert cmd is None


# ============================================================================
# P3 —— frames 管理 / 数据校验族
# ============================================================================


def test_frame_dir_and_current():
    _r, cmd = _call("stata_frame", action="dir")
    assert cmd == "frames dir"
    _r, cmd = _call("stata_frame", action="current")
    assert cmd == "frame pwf"


def test_frame_create_change_drop():
    _r, cmd = _call("stata_frame", action="create", name="alt")
    assert cmd == "frame create alt"
    _r, cmd = _call("stata_frame", action="change", name="alt")
    assert cmd == "frame change alt"
    _r, cmd = _call("stata_frame", action="drop", name="alt")
    assert cmd == "frame drop alt"


def test_frame_copy_and_rename_need_two_names():
    _r, cmd = _call("stata_frame", action="copy", name="a", newname="b")
    assert cmd == "frame copy a b"
    _r, cmd = _call("stata_frame", action="rename", name="a", newname="b")
    assert cmd == "frame rename a b"


def test_frame_actions_requiring_name_are_checked():
    for action in ("create", "change", "drop"):
        result, cmd = _call("stata_frame", action=action)
        assert getattr(result, "is_error", False), action
        assert cmd is None
    for action in ("copy", "rename"):
        result, cmd = _call("stata_frame", action=action, name="a")
        assert getattr(result, "is_error", False), action
        assert cmd is None


def test_frame_rejects_unknown_action_and_injected_name():
    result, cmd = _call("stata_frame", action="merge", name="a")
    assert getattr(result, "is_error", False)
    assert cmd is None
    result, cmd = _call("stata_frame", action="create", name="a; shell evil")
    assert getattr(result, "is_error", False)
    assert cmd is None


def test_verify_count_is_default():
    _r, cmd = _call("stata_verify")
    assert cmd == "count"


def test_verify_count_with_filters():
    _r, cmd = _call("stata_verify", check="count", condition="price > 10000",
                    in_range="1/100")
    assert cmd == "count if price > 10000 in 1/100"


def test_verify_duplicates_defaults_to_report():
    _r, cmd = _call("stata_verify", check="duplicates", varlist="id year")
    assert cmd == "duplicates report id year"


def test_verify_duplicates_subcommand_can_be_chosen():
    _r, cmd = _call("stata_verify", check="duplicates", varlist="id",
                    options="list")
    assert cmd == "duplicates list id"


def test_verify_isid_and_misstable():
    _r, cmd = _call("stata_verify", check="isid", varlist="id year")
    assert cmd == "isid id year"
    _r, cmd = _call("stata_verify", check="missing", varlist="price mpg")
    assert cmd == "misstable summarize price mpg"


def test_verify_assert_needs_an_expression():
    _r, cmd = _call("stata_verify", check="assert", expression="price > 0")
    assert cmd == "assert price > 0"
    result, cmd = _call("stata_verify", check="assert")
    assert getattr(result, "is_error", False)
    assert cmd is None


def test_verify_isid_needs_varlist():
    result, cmd = _call("stata_verify", check="isid")
    assert getattr(result, "is_error", False)
    assert cmd is None


def test_verify_rejects_unknown_check_and_injection():
    result, cmd = _call("stata_verify", check="checksum")
    assert getattr(result, "is_error", False)
    assert cmd is None
    result, cmd = _call("stata_verify", check="assert", expression="1; shell evil")
    assert getattr(result, "is_error", False)
    assert cmd is None


# ============================================================================
# 包搜索 —— net search 的官方选项
# ============================================================================


def test_find_package_default_is_plain_net_search():
    _r, cmd = _call("stata_find_package", keyword="binscatter")
    assert cmd == "net search binscatter"


def test_find_package_scope_narrows_results():
    """实测广词查询默认 94K 字符 / 24 页，scope="toc" 收窄到 12K。"""
    _r, cmd = _call("stata_find_package", keyword="did", scope="toc")
    assert cmd == "net search did, toc"


def test_find_package_supports_all_official_scopes():
    for scope in ("toc", "pkg", "tocpkg", "everywhere", "filenames"):
        _r, cmd = _call("stata_find_package", keyword="iv", scope=scope)
        assert cmd == f"net search iv, {scope}"


def test_find_package_match_any_and_exclude_sj():
    _r, cmd = _call("stata_find_package", keyword="panel data",
                    match_any=True, exclude_sj=True)
    assert "or" in cmd.split(", ")[1].split()
    assert "nosj" in cmd


def test_find_package_error_on_no_match_is_opt_in():
    """默认无匹配返回 isError=False（搜不到不是错误）；errnone 才转 rc=111。"""
    _r, cmd = _call("stata_find_package", keyword="zzz", error_if_none=True)
    assert "errnone" in cmd


def test_find_package_rejects_unknown_scope():
    result, cmd = _call("stata_find_package", keyword="iv", scope="deep")
    assert getattr(result, "is_error", False)
    assert cmd is None


def test_find_package_still_rejects_injection_and_empty():
    for kw in ("", "iv; shell evil"):
        result, cmd = _call("stata_find_package", keyword=kw)
        assert getattr(result, "is_error", False), kw
        assert cmd is None


# --- 含空格的文件路径 ----------------------------------------------------------
# _split_using_paths 按空白切分以支持 append 的多文件语法，代价是任何含空格的
# 路径都被劈成两半（`/Users/x/My Drive/…`、`C:/Program Files/…` 在真实系统上是
# 常态）。其余接路径的工具都用单参数 + 双引号包裹，完全支持空格 —— 只有这两个
# 工具不支持，且报出的错与真实原因无关（merge 报「只能接一个文件」，append 把
# 第二个碎片按 Python cwd 解析成另一个真实存在的路径）。


def test_merge_accepts_path_with_spaces():
    from server import stata_merge

    target = abs_path("tmp", "my data", "panel.dta")
    with (
        patch("server._run_stata_command") as mock_run,
        patch("server.os.path.isfile", return_value=True),
    ):
        stata_merge(kind="1:1", keyvars="id", using=target)
    cmd = mock_run.call_args[0][0]
    assert f'using "{target}"' in cmd
    assert mock_run.call_args.kwargs["require_file"] == target


def test_append_accepts_quoted_paths_with_spaces():
    from server import stata_append

    a = abs_path("tmp", "my data", "a.dta")
    b = abs_path("tmp", "b.dta")
    with (
        patch("server._run_stata_command") as mock_run,
        patch("server.os.path.isfile", return_value=True),
    ):
        stata_append(using=f'"{a}" "{b}"')
    cmd = mock_run.call_args[0][0]
    assert f'"{a}"' in cmd
    assert f'"{b}"' in cmd
    assert mock_run.call_args.kwargs["require_file"] == a


def test_append_rejects_any_missing_input_before_stata():
    from server import stata_append

    a = abs_path("tmp", "a.dta")
    b = abs_path("tmp", "missing.dta")
    with (
        patch("server._run_stata_command") as mock_run,
        patch("server.os.path.isfile", side_effect=lambda path: path == a),
    ):
        result = stata_append(using=f'"{a}" "{b}"')
    assert getattr(result, "is_error", False)
    assert "missing.dta" in _result_text(result)
    mock_run.assert_not_called()


def test_append_still_splits_plain_space_separated_paths():
    from server import stata_append

    a = abs_path("tmp", "a.dta")
    b = abs_path("tmp", "b.dta")
    with (
        patch("server._run_stata_command") as mock_run,
        patch("server.os.path.isfile", return_value=True),
    ):
        stata_append(using=f"{a} {b}")
    cmd = mock_run.call_args[0][0]
    assert f'"{a}"' in cmd and f'"{b}"' in cmd


def test_append_reports_unbalanced_quotes():
    from server import stata_append

    result = stata_append(using='"' + abs_path("tmp", "a.dta"))
    text = result.content[0].text if hasattr(result, "content") else result
    assert "引号" in text


def test_verify_rejects_mutating_duplicates_subcommands():
    """``stata_verify`` 标 readOnlyHint=True，就不能执行会改数据的子命令。

    ``duplicates drop`` 删除观测、``duplicates tag()`` 创建变量 —— 二者都是
    「修改」而非「校验」，而遵循 MCP 注解的客户端会对只读工具跳过确认，等于
    静默改数据。工具名即契约：把破坏性子命令挡在门外，比给一个「除非传某个
    选项否则只读」的工具更安全。
    """
    from server import stata_verify

    for sub in ("drop", "tag(dup)", "TAG(dup)", "  drop  "):
        result = stata_verify(check="duplicates", options=sub)
        text = result.content[0].text if hasattr(result, "content") else result
        assert "错误" in text, sub
        assert "stata_run" in text, sub


def test_verify_allows_read_only_duplicates_subcommands():
    from server import stata_verify

    for sub in ("", "report", "list", "examples"):
        with patch("server._run_stata_command") as mock_run:
            stata_verify(check="duplicates", options=sub)
        assert mock_run.call_args[0][0].startswith("duplicates ")


def test_install_package_clamps_timeout():
    """与 stata_run / stata_run_do_file 一致地夹在 10–1800 秒。

    此前完全未钳制：timeout=1 会架起 1 秒看门狗（而安装实测需 3–13 秒），
    timeout=10**6 则突破文档所称的 1800 秒上限。
    """
    from server import stata_install_package

    for given, expected in ((1, 10), (0, 10), (10**6, 1800), (120, 120)):
        with patch("server._run_stata_command") as mock_run:
            stata_install_package("estout", timeout=given)
        assert mock_run.call_args.kwargs["timeout"] == expected


def test_graph_export_preserves_named_graphs(tmp_path):
    """导出后只清匿名图，不能连用户具名的图一起 drop。

    `graph drop _all` 会摧毁多面板工作流：具名图正是「我要在后续命令里引用它」
    的显式表达，而 combine 之后再换个布局导出第二张就会发现图已经没了。
    真机确认（Stata 19.5 MP）：匿名图名为 `Graph`，`graph drop Graph` 只删它、
    具名图 `g2` 存活且 rc=0。
    """
    from server import stata_graph

    with patch("server._run_stata_command", return_value="ok") as mock_run:
        stata_graph(command="graph combine g1 g2", export=str(tmp_path / "c.png"))
    cmd = mock_run.call_args[0][0]
    assert "graph drop Graph" in cmd
    assert "graph drop _all" not in cmd


def test_run_do_file_keeps_error_output_multiline(tmp_path):
    """含 ssc install 的 do 文件执行失败时，错误输出不得被压成单行。

    _result_text_inline 是为并入**安装报告行**设计的（换行变 " | "），却被套在
    可达 120K 字符的 do 文件完整输出上：Stata 的错误上下文、表格、行号提示全被
    压成一条巨型单行。同一个 do 文件只要不含 ssc install 就走原路径、格式完好
    —— 一行 ssc install 的存在改变了错误报告的可读性。
    """
    from server import ToolResult, stata_run_do_file

    target = tmp_path / "s.do"
    target.write_text("ssc install estout\nregress bad\n", encoding="utf-8")

    failure = ToolResult(content="[返回码: 111]\nvariable bad not found\nr(111);")
    with (
        patch("server._prepare_ssc_installs", return_value=["  · estout: 已安装，跳过"]),
        patch("server._run_stata_command", return_value=failure),
    ):
        result = stata_run_do_file(str(target))

    text = result.content[0].text if hasattr(result, "content") else result
    assert "variable bad not found" in text
    assert " | " not in text
    assert text.count("\n") >= 3


def test_run_do_file_aborts_body_when_ssc_install_did_not_execute(tmp_path):
    from server import stata_run_do_file

    target = tmp_path / "s.do"
    target.write_text("ssc install estout\nregress price weight\n", encoding="utf-8")
    with (
        patch(
            "server._prepare_ssc_installs",
            return_value=["  · estout: 安装未完成（Stata 已自动恢复，请重试）"],
        ),
        patch("server._run_stata_command") as mock_run,
    ):
        result = stata_run_do_file(str(target))

    text = _result_text(result)
    assert getattr(result, "is_error", False)
    assert "脚本主体未执行" in text
    mock_run.assert_not_called()


# --- stata_etable：官方回归表导出 ------------------------------------------------
# 此前唯一的回归表导出路径是 stata_export_excel(results=True)：依赖第三方 estout，
# 且名叫 excel 却只能产出 CSV。Stata 17+ 自带的 etable 无需任何第三方包，直接产出
# .docx/.xlsx/.pdf/.tex —— 正是应用计量最常见的最后一步交付物。


def test_etable_active_estimates_minimal():
    from server import stata_etable

    with patch("server._run_stata_command", return_value="ok") as mock_run:
        stata_etable()
    assert mock_run.call_args[0][0] == "etable"


def test_etable_multiple_stored_models():
    from server import stata_etable

    with patch("server._run_stata_command", return_value="ok") as mock_run:
        stata_etable(estimates="m1 m2 m3", stars=True, stats="N r2")
    cmd = mock_run.call_args[0][0]
    assert "estimates(m1 m2 m3)" in cmd
    assert "showstars" in cmd and "showstarsnote" in cmd
    # 每个统计量各自一个 mstat()，这是官方语法
    assert "mstat(N)" in cmd and "mstat(r2)" in cmd


def test_etable_export_builds_export_option(tmp_path):
    from server import stata_etable

    target = tmp_path / "table.docx"
    with patch("server._run_stata_command", return_value="ok") as mock_run:
        stata_etable(estimates="m1", export=str(target), replace=True)
    cmd = mock_run.call_args[0][0]
    assert f'export("{target}", replace)' in cmd


def test_etable_export_omits_replace_by_default(tmp_path):
    from server import stata_etable

    target = tmp_path / "table.docx"
    with patch("server._run_stata_command", return_value="ok") as mock_run:
        stata_etable(export=str(target))
    assert f'export("{target}")' in mock_run.call_args[0][0]


@pytest.mark.parametrize("ext", [".csv", ".rtf", ".png"])
def test_etable_rejects_unsupported_export_formats(tmp_path, ext):
    """真机实测 .csv/.rtf 都是 r(198)，而错误会淹没在正常的表格输出里。"""
    from server import stata_etable

    result = stata_etable(export=str(tmp_path / f"t{ext}"))
    text = result.content[0].text if hasattr(result, "content") else result
    assert "错误" in text and ext in text


@pytest.mark.parametrize(
    "ext", [".docx", ".xlsx", ".html", ".pdf", ".tex", ".md", ".txt", ".xls", ".smcl"]
)
def test_etable_accepts_supported_export_formats(tmp_path, ext):
    """真机逐一验证过的 9 种格式。"""
    from server import stata_etable

    with patch("server._run_stata_command", return_value="ok") as mock_run:
        stata_etable(export=str(tmp_path / f"t{ext}"))
    assert "export(" in mock_run.call_args[0][0]


def test_etable_title_is_quoted():
    from server import stata_etable

    with patch("server._run_stata_command", return_value="ok") as mock_run:
        stata_etable(title="模型比较 (2024)")
    assert 'title("模型比较 (2024)")' in mock_run.call_args[0][0]


def test_etable_rejects_injection_in_estimates():
    from server import stata_etable

    result = stata_etable(estimates='m1) export("/evil/x.docx") //')
    text = result.content[0].text if hasattr(result, "content") else result
    assert "错误" in text
