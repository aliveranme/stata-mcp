"""自由文本路径审计的端到端验证（真实 Stata）。

配置 STATA_ALLOWED_ROOTS 后，stata_run 的引号路径必须落在白名单内。
审计在 _run_stata_command 锁内启用，仅当白名单配置时；此文件显式设置白名单。

注意：E2E 共享一个 server 实例，改 STATA_ALLOWED_ROOTS 前必须清 _ALLOWED_ROOTS_CACHE，
用后恢复，避免污染其他用例。
"""

import os

import pytest

from tests_e2e.conftest import SKIP_REASON, STATA_AVAILABLE, result_text

pytestmark = [
    pytest.mark.stata,
    pytest.mark.skipif(not STATA_AVAILABLE, reason=SKIP_REASON),
]


def _set_roots(stata, roots: str | None):
    """设置/清除 STATA_ALLOWED_ROOTS 并失效缓存。"""
    if roots is None:
        os.environ.pop("STATA_ALLOWED_ROOTS", None)
    else:
        os.environ["STATA_ALLOWED_ROOTS"] = roots
    stata._ALLOWED_ROOTS_CACHE = None


def test_free_text_path_audit_blocks_outside_and_allows_inside(stata, outdir):
    allowed = outdir / "allowed"
    allowed.mkdir()
    inside = str(allowed / "in.dta")
    outside = str(outdir / "outside.dta")
    stata.stata_run("sysuse auto, clear")
    stata.stata_run(f'save "{inside}", replace')
    stata.stata_run("clear all")

    _set_roots(stata, str(allowed))
    try:
        # 沙箱内：正常加载
        ok = result_text(stata.stata_run(f'use "{inside}"'))
        assert getattr(stata.stata_run("display c(N)"), "is_error", False) is False
        assert "74" in result_text(stata.stata_run("display c(N)")) or "74" in ok

        # 沙箱外：被审计拦截（不触发 Stata 执行）
        blocked = stata.stata_run(f'use "{outside}"')
        assert getattr(blocked, "is_error", False), result_text(blocked)
        assert "沙箱外" in result_text(blocked)

        # 结构化工具同样受约束：use_dataset 越界被拒（既有能力，顺带验证）
        blocked2 = stata.stata_use_dataset(outside)
        assert getattr(blocked2, "is_error", False)
    finally:
        _set_roots(stata, None)


def test_free_text_path_audit_disabled_without_roots(stata, outdir):
    """未配置白名单时不启用审计（向后兼容），越界自由文本照常执行。"""
    _set_roots(stata, None)
    p = str(outdir / "plain.dta")
    stata.stata_run("sysuse auto, clear")
    stata.stata_run(f'save "{p}", replace')
    stata.stata_run("clear all")
    res = result_text(stata.stata_run(f'use "{p}"'))
    assert "沙箱外" not in res
