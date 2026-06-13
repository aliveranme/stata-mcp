import asyncio
import importlib.util
import os
import sys

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SERVER_PATH = os.path.join(_SERVER_DIR, "server.py")


def test_tools_registered_via_importlib():
    """Load server.py via importlib and verify at least 20 MCP tools are registered."""
    module_name = "stata_server_test_import"
    if module_name in sys.modules:
        del sys.modules[module_name]

    spec = importlib.util.spec_from_file_location(module_name, _SERVER_PATH)
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)

    tools = asyncio.run(server.mcp.list_tools())
    assert len(tools) >= 20, f"Expected >= 20 registered tools, got {len(tools)}"
