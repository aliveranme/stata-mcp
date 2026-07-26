"""Tests for input validation helpers."""

import pytest

from server import (
    _has_dangerous_command_prefix,
    _validate_command_blocks,
    _validate_identifier,
    _validate_no_injection,
    _validate_scheme_name,
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
        # winexec 在 Windows 上直接启动程序，与 shell 等价
        "winexec notepad.exe",
        # Mata 是可执行任意代码的子语言：_stata() 能调用任意 Stata 命令（含 !），
        # unlink()/fopen() 能直接读写文件。行首前缀检查对块内代码无效，
        # 实测 `mata:` + `_stata("display 12345")` 曾原样穿过并执行成功。
        "mata:",
        "mata: 6*7",
        "mata",
        'mata:\n_stata("!rm -rf /")\nend',
        "mata drop _all",
        "summarize mpg\nmata:\n_stata(\"display 1\")\nend",
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
        # 以 mata 起头的普通字符串不应误伤
        'display "matador"',
        "generate matador = 1",
    ],
)
def test_dangerous_command_prefix_allows_safe(cmd):
    assert _has_dangerous_command_prefix(cmd) is None


@pytest.mark.parametrize(
    "cmd",
    [
        # Stata 的通用前缀命令可套在任意命令前，加上之后行首不再是危险词。
        # 真机验证（Stata 19.5 MP）：`capture shell touch <f>` 与
        # `quietly mata: _stata("shell touch <f>")` 都真实创建了文件，
        # `capture noisily mata: _stata("display 999888")` 打印了 999888。
        "capture shell touch /tmp/x",
        "cap shell touch /tmp/x",
        "quietly shell rm -f /tmp/x",
        "qui shell echo hi",
        "noisily !touch /tmp/x",
        "capture noisily shell echo hi",
        'quietly mata: _stata("shell touch /tmp/x")',
        'cap mata: unlink("/tmp/x")',
        "qui python: import os",
        # 带冒号的前缀（by/bysort/version/svy/xi）同理
        "by foreign: shell echo hi",
        "bysort foreign: shell echo hi",
        "version 17: shell echo hi",
        # 前缀叠加
        "capture quietly noisily shell echo hi",
    ],
)
def test_dangerous_prefix_blocks_behind_stata_prefix_commands(cmd):
    """通用前缀不改变被执行的命令，护栏必须先剥前缀再判行首。"""
    assert _has_dangerous_command_prefix(cmd) is not None


@pytest.mark.parametrize(
    "cmd",
    [
        # 剥前缀不能误伤合法命令
        "capture noisily regress price weight",
        "quietly summarize mpg",
        "by foreign: summarize price",
        "bysort foreign: egen m = mean(price)",
        "capture drop matador",
        # 扩展宏函数里的顶层冒号：剥掉后仍是安全命令
        "local n : word count `varlist'",
        # 冒号在字符串内，不是前缀分隔符
        'display "ratio 1:2"',
    ],
)
def test_dangerous_prefix_allows_safe_behind_prefix(cmd):
    assert _has_dangerous_command_prefix(cmd) is None


@pytest.mark.parametrize(
    "cmd",
    [
        # #delimit ; 让分号成为命令分隔符，于是 ! 永远不在行首。
        # 真机验证：`#delimit ;` + `display 3 ; !touch <f> ;` 真实创建了文件。
        "#delimit ;\ndisplay 2 ; !touch /tmp/x ;",
        "display 1 ; shell echo hi ;",
    ],
)
def test_dangerous_prefix_blocks_after_semicolon(cmd):
    """分隔符可被 #delimit 改成 `;`，护栏须对分号切分后的每段做行首匹配。"""
    assert _has_dangerous_command_prefix(cmd) is not None


def test_dangerous_prefix_allows_semicolon_inside_string():
    """字符串内的分号不是命令分隔符，不应触发误报。"""
    assert _has_dangerous_command_prefix('display "a; shell b"') is None


