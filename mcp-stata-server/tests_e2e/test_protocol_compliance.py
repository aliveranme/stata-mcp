"""MCP 协议符合性测试（对照规范 2025-11-25）。

通过真实 stdio 进程（node index.js → python server.py）发 JSON-RPC 消息，
验证响应符合 MCP 规范：initialize 握手、capabilities 协商、tools/list 结构、
JSON-RPC 错误处理、resources 模板、prompts、协议版本协商。

运行：``STATA_HOME=/... .venv/bin/python -m pytest tests_e2e/test_protocol_compliance.py -q``
"""

import json
import os
import select
import subprocess
import time

import pytest

from tests_e2e.conftest import SKIP_REASON, STATA_AVAILABLE

pytestmark = [
    pytest.mark.stata,
    pytest.mark.skipif(not STATA_AVAILABLE, reason=SKIP_REASON),
]


class McpClient:
    """极简 MCP 客户端：spawn node index.js，保持 stdin open，收发 JSON-RPC。"""

    def __init__(self):
        env = dict(
            os.environ,
            STATA_HOME=os.environ["STATA_HOME"],
            PYTHON=os.environ.get(
                "PYTHON",
                os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".venv", "bin", "python"),
            ),
        )
        idx = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "npm-package", "index.js")
        self.proc = subprocess.Popen(
            ["node", idx], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, env=env,
        )
        time.sleep(10)  # 等 Stata 初始化 + FastMCP banner

    def send(self, msg):
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def recv(self, timeout=8):
        deadline = time.time() + timeout
        while time.time() < deadline:
            r, _, _ = select.select([self.proc.stdout], [], [], 0.4)
            if r:
                line = self.proc.stdout.readline()
                if line:
                    try:
                        return json.loads(line)
                    except ValueError:
                        continue
        return None

    def close(self):
        self.proc.terminate()
        self.proc.wait(timeout=5)


@pytest.fixture(scope="module")
def client():
    c = McpClient()
    yield c
    c.close()


@pytest.fixture()
def session(client):
    """initialize + initialized 握手。"""
    client.send({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                   "clientInfo": {"name": "pt", "version": "1.0"}},
    })
    init = client.recv()
    client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    time.sleep(0.3)
    return init


def test_initialize_handshake(session):
    """initialize 返回 serverInfo + capabilities + 协商的 protocolVersion。"""
    assert "result" in session, session
    r = session["result"]
    assert r.get("protocolVersion") == "2025-11-25"
    assert "serverInfo" in r and "name" in r["serverInfo"]
    caps = r.get("capabilities", {})
    assert "tools" in caps          # 我们声明了 tools
    assert "resources" in caps      # 资源模板
    assert "prompts" in caps
    assert "instructions" in r      # 服务器说明


def test_tools_list_structure(session, client):
    """tools/list 返回的每项含 name/description/inputSchema。"""
    client.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    r = client.recv()
    tools = r["result"]["tools"]
    assert len(tools) >= 75
    for t in tools[:5]:
        assert {"name", "description", "inputSchema"} <= set(t.keys()), t


def test_tool_call_result_structure(session, client):
    """tools/call 返回 content 数组 + isError 标志。"""
    client.send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "stata_ping", "arguments": {}}})
    r = client.recv()
    result = r["result"]
    assert "content" in result and isinstance(result["content"], list)
    assert "isError" in result
    assert "pong" in result["content"][0].get("text", "")  # 真实 Stata 调用


def test_unknown_method_returns_jsonrpc_error(session, client):
    """未知方法返回 JSON-RPC error（含 code/message）。"""
    client.send({"jsonrpc": "2.0", "id": 9, "method": "bogus/method", "params": {}})
    r = client.recv()
    assert "error" in r
    assert "code" in r["error"] and "message" in r["error"]
    assert r.get("id") == 9


def test_tool_call_bad_arguments_returns_iserror(session, client):
    """tools/call 错误参数 → isError=true（而非协议级 error）。"""
    client.send({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                 "params": {"name": "stata_ping", "arguments": {"bogus": 1}}})
    r = client.recv()
    assert r["result"]["isError"] is True


def test_resources_template_declared(session, client):
    """资源模板 stato-file:///{path*} 已声明（规范 resources/templates）。"""
    client.send({"jsonrpc": "2.0", "id": 5, "method": "resources/templates/list", "params": {}})
    r = client.recv()
    templates = r["result"].get("resourceTemplates", [])
    assert any("stata-file" in t.get("uriTemplate", "") for t in templates)


def test_prompts_list_valid(session, client):
    """prompts/list 返回合法结构（可为空）。"""
    client.send({"jsonrpc": "2.0", "id": 6, "method": "prompts/list", "params": {}})
    r = client.recv()
    assert "prompts" in r["result"]


def test_notification_gets_no_response(session, client):
    """notification（无 id）不应有响应。"""
    client.send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                 "params": {"requestId": 999, "reason": "test"}})
    assert client.recv(timeout=1.5) is None
