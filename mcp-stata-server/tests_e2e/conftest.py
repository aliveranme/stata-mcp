"""E2E 测试配置：驱动真实 Stata，不使用 ``tests/conftest.py`` 的 pystata 桩。

**必须与 ``tests/`` 分开调用。** ``tests/conftest.py`` 在导入时就把 ``pystata`` /
``sfi`` 换成 MagicMock（且只在 ``sys.modules`` 里没有时才装桩），同一个 pytest
进程里再也换不回真 Stata —— 混跑时这里的用例会静默地对着 mock 断言。

    STATA_HOME=/path/to/StataNow .venv/bin/python -m pytest tests_e2e/ -q

未检测到 Stata 安装时整个目录自动跳过，CI/无 Stata 的机器上是无害的 no-op。
"""

import glob
import os
import sys

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "stata: 需要真实 Stata 安装的端到端用例")


# --- 定位 Stata --------------------------------------------------------------

_HOME_CANDIDATES = [
    "/Applications/StataNow",
    "/Applications/Stata",
    "/Volumes/*/Applications/StataNow",
    "/Volumes/*/Applications/Stata",
    "C:/Program Files/StataNow/StataNow19",
    "C:/Program Files/Stata19",
]


def _detect_home() -> str | None:
    if env := os.environ.get("STATA_HOME"):
        return env if os.path.isdir(os.path.join(env, "utilities")) else None
    for pattern in _HOME_CANDIDATES:
        for path in sorted(glob.glob(pattern)):
            if os.path.isdir(os.path.join(path, "utilities")):
                return path
    return None


def _detect_edition(home: str) -> str | None:
    """按平台特征文件推断版本；探测不到时不猜，交由调用方跳过。"""
    if env := os.environ.get("STATA_EDITION"):
        return env
    for edition in ("mp", "se", "be"):
        patterns = [
            os.path.join(home, f"{edition}-64.dll"),
            os.path.join(home, "*.app", "Contents", "MacOS", f"libstata-{edition}.dylib"),
            os.path.join(home, f"libstata-{edition}.so"),
        ]
        if any(glob.glob(p) for p in patterns):
            return edition
    return None


def _load_server():
    """导入真实的 server 模块；不可用时返回 (None, 原因)。"""
    if type(sys.modules.get("pystata")).__module__.startswith("unittest.mock"):
        return None, "pystata 已被 tests/conftest.py 桩掉，E2E 必须单独跑 tests_e2e/"

    home = _detect_home()
    if not home:
        return None, "未检测到 Stata 安装（可用 STATA_HOME 指定）"
    edition = _detect_edition(home)
    if not edition:
        return None, f"无法推断 Stata 版本（可用 STATA_EDITION 指定）：{home}"

    os.environ["STATA_HOME"] = home
    os.environ["STATA_EDITION"] = edition
    server_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if server_dir not in sys.path:
        sys.path.insert(0, server_dir)

    try:
        import server
    except SystemExit as e:  # server.py 初始化失败时 sys.exit(1)
        return None, f"Stata 初始化失败（{home}, {edition}）：{e}"
    return server, ""


SERVER, SKIP_REASON = _load_server()
STATA_AVAILABLE = SERVER is not None


_EXIT_STATUS = {"code": 0}


def pytest_sessionfinish(session, exitstatus):
    _EXIT_STATUS["code"] = int(exitstatus)


def pytest_unconfigure(config):
    """用 ``os._exit`` 强制收口退出码。

    实测（Stata 19.5 MP，macOS）：``pystata.config.init()`` 之后进程退出码恒为 0
    —— 连 ``sys.exit(3)`` 都返回 0，Stata 运行时接管了解释器的退出路径。后果是
    E2E 失败会被任何按 ``$?`` 判断的脚本或 CI 当成通过，这批用例形同虚设。

    ``os._exit`` 跳过 atexit 与解释器清理直接落 syscall，是唯一能绕开的手段。
    必须挂在 ``pytest_unconfigure`` 而非 ``pytest_sessionfinish``：后者早于终端
    汇总，会把「N passed」那几行一起截掉。未初始化 Stata 时不介入。
    """
    if not STATA_AVAILABLE:
        return
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_EXIT_STATUS["code"])


@pytest.fixture(scope="session")
def stata():
    """已初始化的 server 模块。"""
    return SERVER


@pytest.fixture
def auto_data(stata):
    """每个用例都从干净的 auto 数据集开始，消除用例间的数据污染。"""
    stata.stata_run("sysuse auto, clear")
    return stata


@pytest.fixture
def outdir(tmp_path):
    return tmp_path


def result_text(result) -> str:
    """统一提取 str / ToolResult 的文本。"""
    return result.content[0].text if hasattr(result, "content") else result
