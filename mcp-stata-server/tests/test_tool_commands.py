from unittest.mock import patch

from fastmcp.tools.base import ToolResult

from server import (
    stata_codebook,
    stata_export_excel,
    stata_graph,
    stata_install_package,
    stata_list,
    stata_logistic,
    stata_regress,
    stata_save_dataset,
    stata_summarize,
    stata_tabulate,
    stata_ttest,
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
        patch("server.os.path.isfile", return_value=True),
        patch("server.os.path.getsize", return_value=1024),
    ):
        result = stata_export_excel("C:/output/results.xlsx", results=True)
        cmd = mock_run.call_args[0][0]
        assert 'esttab using "C:/output/results.csv"' in cmd
        assert ", csv " in cmd
        assert "提示：" in result


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
