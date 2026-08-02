"""新功能端到端验证：文件资源回传、会话生命周期、后台任务、便利工具。

运行：``.venv/bin/python -m pytest tests_e2e/ -q``（需真实 Stata）
只放单元测试无法证伪的断言（对 Stata 实际行为的假设）；命令拼接留在 tests/。
"""

import base64
import time

import pytest

from tests_e2e.conftest import SKIP_REASON, STATA_AVAILABLE, result_text

pytestmark = [
    pytest.mark.stata,
    pytest.mark.skipif(not STATA_AVAILABLE, reason=SKIP_REASON),
]


def _ok(result, label=""):
    assert not getattr(result, "is_error", False), f"{label}{result_text(result)}"
    return result_text(result)


def _is_error(result):
    return bool(getattr(result, "is_error", False))


# ===========================================================================
# 文件资源回传
# ===========================================================================


def test_export_delimited_registers_and_reads_back(stata, outdir):
    stata.stata_run("sysuse auto, clear")
    p = str(outdir / "auto.csv")
    _ok(stata.stata_export_delimited(p, replace=True))
    listing = _ok(stata.stata_list_resources())
    assert "auto.csv" in listing, listing

    info = _ok(stata.stata_read_file(p))
    assert "stata-file:///" in info and "text/csv" in info, info

    b64 = _ok(stata.stata_read_file(p, action="read"))
    csv_text = base64.b64decode(b64).decode("utf-8")
    assert "make,price,mpg" in csv_text, csv_text[:200]  # 表头


def test_resource_template_serves_real_graph(stata, outdir):
    """stata_graph 导出的 PNG 能经 resources/read 取回，且是真实图片字节。"""
    import asyncio

    stata.stata_run("sysuse auto, clear")
    p = str(outdir / "scatter.png")
    _ok(stata.stata_graph("twoway scatter price weight", export=p, replace=True))
    uri = stata._resource_uri(p)  # fixture 即 server 模块，可直接取内部助手
    rr = asyncio.run(stata.mcp.read_resource(uri))
    content = rr.contents[0].content
    assert content[:4] == b"\x89PNG", content[:8]  # PNG 魔数


def test_save_dataset_registers_dta(stata, outdir):
    stata.stata_run("sysuse auto, clear")
    p = str(outdir / "auto.dta")
    _ok(stata.stata_save_dataset(p, replace=True))
    info = _ok(stata.stata_read_file(p))
    assert "application/octet-stream" in info or "stata" in info.lower(), info


def test_save_output_captures_full_text(stata, outdir):
    import asyncio

    stata.stata_run("sysuse auto, clear")
    p = str(outdir / "big.txt")
    # 生成超过 120K 的输出（约 30K 行），验证 save_output 保存的是完整文本。
    # Stata 要求 { 单独成行（单行 forvalues ... { display } 会 r(198)）。
    loop = "forvalues i=1/30000 {\n    display `i'\n}"
    res = result_text(stata.stata_run(loop, save_output=p))
    assert "完整输出已保存" in res, res[:300]

    # 大文件经资源协议取回（流式二进制，不占用工具返回载荷）
    uri = stata._resource_uri(p)
    rr = asyncio.run(stata.mcp.read_resource(uri))
    full = rr.contents[0].content.decode("utf-8")
    assert full.strip().endswith("30000")  # 最后一行被完整保存（内存里已被 120K 截断）

    # base64 工具读取对超限文件给出可操作报错并指向资源 URI
    err = stata.stata_read_file(p, action="read")
    assert getattr(err, "is_error", False)
    assert "资源协议" in result_text(err) and "stata-file:///" in result_text(err)


def test_read_file_rejects_unregistered(stata, outdir):
    p = str(outdir / "ghost.csv")
    with open(p, "w") as f:
        f.write("x\n")
    assert _is_error(stata.stata_read_file(p))  # 未登记不可读


def test_register_file_then_read(stata, outdir):
    p = str(outdir / "manual.txt")
    with open(p, "w") as f:
        f.write("hello resource\n")
    _ok(stata.stata_register_file(p))
    b64 = _ok(stata.stata_read_file(p, action="read"))
    assert base64.b64decode(b64).decode() == "hello resource\n"


# ===========================================================================
# 会话生命周期
# ===========================================================================


def test_clear_data_empties_dataset(stata):
    stata.stata_run("sysuse auto, clear")
    stata.stata_run("gen id = _n")
    _ok(stata.stata_clear(scope="data"))
    out = result_text(stata.stata_run("display c(N)"))
    assert out.strip() == "0", out  # clear all 后无观测