@pytest.mark.parametrize(
    "cmd",
    [
        "#delimit ;\nsysuse auto, clear ;",
        "  #delimit;\ndisplay 1 ;",
        "#DELIMIT ;",
    ],
)
def test_precheck_rejects_delimit_change(cmd):
    """``#delimit ;`` 把命令分隔符改成分号，行导向的解析器无法跟踪。

    真机验证：`#delimit ;` 脚本经 ``_parse_command_blocks`` 被逐行切成碎块，
    `regress price weight` 与续行 `  mpg ;` 被劈成两条独立命令 —— 少跑一个
    回归元却各自「成功」。与其静默给出另一个模型，不如在入口报错并指向
    ``stata_run_do_file``（do 文件由 Stata 自己解析，原生支持 #delimit）。
    """
    from server import _precheck_command

    reason = _precheck_command(cmd)
    assert reason is not None
    assert "#delimit" in reason
    assert "stata_run_do_file" in reason


def test_precheck_allows_delimit_word_inside_string():
    """字符串里出现 #delimit 字样不应误伤。"""
    from server import _precheck_command

    assert _precheck_command('display "use #delimit ; in do files"') is None


@pytest.mark.parametrize(
    "value",
    [
        # `//` 注释掉命令余下部分（含已校验的路径 / 选项）
        '1 using "/tmp/secret/x.sas7bdat" //',
        "price > 0 // rest is gone",
        # `/* */` 同样能吞掉后续文本
        "price > 0 /* using",
        "1 */ using",
        # 独立的 using 会引入第二个文件路径
        'foreign == 1 using "/tmp/evil.dta"',
        # 未闭合的双引号会把后续的 using "路径" 吞成字符串内容
        'make == "Honda',
    ],
)
def test_validate_filter_expr_rejects_path_rewrites(value):
    """``if`` / ``in`` 子句会与已校验路径拼在同一条命令里，须拒绝改写路径的记号。

    实测（STATA_ALLOWED_ROOTS=/tmp/allowed）：
    ``stata_import(filepath="/tmp/allowed/ok.sas7bdat", format="sas",
    condition='1 using "/tmp/secret/x.sas7bdat" //')``
    拼出 ``import sas if 1 using "/tmp/secret/x.sas7bdat" // using "/tmp/allowed/ok.sas7bdat", clear``，
    解析器遇行内 ``//`` 截断后只剩越界读取 —— 与已修复的 varlist 路径注入同类。
    """
    from server import _validate_filter_expr

    assert _validate_filter_expr(value, "condition") is not None


@pytest.mark.parametrize(
    "value",
    [
        # in_range 天然需要 `/`
        "1/100",
        "1/10",
        "f/l",
        # condition 的常见合法形态
        'make == "Honda"',
        "price > 5000 & foreign == 1",
        "inlist(rep78, 3, 4, 5)",
        # 字符串内的 `//` 不是注释，不应误伤
        'strpos(url, "//") > 0',
        "",
    ],
)
def test_validate_filter_expr_allows_legitimate(value):
    from server import _validate_filter_expr

    assert _validate_filter_expr(value, "condition") is None


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


def test_has_unsafe_brace_detects_unmatched_closing():
    from server import _has_unsafe_brace

    # 字符串外的 } 应检测为不安全
    assert _has_unsafe_brace("scatter price weight }") is True
    assert _has_unsafe_brace("} scatter price weight") is True


def test_has_unsafe_brace_allows_balanced_or_none():
    from server import _has_unsafe_brace

    # 无花括号 → 安全
    assert _has_unsafe_brace("scatter price weight") is False
    # 均衡的 { } → 安全
    assert _has_unsafe_brace("capture noisily { scatter price weight }") is False


def test_has_unsafe_brace_flags_unclosed_opening():
    """自带未闭合 { 的命令会让 Stata 进入等待输入状态并挂死会话，SetBreak 救不回。"""
    from server import _has_unsafe_brace

    assert _has_unsafe_brace("forvalues i=1/3 {") is True
    assert _has_unsafe_brace("scatter price weight {") is True


def test_has_unsafe_brace_allows_brace_inside_string():
    from server import _has_unsafe_brace

    # 字符串内的 } 应视为安全
    assert _has_unsafe_brace('scatter price weight, title("a} b")') is False


