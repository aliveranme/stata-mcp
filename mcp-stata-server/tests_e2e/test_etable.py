"""``stata_etable`` —— 真实 Stata 端到端验证。

单元测试只能证明命令串拼对了。这里要证的是单元测试无法证伪的那部分：
Stata 真的接受这个语法、文件真的被写出、以及不支持的格式真的会失败
（而 ``etable`` 会先把表格正常打印出来再报 r(198)，只看输出会把失败当成功）。

运行：``STATA_HOME=... .venv/bin/python -m pytest tests_e2e/ -q``
"""

import os

import pytest

from tests_e2e.conftest import SKIP_REASON, STATA_AVAILABLE, result_text

pytestmark = [
    pytest.mark.stata,
    pytest.mark.skipif(not STATA_AVAILABLE, reason=SKIP_REASON),
]


def _ok(result, label=""):
    assert not getattr(result, "is_error", False), f"{label}{result_text(result)}"
    return result_text(result)


@pytest.fixture
def two_models():
    """跑两个回归并存起来，供并排成表。"""
    from server import stata_estimates, stata_regress, stata_use_example

    _ok(stata_use_example("auto"), "load: ")
    _ok(stata_regress(depvar="price", indepvars="weight"), "m1: ")
    _ok(stata_estimates(action="store", name="e2e_m1"), "store m1: ")
    _ok(stata_regress(depvar="price", indepvars="weight mpg"), "m2: ")
    _ok(stata_estimates(action="store", name="e2e_m2"), "store m2: ")
    yield "e2e_m1 e2e_m2"
    stata_estimates(action="drop", name="e2e_m1 e2e_m2")


def test_etable_prints_table_for_active_estimates(two_models):
    from server import stata_etable

    text = _ok(stata_etable(), "etable: ")
    assert "price" in text


def test_etable_combines_stored_models_with_stars_and_stats(two_models):
    from server import stata_etable

    text = _ok(
        stata_etable(estimates=two_models, stars=True, stats="N r2"),
        "etable multi: ",
    )
    # 两个模型并排 → price 列出现两次
    assert text.count("price") >= 2


@pytest.mark.parametrize("ext", [".docx", ".xlsx", ".html", ".pdf", ".tex", ".md"])
def test_etable_export_writes_real_file(tmp_path, two_models, ext):
    """逐格式确认文件真被写出 —— 这是本工具的核心承诺。"""
    from server import stata_etable

    target = tmp_path / f"table{ext}"
    text = _ok(
        stata_etable(estimates=two_models, export=str(target), replace=True),
        f"export {ext}: ",
    )
    assert target.is_file(), text
    assert target.stat().st_size > 0
    assert "已导出" in text


def test_etable_export_without_replace_fails_on_existing_file(tmp_path, two_models):
    """已存在且未传 replace 时必须报错，不能把陈旧文件当成功。"""
    from server import stata_etable

    target = tmp_path / "t.docx"
    _ok(stata_etable(estimates=two_models, export=str(target), replace=True), "first: ")
    before = target.stat().st_mtime_ns

    result = stata_etable(estimates=two_models, export=str(target))

    assert getattr(result, "is_error", False) is True
    assert "replace" in result_text(result)
    assert target.stat().st_mtime_ns == before


def test_etable_rejects_csv_before_reaching_stata(tmp_path, two_models):
    """.csv 在 Stata 里是 r(198)，且错误会淹没在表格输出里，故在入口拦下。"""
    from server import stata_etable

    target = tmp_path / "t.csv"
    result = stata_etable(estimates=two_models, export=str(target))

    assert getattr(result, "is_error", False) is True
    assert ".csv" in result_text(result)
    assert not os.path.exists(target)