def test_snapshot_restore_roundtrip(stata):
    stata.stata_run("sysuse auto, clear")
    stata.stata_run("gen marker = 1")
    _ok(stata.stata_snapshot(action="save", label="before"))

    # 从 snapshot list 解析分配到的编号（会话共享，编号不一定从 1 开始）
    # 输出格式：snapshot 1 (before) created at 2 Aug 2026 12:39
    import re

    listing = _ok(stata.stata_snapshot(action="list"))
    m = re.search(r"snapshot\s+(\d+)\s*\(before\)", listing)
    number = int(m.group(1)) if m else None
    assert number, listing

    # 破坏数据后再恢复
    stata.stata_run("drop make")
    _ok(stata.stata_snapshot(action="restore", number=number))
    out = result_text(stata.stata_run("confirm variable make"))
    assert "0" not in out, "make 变量应恢复存在"
    stata.stata_snapshot(action="erase", number=number)


# ===========================================================================
# 后台任务
# ===========================================================================


def test_background_task_runs_and_returns_result(stata):
    tid = result_text(stata.stata_background("display 12345"))
    assert tid.strip(), "应返回 task_id"
    tid = tid.split(":")[1].split()[0]

    # 轮询直到完成
    for _ in range(50):
        status = _ok(stata.stata_task_status(tid))
        if "done" in status or "failed" in status:
            break
        time.sleep(0.2)
    assert "done" in status, status
    out = _ok(stata.stata_task_result(tid))
    assert "12345" in out, out


def test_background_task_cancel(stata):
    # 长循环；{ 单独成行（单行 forvalues ... { display } 会 r(198)）
    loop = "forvalues i=1/100000000 {\n    display `i'\n}"
    tid = result_text(stata.stata_background(loop, timeout=600))
    tid = tid.split(":")[1].split()[0]
    # 等它跑起来再取消
    time.sleep(1.0)
    _ok(stata.stata_task_cancel(tid))
    for _ in range(50):
        status = _ok(stata.stata_task_status(tid))
        if "cancelled" in status or "done" in status or "failed" in status:
            break
        time.sleep(0.2)
    assert "cancelled" in status, status


# ===========================================================================
# 结构化便利工具（真机执行验证命令合法）
# ===========================================================================


def test_data_restructure_tools_on_auto(stata):
    stata.stata_run("sysuse auto, clear")
    _ok(stata.stata_replace("price", "price/100"))
    assert "price" in _ok(stata.stata_describe("price"))

    _ok(stata.stata_rename("mpg", "mileage"))
    _ok(stata.stata_recode("rep78", "(1=0) (2/5=1)"))
    assert "mileage" in _ok(stata.stata_describe())

    _ok(stata.stata_generate("price_c", 'string(price, "%9.0f")'))
    _ok(stata.stata_destring("price_c", replace=True, force=True))

    _ok(stata.stata_keep("price mileage rep78"))
    _ok(stata.stata_drop("rep78"))
    listing = _ok(stata.stata_describe())
    assert "rep78" not in listing and "price" in listing, listing


def test_extended_estimation_tools(stata):
    stata.stata_run("sysuse auto, clear")
    _ok(stata.stata_logit("foreign", "price mpg"))
    _ok(stata.stata_nbreg("rep78", "price weight", condition="rep78 > 0"))
    _ok(stata.stata_qreg("price", "weight mpg", quantile=0.5))
    # mlogit 需要多分类因变量
    stata.stata_run("gen cat = 0 if rep78 <= 3")
    stata.stata_run("replace cat = 1 if rep78 == 4")
    stata.stata_run("replace cat = 2 if rep78 == 5")
    _ok(stata.stata_mlogit("cat", "price weight", baseoutcome="1"))
    # mixed 显式 || 记法（auto 无分组变量，用 foreign 做两层）
    _ok(stata.stata_mixed("price", "weight mpg", random="|| foreign:"))


def test_postestimation_tools(stata):
    stata.stata_run("sysuse auto, clear")
    _ok(stata.stata_regress("price", "weight mpg"))
    out = _ok(stata.stata_lincom("_b[weight] + _b[mpg]"))
    # 表头随版本而变（Coef./Coefficient），断言落在稳定的线性组合标签上
    assert "weight + mpg" in out and "Std. err." in out, out[:300]
    _ok(stata.stata_nlcom("exp(_b[mpg])"))

    # hausman：需先存两个模型
    _ok(stata.stata_regress("price", "weight mpg"))
    _ok(stata.stata_estimates(action="store", name="m1"))
    _ok(stata.stata_regress("price", "weight"))
    _ok(stata.stata_estimates(action="store", name="m2"))
    out = _ok(stata.stata_hausman("m2", "m1", options="sigmamore"))
    assert "chi2" in out, out[:300]
    stata.stata_run("capture estimates drop m1 m2")


# ===========================================================================
# 服务器日志
# ===========================================================================


def test_read_log_path_and_tail(stata):
    p = _ok(stata.stata_read_log(action="path"))
    assert "stata-mcp.log" in p, p
    tail = _ok(stata.stata_read_log(action="tail", lines=50))
    assert tail.strip(), "日志应有内容"
