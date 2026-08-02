"""pytest configuration for stata-mcp-server tests."""

import atexit
import os
import sys
from unittest.mock import MagicMock

import pytest


def abs_path(*parts: str) -> str:
    """构造平台原生的绝对路径（Windows: ``C:/a/b``，POSIX: ``/a/b``）。

    测试中不能硬编码 ``C:/`` 前缀：POSIX 下 ``os.path.isabs("C:/x")`` 为 False，
    于是 ``_validate_path`` / ``_normalize_path`` 会把它当相对路径拼上 cwd，
    断言随之失败。反过来 ``/x`` 在 Windows 上也不是完整绝对路径（缺盘符）。
    """
    root = "C:/" if sys.platform == "win32" else "/"
    return root + "/".join(parts)


def pytest_configure(config):
    """Register custom pytest markers."""
    config.addinivalue_line(
        "markers",
        "slow: mark test as slow (skipped in default runs)",
    )
    config.addinivalue_line(
        "markers",
        "stata: mark test that requires a real Stata installation",
    )
    config.addinivalue_line(
        "markers",
        "path_sandbox: mark test that exercises path sandbox validation",
    )


# Ensure server.py can be imported as a plain module.

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

# Make server.py's Stata utilities directory check pass without real Stata.
_real_isdir = os.path.isdir


def _fake_isdir(path):
    if isinstance(path, str) and path.rstrip(os.sep).endswith("utilities"):
        return True
    return _real_isdir(path)


if os.path.isdir is not _fake_isdir:
    os.path.isdir = _fake_isdir

    def _restore_isdir():
        os.path.isdir = _real_isdir

    atexit.register(_restore_isdir)

# Stub pystata so that importing server.py does not require an actual Stata install.
if "pystata" not in sys.modules:
    _mock_pystata = MagicMock()
    _mock_config = MagicMock()
    _mock_pystata.config = _mock_config
    _mock_config.stversion = "Mock"
    _mock_config.stedition = "mp"
    _mock_config.sthome = "/mock/stata"
    _mock_config.stconfig = {}
    _mock_config.stlib = MagicMock()
    _mock_config.is_stata_initialized.return_value = False

    _mock_pystata_core = MagicMock()
    _mock_pystata.core = _mock_pystata_core

    sys.modules["pystata"] = _mock_pystata
    sys.modules["pystata.core"] = _mock_pystata_core

# Stub sfi (shipped with Stata) — server.py uses SFIToolkit.getTempFile() to
# materialize multi-line blocks. Hand back a real temp file so the write path
# under test behaves like production instead of silently accepting a MagicMock.
if "sfi" not in sys.modules:
    import tempfile

    _mock_sfi = MagicMock()

    def _fake_get_temp_file():
        fd, path = tempfile.mkstemp(suffix=".do", prefix="stata_mcp_test_")
        os.close(fd)
        return path

    _mock_sfi.SFIToolkit.getTempFile = _fake_get_temp_file
    sys.modules["sfi"] = _mock_sfi


@pytest.fixture(scope="session", autouse=True)
def _isolate_server_log():
    """测试不污染生产日志 logs/stata-mcp.log。

    实战发现：server.py 在 import 时（模块级）挂上 RotatingFileHandler，pytest
    用例的错误/异常会经 logger 写入生产日志，stata_read_log 因此读到本机跑过
    pytest 后混入的 traceback 与 mock 噪音。摘掉文件 handler，保留 stderr。
    """
    import logging
    import logging.handlers

    import server

    server.logger.handlers = [
        h for h in server.logger.handlers if not isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    yield


@pytest.fixture(autouse=True)
def _reset_server_globals():
    """重置 server 的模块级可变状态，消除测试顺序相关性。

    _last_ping_time 会被多个用例真实写入（stata_ping 成功路径、_ping_stata），
    残留值会让后续用例在 2 秒缓存窗口内跳过心跳，走上与单独运行时不同的分支；
    _last_output 同理会让 stata_more 的用例读到上一个用例的输出。
    _resource_registry / _bg_tasks 由资源与后台任务用例写入，残留会污染后续用例。
    _ALLOWED_ROOTS_CACHE 由路径沙箱用例写入（第二轮审查发现漏重置），残留会让
    后续用例沿用前一个用例的 STATA_ALLOWED_ROOTS 白名单。
    """
    import server

    server._last_ping_time = 0.0
    server._ALLOWED_ROOTS_CACHE = None
    with server._output_lock:
        server._last_output = ""
    with server._resource_lock:
        server._resource_registry.clear()
    with server._bg_lock:
        server._bg_tasks.clear()
    yield
    server._ALLOWED_ROOTS_CACHE = None
    server._last_ping_time = 0.0
    with server._resource_lock:
        server._resource_registry.clear()
    with server._bg_lock:
        server._bg_tasks.clear()
