"""Tests for the repo-root ``setup.py`` installer.

``setup.py`` 是每个新用户第一个运行的脚本，而 ``generate_mcp_json`` 是整个项目里
唯一**写用户真实配置文件**的代码 —— 它此前完全没有测试。这里优先覆盖那些「写坏了
用户数据也不会有人发现」的路径：合并语义、非常规文件内容、写入失败。

``setup.py`` 的顶层只有定义与常量（入口在 ``if __name__ == "__main__"`` 之下），
可安全导入。
"""

import importlib.util
import json
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_setup():
    """按路径加载仓库根的 setup.py（不能用 import setup —— 会撞包名）。"""
    path = os.path.join(_REPO_ROOT, "setup.py")
    spec = importlib.util.spec_from_file_location("stata_mcp_setup", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["stata_mcp_setup"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def setup_mod():
    return _load_setup()


def _write_json(path, payload):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# --- 合并语义 -----------------------------------------------------------------


def test_generate_mcp_json_creates_file(tmp_path, setup_mod):
    setup_mod.generate_mcp_json(str(tmp_path), "/py", "/opt/Stata", "se")

    data = _read_json(tmp_path / ".mcp.json")
    stata = data["mcpServers"]["stata"]
    assert stata["command"] == "/py"
    assert stata["env"]["STATA_HOME"] == "/opt/Stata"
    assert stata["env"]["STATA_EDITION"] == "se"


def test_generate_mcp_json_preserves_other_servers(tmp_path, setup_mod):
    target = tmp_path / ".mcp.json"
    _write_json(target, {"mcpServers": {"other": {"command": "node", "args": ["x.js"]}}})

    setup_mod.generate_mcp_json(str(tmp_path), "/py", "/opt/Stata")

    servers = _read_json(target)["mcpServers"]
    assert servers["other"] == {"command": "node", "args": ["x.js"]}
    assert "stata" in servers


def test_generate_mcp_json_preserves_top_level_keys(tmp_path, setup_mod):
    target = tmp_path / ".mcp.json"
    _write_json(target, {"$schema": "https://example.com/s.json", "mcpServers": {}})

    setup_mod.generate_mcp_json(str(tmp_path), "/py", "/opt/Stata")

    assert _read_json(target)["$schema"] == "https://example.com/s.json"


def test_generate_mcp_json_preserves_user_env_vars(tmp_path, setup_mod):
    """用户按提示手加的 STATA_ALLOWED_ROOTS 等不能被重跑抹掉。"""
    target = tmp_path / ".mcp.json"
    _write_json(
        target,
        {
            "mcpServers": {
                "stata": {
                    "command": "/old/py",
                    "args": ["/old/server.py"],
                    "env": {
                        "STATA_HOME": "/old/Stata",
                        "STATA_ALLOWED_ROOTS": "/data;/projects",
                        "STATA_ALLOW_UNC": "1",
                    },
                }
            }
        },
    )

    setup_mod.generate_mcp_json(str(tmp_path), "/new/py", "/new/Stata", "be")

    env = _read_json(target)["mcpServers"]["stata"]["env"]
    assert env["STATA_ALLOWED_ROOTS"] == "/data;/projects"
    assert env["STATA_ALLOW_UNC"] == "1"
    assert env["STATA_HOME"] == "/new/Stata"
    assert env["STATA_EDITION"] == "be"


def test_generate_mcp_json_preserves_user_keys_on_stata_entry(tmp_path, setup_mod):
    """stata 条目上用户自加的键（type/cwd/disabled…）同样不能被整条重建抹掉。

    此前 ``servers["stata"] = {...}`` 直接替换整个条目，只有 ``env`` 被搬运；
    用户为解决 MCP 客户端差异手加的 ``type``/``cwd`` 会在每次重跑后消失，且
    没有任何提示。
    """
    target = tmp_path / ".mcp.json"
    _write_json(
        target,
        {
            "mcpServers": {
                "stata": {
                    "command": "/old/py",
                    "args": ["/old/server.py"],
                    "type": "stdio",
                    "cwd": "/work",
                    "env": {},
                }
            }
        },
    )

    setup_mod.generate_mcp_json(str(tmp_path), "/new/py", "/new/Stata")

    entry = _read_json(target)["mcpServers"]["stata"]
    assert entry["type"] == "stdio"
    assert entry["cwd"] == "/work"
    # 本脚本负责的两项仍被更新
    assert entry["command"] == "/new/py"


# --- 非常规文件内容 ------------------------------------------------------------


def test_generate_mcp_json_backs_up_unparsable_file(tmp_path, setup_mod):
    target = tmp_path / ".mcp.json"
    target.write_text("{ this is not json", encoding="utf-8")

    setup_mod.generate_mcp_json(str(tmp_path), "/py", "/opt/Stata")

    assert (tmp_path / ".mcp.json.bak").read_text(encoding="utf-8") == "{ this is not json"
    assert "stata" in _read_json(target)["mcpServers"]


@pytest.mark.parametrize("payload", [[], "text", 42, None])
def test_generate_mcp_json_backs_up_non_dict_json(tmp_path, payload, setup_mod):
    """合法 JSON 但顶层非 dict 时也要备份 —— 此前这条路径静默丢弃用户数据。

    备份保护只挂在 ``except (OSError, JSONDecodeError)`` 上；``json.load`` 成功
    而 ``isinstance(loaded, dict)`` 为假时既不备份也不提示，整文件直接被覆盖。
    """
    target = tmp_path / ".mcp.json"
    _write_json(target, payload)
    original = target.read_text(encoding="utf-8")

    setup_mod.generate_mcp_json(str(tmp_path), "/py", "/opt/Stata")

    assert (tmp_path / ".mcp.json.bak").read_text(encoding="utf-8") == original
    assert "stata" in _read_json(target)["mcpServers"]


def test_generate_mcp_json_backs_up_non_dict_mcpservers(tmp_path, setup_mod):
    target = tmp_path / ".mcp.json"
    _write_json(target, {"mcpServers": ["not", "a", "dict"], "keep": 1})
    original = target.read_text(encoding="utf-8")

    setup_mod.generate_mcp_json(str(tmp_path), "/py", "/opt/Stata")

    assert (tmp_path / ".mcp.json.bak").read_text(encoding="utf-8") == original
    data = _read_json(target)
    assert data["keep"] == 1
    assert "stata" in data["mcpServers"]


# --- 写入失败 ------------------------------------------------------------------


def test_generate_mcp_json_leaves_file_intact_on_write_failure(tmp_path, setup_mod, monkeypatch):
    """写入中途失败不得把用户配置截成空文件或半截 JSON。

    唯一的写入路径此前是 ``open(path, "w")`` 截断后 ``json.dump``：磁盘满、
    Ctrl-C 或进程被杀都会留下残骸，而下次重跑时读取侧的「备份保护」会把残骸
    备份走并只重建 stata 条目 —— 原始数据永久丢失，备份形同误导。
    """
    target = tmp_path / ".mcp.json"
    _write_json(target, {"mcpServers": {"other": {"command": "node"}}})
    original = target.read_text(encoding="utf-8")

    real_replace = os.replace

    def boom(src, dst):
        if str(dst).endswith(".mcp.json"):
            raise OSError(28, "No space left on device")
        return real_replace(src, dst)

    monkeypatch.setattr(setup_mod.os, "replace", boom)
    ok = setup_mod.generate_mcp_json(str(tmp_path), "/py", "/opt/Stata")

    assert ok is False
    assert target.read_text(encoding="utf-8") == original


def test_generate_mcp_json_leaves_no_temp_file_behind(tmp_path, setup_mod):
    setup_mod.generate_mcp_json(str(tmp_path), "/py", "/opt/Stata")
    assert [p.name for p in tmp_path.iterdir()] == [".mcp.json"]


def test_generate_mcp_json_returns_true_on_success(tmp_path, setup_mod):
    assert setup_mod.generate_mcp_json(str(tmp_path), "/py", "/opt/Stata") is True


# --- STATA_HOME 环境变量 --------------------------------------------------------


def test_find_stata_warns_when_env_home_missing(tmp_path, setup_mod, monkeypatch, capsys):
    """STATA_HOME 已设置但目录不存在时必须提示，不能静默降级到自动检测。

    环境变量是文档声明的最高优先级。外置卷未挂载或路径笔误时，静默改用
    /Applications 下扫到的**另一套** Stata 写进 .mcp.json，用户会以为配置的是
    自己显式指定的那套。
    """
    monkeypatch.setenv("STATA_HOME", str(tmp_path / "not-mounted"))
    monkeypatch.setattr(setup_mod, "STATA_COMMON_PATHS", [])
    monkeypatch.setattr(setup_mod.sys, "platform", "linux")

    setup_mod.find_stata_installation()

    out = capsys.readouterr().out
    assert "STATA_HOME" in out
    assert "not-mounted" in out


def test_find_stata_uses_env_home_when_valid(tmp_path, setup_mod, monkeypatch):
    home = tmp_path / "StataNow"
    (home / "utilities" / "pystata").mkdir(parents=True)
    monkeypatch.setenv("STATA_HOME", str(home))

    path, _edition = setup_mod.find_stata_installation()

    assert path == str(home)


# --- 依赖安装 ------------------------------------------------------------------


def test_install_deps_pins_fastmcp_lower_bound(tmp_path, setup_mod, monkeypatch):
    """安装命令必须带上 ``>=3.2.0`` 下界。

    server.py 从 ``fastmcp.tools.base`` 导入 ToolResult，该模块 3.2.0 才出现，
    低版本 import 即崩。而这是新用户唯一的自动安装路径，此前装的是无版本约束的
    裸 ``fastmcp``：venv 里若已有更低版本，uv/pip 报 already-satisfied rc=0，
    install_deps 打印 ✓ 成功，直到 Step 4 才以截尾 stderr 暴露 ModuleNotFoundError
    —— 诊断远离根因。requirements.txt / pyproject.toml 里的下界声明无人消费。
    """
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(setup_mod.shutil, "which", lambda name: "/usr/bin/uv")
    monkeypatch.setattr(setup_mod.subprocess, "run", fake_run)

    assert setup_mod.install_deps(str(tmp_path), _REPO_ROOT) is True
    spec = [tok for cmd in calls for tok in cmd if tok.startswith("fastmcp")]
    assert spec and all(">=3.2.0" in tok for tok in spec), calls


def test_install_deps_pip_fallback_pins_lower_bound(tmp_path, setup_mod, monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(setup_mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(setup_mod.subprocess, "run", fake_run)

    assert setup_mod.install_deps(str(tmp_path), _REPO_ROOT) is True
    spec = [tok for cmd in calls for tok in cmd if tok.startswith("fastmcp")]
    assert spec and all(">=3.2.0" in tok for tok in spec), calls


def test_install_deps_reports_timeout_instead_of_raising(tmp_path, setup_mod, monkeypatch):
    """慢网络下 pip 超时应给出可操作提示，而不是裸 traceback。

    五处 subprocess.run 都传了 timeout 却无一捕获 TimeoutExpired（它不是
    OSError），而 ``pip install fastmcp`` 要拉 pydantic/uvicorn/starlette 一串
    依赖，慢网络超 180s 完全现实。
    """

    def fake_run(cmd, **kwargs):
        raise setup_mod.subprocess.TimeoutExpired(cmd, 180)

    monkeypatch.setattr(setup_mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(setup_mod.subprocess, "run", fake_run)

    assert setup_mod.install_deps(str(tmp_path), _REPO_ROOT) is False


def test_fastmcp_spec_matches_requirements_and_pyproject(setup_mod):
    """setup.py 的下界必须与 requirements.txt / pyproject.toml 一致。

    三处各自声明同一个约束，而只有 setup.py 那处会被新用户实际执行 ——
    没有守卫就会像此前那样漂移（元数据写 >=3.2.0，安装的却是裸 fastmcp）。
    """
    import re

    server_dir = os.path.join(_REPO_ROOT, "mcp-stata-server")
    spec = setup_mod.FASTMCP_SPEC
    bound = re.match(r"fastmcp(>=[\d.]+)", spec)
    assert bound, spec

    req = open(os.path.join(server_dir, "requirements.txt"), encoding="utf-8").read()
    assert f"fastmcp{bound.group(1)}" in req

    pyproject = open(os.path.join(server_dir, "pyproject.toml"), encoding="utf-8").read()
    assert f"fastmcp{bound.group(1)}" in pyproject


def test_generate_mcp_json_reports_preserved_custom_env(tmp_path, setup_mod, capsys):
    """保留了自定义环境变量时要说出来。"""
    _write_json(
        tmp_path / ".mcp.json",
        {"mcpServers": {"stata": {"env": {"STATA_ALLOWED_ROOTS": "/data"}}}},
    )

    setup_mod.generate_mcp_json(str(tmp_path), "/py", "/opt/Stata")

    assert "STATA_ALLOWED_ROOTS" in capsys.readouterr().out


def test_generate_mcp_json_hints_optional_env_when_none_set(tmp_path, setup_mod, capsys):
    """没有自定义环境变量时，提示可以加哪些。"""
    setup_mod.generate_mcp_json(str(tmp_path), "/py", "/opt/Stata")

    out = capsys.readouterr().out
    assert "可选环境变量" in out
    assert "STATA_ALLOWED_ROOTS" in out