def test_return_type_str_toolresult_consistency():
    """所有 MCP 工具函数的返回类型都应为 str | ToolResult。"""
    import inspect

    from server import (
        stata_codebook,
        stata_describe,
        stata_display,
        stata_export_excel,
        stata_find_package,
        stata_graph,
        stata_install_package,
        stata_list,
        stata_list_packages,
        stata_logistic,
        stata_more,
        stata_ping,
        stata_regress,
        stata_run,
        stata_run_do_file,
        stata_save_dataset,
        stata_set_cwd,
        stata_status,
        stata_summarize,
        stata_tabulate,
        stata_ttest,
        stata_use_dataset,
    )

    tools_with_str_only_return = []
    for name, func in [
        ("stata_run", stata_run),
        ("stata_run_do_file", stata_run_do_file),
        ("stata_use_dataset", stata_use_dataset),
        ("stata_save_dataset", stata_save_dataset),
        ("stata_set_cwd", stata_set_cwd),
        ("stata_describe", stata_describe),
        ("stata_summarize", stata_summarize),
        ("stata_list", stata_list),
        ("stata_codebook", stata_codebook),
        ("stata_tabulate", stata_tabulate),
        ("stata_display", stata_display),
        ("stata_regress", stata_regress),
        ("stata_logistic", stata_logistic),
        ("stata_ttest", stata_ttest),
        ("stata_graph", stata_graph),
        ("stata_export_excel", stata_export_excel),
        ("stata_install_package", stata_install_package),
        ("stata_find_package", stata_find_package),
        ("stata_list_packages", stata_list_packages),
        ("stata_more", stata_more),
        ("stata_status", stata_status),
        ("stata_ping", stata_ping),
    ]:
        sig = inspect.signature(func)
        ret = sig.return_annotation
        ret_str = str(ret)
        if "ToolResult" not in ret_str:
            tools_with_str_only_return.append(name)
    assert not tools_with_str_only_return, (
        f"以下工具返回类型应为 str | ToolResult，但缺少 ToolResult: {tools_with_str_only_return}"
    )


# --- varlist 注入：路径沙箱绕过 -----------------------------------------------
# varlist 被拼进 `export excel <varlist> using "<已校验路径>"`。实测
# varlist='mpg using /evil/out.xlsx, replace //' 可构造出
# `export excel mpg using /evil/out.xlsx, replace // using "<安全路径>"`
# —— `//` 把经 _validate_path 校验的路径整段注释掉，数据落到攻击者指定位置。


@pytest.mark.parametrize(
    "varlist",
    [
        "mpg using /tmp/evil/out.xlsx, replace //",  # 完整的沙箱绕过载荷
        "mpg using /tmp/evil.xlsx",
        "mpg //",
        "mpg /* comment */",
        "mpg, replace",
        "mpg USING /tmp/x.xlsx",  # 大小写不敏感
    ],
)
def test_validate_varlist_rejects_command_rewriting(varlist):
    assert _validate_varlist(varlist, "varlist") is not None


@pytest.mark.parametrize(
    "varlist",
    [
        "mpg price weight",
        "i.group x1 L.x2 c.price##i.foreign [aw=weight]",
        "x1-x10",
        "mpg*",
        "price if_flag",  # 变量名里含 using/if 子串不应误伤
        "housing_use",
    ],
)
def test_validate_varlist_allows_legitimate_syntax(varlist):
    assert _validate_varlist(varlist, "varlist") is None


# --- 护栏必须校验「真正执行的文本」---------------------------------------------
# _has_dangerous_command_prefix 做的是逐行行首匹配，而 _parse_command_blocks 在
# 送执行前还会剥掉 /* */ 块注释、按 /// 拼接续行。在原始文本上匹配等于校验了一份
# 和执行内容不同的东西。实测（Stata 19.5 MP）全部绕过成功。


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        ("/* x */ !whoami", "块注释前缀掩盖 !"),
        ("/*c*/shell echo hi", "块注释前缀掩盖 shell"),
        ("/**/python: import os", "块注释前缀掩盖 python:"),
        ("/* a */ mata: 1", "块注释前缀掩盖 mata"),
        ("summarize price\n/*z*/winexec calc.exe", "多行中的某一行被掩盖"),
        # 下面两条最难防：原始文本里根本不存在 "shell" 这个词，
        # 是解析器把被劈开的 token 重新拼回来的。
        ("sh/*x*/ell echo hi", "块注释从中间劈开 token 后重组"),
        ("sh///\nell echo hi", "续行符从中间劈开 token 后重组"),
        ("capture noisily {\n/* c */ !whoami\n}", "复合块内（会落临时 do 文件）"),
    ],
)
def test_validate_command_blocks_blocks_comment_bypass(payload, why):
    assert _validate_command_blocks(payload) is not None, f"未拦截：{why}"


