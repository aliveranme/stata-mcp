"""pytest configuration for stata-mcp-server tests."""

import atexit
import os
import sys
from unittest.mock import MagicMock


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
