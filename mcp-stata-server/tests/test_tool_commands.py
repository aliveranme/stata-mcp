from unittest.mock import patch

from conftest import abs_path
from fastmcp.tools.base import ToolResult

from server import (
    _ESTOUT_PROBE_CMD,
    stata_codebook,
    stata_export_excel,
    stata_find_package,
    stata_graph,
    stata_install_package,
    stata_list,
    stata_logistic,
    stata_ping,
    stata_regress,
    stata_run,
    stata_save_dataset,
    stata_summarize,
    stata_tabulate,
    stata_ttest,
    stata_use_dataset,
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


def test_ttest_without_byvar():
    with patch("server._run_stata_command") as mock_run:
        stata_ttest("price", options="level(90)")
        cmd = mock_run.call_args[0][0]
        assert cmd == "ttest price, level(90)"


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


def test_install_package_net_url():
    with patch("server._run_stata_command") as mock_run:
        stata_install_package("somepkg", source="https://example.com/pkg")
        cmd = mock_run.call_args[0][0]
        assert cmd == "net install somepkg, from(https://example.com/pkg)"


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
    """estout 未安装时，results=True 应直接报错，不内嵌 ssc install（防 headless 卡死）。"""
    with (
        patch("server._run_stata_command") as mock_run,
        patch("server._execute_safe", return_value=_PROBE_MISSING) as mock_probe,
    ):
        result = stata_export_excel(abs_path("output", "results.csv"), results=True)
        assert mock_probe.call_args[0][0] == _ESTOUT_PROBE_CMD
        text = _result_text(result)
        assert getattr(result, "is_error", False)
        assert "未安装 estout" in text
        assert "stata_install_package" in text
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


def test_stata_ping_degraded_when_no_42():
    with patch("server._execute_single", return_value=(0, "")):
        result = stata_ping()
    assert "degraded" in result


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
