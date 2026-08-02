"""崩溃扫描：对真实 Stata 系统性触发边界/错误场景，断言「不崩溃、不挂死、优雅报错」。

目标覆盖 CLAUDE.md「已修复的崩溃历史」之外仍可能触发 DLL 崩溃/挂死的输入：
未设前置（无估计就 margins）、非法命令组合、危险前缀、未闭合块、unicode、空数据、
坏文件、单行复合块等。

判定规则：结果含「崩溃」/「无响应」/「已自动恢复」→ 判失败；含「[返回码:」或
「错误:」→ 优雅报错（通过）；纯输出 → 通过。超时由工具 timeout 兜底。

运行：``.venv/bin/python -m pytest tests_e2e/test_crash_probes.py -q``
"""

import pytest

from tests_e2e.conftest import SKIP_REASON, STATA_AVAILABLE, result_text

pytestmark = [
    pytest.mark.stata,
    pytest.mark.skipif(not STATA_AVAILABLE, reason=SKIP_REASON),
]

# (工具调用, 场景说明)
_PROBES = [
    # --- 未设前置的估计/后估计 ---
    (lambda s: s.stata_margins(), "无估计就 margins"),
    (lambda s: s.stata_predict("resid"), "无估计就 predict"),
    (lambda s: s.stata_lincom("_b[x]"), "无估计就 lincom"),
    (lambda s: s.stata_nlcom("exp(_b[x])"), "无估计就 nlcom"),
    (lambda s: s.stata_test("x"), "无估计就 test"),
    (lambda s: s.stata_etable(), "无活跃估计就 etable"),
    (lambda s: s.stata_xtreg("y", "x"), "未 xtset 就 xtreg"),
    (lambda s: s.stata_ivregress("price", "weight", ""), "ivregress 缺工具变量"),
    # --- 非法/缺失参数 ---
    (lambda s: s.stata_run("regress"), "regress 缺参数"),
    (lambda s: s.stata_run("regress price nonexistent_xyz"), "回归不存在的变量"),
    (lambda s: s.stata_run("use /nonexistent/data_xyz.dta"), "加载不存在的 dta"),
    (lambda s: s.stata_run("import delimited using /nonexistent/f_xyz.csv"), "导入不存在的 csv"),
    (lambda s: s.stata_use_example("nonexistent_dataset_xyz"), "加载不存在的示例数据"),
    (lambda s: s.stata_run("include /nonexistent/x_xyz.do"), "include 不存在文件"),
    (lambda s: s.stata_run("summarize"), "空数据上的 summarize"),
    (lambda s: s.stata_run("tabulate"), "空数据上的 tabulate"),
    # --- 危险前缀/未闭合块（必须被入口拦截，绝不挂死）---
    (lambda s: s.stata_run("!ls"), "shell out !"),
    (lambda s: s.stata_run("shell ls"), "shell 前缀"),
    (lambda s: s.stata_run("winexec notepad"), "winexec"),
    (lambda s: s.stata_run("python:"), "python 块"),
    (lambda s: s.stata_run("mata:"), "mata 块"),
    (lambda s: s.stata_run("#delimit ;"), "#delimit"),
    (lambda s: s.stata_run("capture noisily {"), "未闭合复合块"),
    (lambda s: s.stata_run("if 1 {"), "未闭合 if 块"),
    (lambda s: s.stata_run("program define foo"), "未闭合 program 定义"),
    (lambda s: s.stata_run("forvalues i=1/3 { display \\`i' }"), "单行复合块（Stata 语法错）"),
    # --- unicode / 特殊字符 ---
    (lambda s: s.stata_run('display "中文测试"'), "unicode 输出"),
    (lambda s: s.stata_run('display `"复合引号"\''), "复合引号"),
    (lambda s: s.stata_display('"abc" + "def"'), "display 字符串"),
    # --- 复合块与续行 ---
    (lambda s: s.stata_run("display 1\ndisplay 2"), "多命令"),
    (lambda s: s.stata_run('display "1" ///\n display "2"'), "/// 续行"),
    (lambda s: s.stata_run("forvalues i=1/3 {\n    display \\`i'\n}"), "合法循环块"),
    (lambda s: s.stata_run("if 1 {\n    display 42\n}"), "合法 if 块"),
    # --- 数据操作边界 ---
    (lambda s: s.stata_run("gen x = missing()"), "generate 函数缺参"),
    (lambda s: s.stata_run("replace x = 1"), "replace 不存在的变量"),
    (lambda s: s.stata_run("drop x_never_exists"), "drop 不存在的变量"),
    (lambda s: s.stata_collapse("price", "nonexistent_xyz"), "collapse 不存在变量"),
    (lambda s: s.stata_run("reshape long x, i(id) j(t)"), "未 reshape 的数据上 reshape"),
    (lambda s: s.stata_merge(kind="1:1", keyvars="price", using="nonexistent_xyz.dta"), "merge 不存在文件"),
    (lambda s: s.stata_run("save /nonexistent_dir_xyz/out.dta"), "保存到不存在目录"),
    # --- 特殊命令 / 输出 ---
    (lambda s: s.stata_run("memory"), "memory 命令"),
    (lambda s: s.stata_run("set more off"), "set more off"),
    (lambda s: s.stata_run("log close _all"), "log close"),
    (lambda s: s.stata_run("version"), "version"),
    (lambda s: s.stata_run("about"), "about"),
    (lambda s: s.stata_run("sleep 50"), "sleep"),
]