@pytest.mark.parametrize(
    "cmd",
    [
        "summarize price",
        "regress price weight mpg",
        "regress price ///\n    weight mpg",
        "forvalues i = 1/3 {\n    display `i'\n}",
        'label variable price `"价格（美元）"\'',
        # 注释里出现危险词不应误伤：被注释掉的内容根本不会产生执行块
        "* 这行注释提到 shell 和 !ls\nsummarize price",
        "/* 块注释提到 python: 与 mata */ summarize price",
    ],
)
def test_validate_command_blocks_allows_legitimate(cmd):
    assert _validate_command_blocks(cmd) is None


def test_validate_command_blocks_still_catches_plain_payloads():
    """不带注释的直白载荷当然也要拦。"""
    for cmd in ("!whoami", "shell ls", "python: import os", "mata:", "winexec notepad.exe"):
        assert _validate_command_blocks(cmd) is not None


# --- 必填参数不能为空 ----------------------------------------------------------
# 空的 depvar 会静默产生**错误结果**而非报错：实测
# stata_regress(depvar="", indepvars="weight") 拼出 `regress  weight`，
# Stata 把 weight 当因变量跑出一个完全不同的回归并返回成功。


def test_validate_identifier_rejects_empty_when_required():
    assert _validate_identifier("", "depvar", required=True) is not None
    assert _validate_identifier("   ", "depvar", required=True) is not None


def test_validate_identifier_allows_empty_when_optional():
    """可选参数（如 ttest 的 byvar）的空值是合法的「不使用」。"""
    assert _validate_identifier("", "byvar") is None
    assert _validate_identifier("   ", "byvar") is None


# --- scheme 改用正向白名单 ------------------------------------------------------
# 黑名单曾漏掉 `,`，而 `set scheme` 支持逗号后的选项（, permanently）。


@pytest.mark.parametrize("scheme", ["s2color", "538", "s1color-asterisk", "economist", "s2mono"])
def test_validate_scheme_allows_real_scheme_names(scheme):
    assert _validate_scheme_name(scheme) is None


@pytest.mark.parametrize(
    "scheme",
    ["s2color,permanently", "s2color, foo", "a b", "x;y", "$x", "`x`", "s(1)", ""],
)
def test_validate_scheme_rejects_anything_outside_whitelist(scheme):
    assert _validate_scheme_name(scheme) is not None


# --- stata_import 的选项校验须与 stata_export_excel 对称 ------------------------
# 同为双引号包裹的 sheet()，export 侧走 _validate_sheet_name（明确拒绝 `"`），
# import 侧却混在 _validate_no_injection 那批里（只拒换行/回车/空字节/分号），
# 于是同一个值在两个工具里下场完全相反：import 侧可提前闭合引号注入任意选项。


@pytest.mark.parametrize(
    ("kwargs", "keyword"),
    [
        ({"sheet": 'S1") cellrange(A1:A1) //'}, "sheet"),
        ({"cellrange": "A1:B2) clear //"}, "cellrange"),
        ({"encoding": 'utf-8") clear //'}, "encoding"),
        ({"varnames": '1) clear //'}, "varnames"),
    ],
)
def test_import_rejects_quote_and_paren_escapes(kwargs, keyword, tmp_path):
    from server import stata_import

    target = tmp_path / ("book.xlsx" if "sheet" in kwargs or "cellrange" in kwargs else "d.csv")
    target.write_text("x\n")
    result = stata_import(filepath=str(target), **kwargs)
    text = result.content[0].text if hasattr(result, "content") else result
    assert keyword in text and "错误" in text


def test_import_allows_normal_sheet_name(tmp_path):
    """含空格与括号的正常工作表名不应误伤（值在双引号内对 Stata 安全）。"""
    from unittest.mock import patch

    from server import stata_import

    target = tmp_path / "book.xlsx"
    target.write_text("x\n")
    with patch("server._run_stata_command") as mock_run:
        stata_import(filepath=str(target), sheet="Q1 (2024)")
    assert 'sheet("Q1 (2024)")' in mock_run.call_args[0][0]
