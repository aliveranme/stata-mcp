import os
import sys
import atexit
from unittest.mock import MagicMock

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