@pytest.fixture(autouse=True)
def _clean_data(stata):
    """每个 probe 前重置到干净空数据，避免上一个 case 的数据污染。"""
    stata.stata_run("clear all")
    yield


@pytest.mark.parametrize("probe", _PROBES, ids=lambda p: p[1])
def test_no_crash_on_boundary_input(stata, probe):
    fn, desc = probe
    try:
        result = fn(stata)
    except Exception as e:  # noqa: BLE001 —— 任何 Python 层异常都算失败
        pytest.fail(f"场景「{desc}」抛出未捕获异常: {type(e).__name__}: {e}")
    text = result_text(result)
    # 崩溃/无响应/恢复标记 —— 有任何一个都说明发生了 DLL 级问题
    for marker in ("StataSO_Execute 崩溃", "Stata DLL 无响应", "已自动恢复"):
        assert marker not in text, f"场景「{desc}」触发崩溃标记: {marker}\n{text[:300]}"


# ---------------------------------------------------------------------------
# 后台任务 × 会话交互（最新基建的崩溃面）
# ---------------------------------------------------------------------------


def test_background_task_then_clear_session(stata):
    """后台任务结束后立即 clear 会话，不应崩溃或残留任务。"""
    import time

    tid = result_text(stata.stata_background("display 999", timeout=120))
    tid = tid.split(":")[1].split()[0]
    for _ in range(30):
        status = result_text(stata.stata_task_status(tid))
        if "done" in status or "failed" in status:
            break
        time.sleep(0.2)
    result = stata.stata_clear(scope="all")
    assert not getattr(result, "is_error", False), result_text(result)[:200]


def test_background_task_cancelled_then_result(stata):
    """取消后取结果返回 cancelled 而非崩溃标记。"""
    import time

    loop = "forvalues i=1/50000000 {\n    display `i'\n}"
    tid = result_text(stata.stata_background(loop, timeout=600))
    tid = tid.split(":")[1].split()[0]
    time.sleep(0.8)
    stata.stata_task_cancel(tid)
    time.sleep(0.5)
    r = stata.stata_task_result(tid)
    txt = result_text(r)
    assert "StataSO_Execute 崩溃" not in txt
    assert "DLL 无响应" not in txt


def test_do_file_clear_all_mid_run(stata, outdir):
    """do 文件里 clear all 不应使后续命令崩溃。"""
    do_file = outdir / "midclear.do"
    do_file.write_text(
        "sysuse auto, clear\nclear all\nsummarize\n", encoding="utf-8"
    )
    r = stata.stata_run_do_file(str(do_file))
    txt = result_text(r)
    assert "崩溃" not in txt and "无响应" not in txt


def test_do_file_shell_command_rejected(stata, outdir):
    """do 文件含 shell-out 被拒（此前真机确认可执行主机命令并污染 stdout）。"""
    do_file = outdir / "shell.do"
    do_file.write_text("shell echo X\n", encoding="utf-8")
    r = stata.stata_run_do_file(str(do_file))
    txt = result_text(r)
    assert "危险命令" in txt, txt[:200]
    # 错误文本会引用被拒命令的目标（echo X），故不能断言 X 不在文本；
    # 改为断言「拒绝执行」且无任何 shell 执行证据（输出应只有错误信息）。
    assert "拒绝执行" in txt, txt[:200]
