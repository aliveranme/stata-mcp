from unittest.mock import patch

from fastmcp.tools.base import ToolResult

from server import (
    stata_codebook,
    stata_export_excel,
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


def test_export_excel_results_forces_csv():
    with (
        patch("server._run_stata_command") as mock_run,
        patch("server._execute_safe", return_value=(0, "D:\\ado\\plus\\e\\estout.ado")),
        patch("server.os.path.isfile", return_value=True),
        patch("server.os.path.getsize", return_value=1024),
    ):
        result = stata_export_excel("C:/output/results.xlsx", results=True)
        cmd = mock_run.call_args[0][0]
        assert 'esttab using "C:/output/results.csv"' in cmd
        assert ", csv " in cmd
        assert "提示：" in result


def test_export_excel_results_aborts_when_estout_missing():
    """estout 未安装时，results=True 应直接报错，不内嵌 ssc install（防 headless 卡死）。"""
    with (
        patch("server._run_stata_command") as mock_run,
        patch("server._execute_safe", return_value=(111, "command estout is unrecognized")),
    ):
        result = stata_export_excel("C:/output/results.csv", results=True)
        assert "错误" in _result_text(result)
        assert "estout" in _result_text(result)
        mock_run.assert_not_called()


def test_export_excel_results_aborts_on_dll_dead():
    """DLL 无响应（rc=998）时探测应中止，不执行 esttab。"""
    with (
        patch("server._run_stata_command") as mock_run,
        patch("server._execute_safe", return_value=(998, "[错误] Stata DLL 无响应")),
    ):
        result = stata_export_excel("C:/output/results.csv", results=True)
        assert getattr(result, "is_error", False) or "错误" in _result_text(result)
        mock_run.assert_not_called()


def test_export_excel_dataset_uses_excel():
    with (
        patch("server._run_stata_command") as mock_run,
        patch("server.os.path.isfile", return_value=True),
        patch("server.os.path.getsize", return_value=1024),
    ):
        stata_export_excel("C:/output/data.xlsx", varlist="mpg price", sheet="Data", replace=True)
        cmd = mock_run.call_args[0][0]
        assert (
            cmd
            == 'export excel mpg price using "C:/output/data.xlsx", replace firstrow(variables) sheet("Data")'
        )


def test_graph_export_includes_height_when_set():
    with (
        patch("server._run_stata_command") as mock_run,
        patch("server.os.path.isfile", return_value=True),
        patch("server.os.path.getsize", return_value=1024),
    ):
        from server import stata_graph

        stata_graph("scatter price weight", export="C:/output/fig.png", width=1200, height=800)
        cmd = mock_run.call_args[0][0]
        assert 'graph export "C:/output/fig.png"' in cmd
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

        stata_graph('twoway scatter price weight, title("a} b")', export="C:/output/fig.png")
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
        stata_save_dataset("C:/output/data.dta", replace=True)
        cmd = mock_run.call_args[0][0]
        assert cmd == 'save "C:/output/data.dta", replace'


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
        stata_use_dataset("C:/data/auto.dta", clear=True)
        cmd = mock_run.call_args[0][0]
        assert 'use "C:/data/auto.dta", clear' == cmd
        assert mock_run.call_args.kwargs["require_file"] == "C:/data/auto.dta"


def test_stata_use_dataset_without_clear():
    with patch("server._run_stata_command") as mock_run:
        stata_use_dataset("C:/data/auto.dta", clear=False)
        cmd = mock_run.call_args[0][0]
        assert cmd == 'use "C:/data/auto.dta"'


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
