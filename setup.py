#!/usr/bin/env python3
"""
Stata MCP Server — 一键安装配置脚本

自动完成：
  1. 检测/查找 Stata 安装路径
  2. 创建 Python 虚拟环境
  3. 安装 FastMCP 依赖
  4. 生成 .mcp.json 配置文件
  5. 验证服务器可正常启动
"""

import sys
import os
import subprocess
import shutil
from pathlib import Path


# =============================================================================
# 颜色输出
# =============================================================================

def green(s): return f"\033[92m{s}\033[0m"
def red(s): return f"\033[91m{s}\033[0m"
def yellow(s): return f"\033[93m{s}\033[0m"
def bold(s): return f"\033[1m{s}\033[0m"


# =============================================================================
# Step 1: 检测 Stata 安装
# =============================================================================

STATA_COMMON_PATHS = [
    "D:/StataNow19",
    "C:/Program Files/StataNow19",
    "C:/Program Files (x86)/StataNow19",
    "C:/Program Files/StataNow",
    "C:/Program Files/Stata18",
    "C:/Program Files/Stata17",
    "C:/Program Files (x86)/Stata18",
    "C:/Program Files (x86)/Stata17",
    "D:/StataNow",
    "D:/Stata18",
    "D:/Stata17",
    "E:/Stata18",
    "E:/Stata17",
]

STATA_EDITIONS = ["mp", "se", "be"]


def _detect_edition(path):
    """检测路径中可用的 Stata 版本（mp/se/be），优先 mp。"""
    for edition in STATA_EDITIONS:
        dll = os.path.join(path, f"{edition}-64.dll")
        if os.path.isfile(dll):
            return edition
    return None


def find_stata_installation():
    """查找 Stata 安装目录和版本，返回 (path, edition)。"""
    # 1. 检查环境变量
    env_home = os.environ.get("STATA_HOME")
    if env_home and os.path.isdir(env_home):
        edition = _detect_edition(env_home) or "mp"
        return env_home, edition

    # 2. 检查常见路径
    for path in STATA_COMMON_PATHS:
        if os.path.isdir(path):
            edition = _detect_edition(path)
            utilities = os.path.join(path, "utilities", "pystata")
            if edition and os.path.isdir(utilities):
                return path, edition

    # 3. 搜索 Program Files
    prog = os.environ.get("ProgramFiles", "C:/Program Files")
    prog_x86 = os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")
    for base in [prog, prog_x86, "D:/", "E:/"]:
        if not os.path.isdir(base):
            continue
        try:
            for entry in os.listdir(base):
                if entry.lower().startswith("stata"):
                    full = os.path.join(base, entry)
                    if os.path.isdir(full):
                        edition = _detect_edition(full)
                        utilities = os.path.join(full, "utilities", "pystata")
                        if edition and os.path.isdir(utilities):
                            return full, edition
        except PermissionError:
            continue

    return None, None


def verify_stata(path, edition="mp"):
    """验证 Stata 安装是否包含必要组件。"""
    dll = os.path.join(path, f"{edition}-64.dll")
    pystata = os.path.join(path, "utilities", "pystata")
    errors = []
    if not os.path.isfile(dll):
        errors.append(f"缺少 {edition}-64.dll: {dll}")
    if not os.path.isdir(pystata):
        errors.append(f"缺少 pystata: {pystata}")
    return errors


# =============================================================================
# Step 2: Python 虚拟环境
# =============================================================================

def setup_venv(project_root):
    """创建或验证 Python 虚拟环境。"""
    venv_dir = os.path.join(project_root, "mcp-stata-server", ".venv")

    if os.path.isdir(venv_dir):
        print(f"  {green('✓')} 虚拟环境已存在: {venv_dir}")
        return venv_dir

    print(f"  正在创建虚拟环境...")
    result = subprocess.run(
        [sys.executable, "-m", "venv", venv_dir],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        print(f"  {red('✗')} 创建失败:\n{result.stderr}")
        return None

    print(f"  {green('✓')} 虚拟环境已创建")
    return venv_dir


def get_python_exe(venv_dir):
    """获取 venv 中的 Python 可执行文件路径。"""
    return os.path.join(venv_dir, "Scripts", "python.exe")


def install_deps(venv_dir, project_root):
    """安装 FastMCP。使用 uv 或 pip。"""
    server_dir = os.path.join(project_root, "mcp-stata-server")

    # 先尝试 uv（更可靠）
    if shutil.which("uv"):
        print(f"  使用 uv 安装 fastmcp...")
        result = subprocess.run(
            ["uv", "pip", "install", "fastmcp", "--python", get_python_exe(venv_dir)],
            cwd=server_dir,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode == 0:
            print(f"  {green('✓')} fastmcp 已安装")
            return True
        print(f"  {yellow('⚠')} uv 安装失败，尝试 pip...")

    # 回退到 pip（需先 ensurepip）
    python_exe = get_python_exe(venv_dir)
    result = subprocess.run(
        [python_exe, "-m", "ensurepip", "--default-pip"],
        capture_output=True, text=True, timeout=60,
    )
    result = subprocess.run(
        [python_exe, "-m", "pip", "install", "fastmcp", "--quiet"],
        cwd=server_dir,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0:
        print(f"  {red('✗')} 安装失败:\n{result.stderr}")
        return False

    print(f"  {green('✓')} fastmcp 已安装")
    return True


# =============================================================================
# Step 3: 生成 .mcp.json
# =============================================================================

def generate_mcp_json(project_root, python_exe, stata_home, stata_edition="mp"):
    """生成 .mcp.json 配置文件。"""
    server_script = os.path.join(project_root, "mcp-stata-server", "server.py")
    server_script = os.path.normpath(server_script).replace("\\", "/")

    mcp_config = {
        "mcpServers": {
            "stata": {
                "command": python_exe.replace("\\", "/"),
                "args": [server_script],
                "env": {
                    "STATA_HOME": stata_home.replace("\\", "/"),
                    "STATA_EDITION": stata_edition
                }
            }
        }
    }

    import json
    mcp_json_path = os.path.join(project_root, ".mcp.json")
    with open(mcp_json_path, "w", encoding="utf-8") as f:
        json.dump(mcp_config, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"  {green('✓')} .mcp.json 已生成")
    print(f"    Stata: {stata_home}")
    print(f"    Python: {python_exe}")

    return mcp_json_path


# =============================================================================
# Step 4: 验证
# =============================================================================

def test_server(project_root, python_exe, stata_home, stata_edition="mp"):
    """测试 MCP Server 能否正常加载。

    使用 importlib 加载 server 模块并枚举工具数量。
    在子进程中执行以避免污染主进程状态。
    """
    import json
    server_script = os.path.join(project_root, "mcp-stata-server", "server.py")
    env = os.environ.copy()
    env["STATA_HOME"] = stata_home
    env["STATA_EDITION"] = stata_edition

    print(f"  正在测试服务器...")

    # 写入临时测试脚本（比 exec() 字符串拼接更可靠）
    test_script = os.path.join(project_root, "mcp-stata-server", "_test_bootstrap.py")
    with open(test_script, "w", encoding="utf-8") as f:
        f.write(
            '"""Bootstrap test — imported by setup.py to verify MCP server."""\n'
            "import sys, os\n"
            f"sys.path.insert(0, {repr(os.path.join(stata_home, 'utilities'))})\n"
            "os.environ['STATA_HOME'] = " + repr(stata_home) + "\n"
            f"os.environ['STATA_EDITION'] = {repr(stata_edition)}\n"
            "import importlib.util\n"
            "spec = importlib.util.spec_from_file_location(\n"
            "    'stata_server', " + repr(server_script) + "\n"
            ")\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(mod)\n"
            "import asyncio\n"
            "async def _list():\n"
            "    tools = await mod.mcp.list_tools()\n"
            "    print('TOOLS:' + str(len(tools)))\n"
            "asyncio.run(_list())\n"
        )

    try:
        result = subprocess.run(
            [python_exe, test_script],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=os.path.join(project_root, "mcp-stata-server"),
        )

        # 在 stdout/stderr 中查找 TOOLS: 标记
        combined = (result.stdout or "") + (result.stderr or "")
        if "TOOLS:" in combined:
            n_tools = combined.split("TOOLS:")[-1].strip().split()[0]
            print(f"  {green('✓')} 服务器正常 — 注册了 {n_tools} 个工具")
            return True
        else:
            print(f"  {red('✗')} 服务器测试失败")
            if result.stderr.strip():
                lines = [l for l in result.stderr.strip().split("\n") if "TOOLS:" not in l]
                for l in lines[-3:]:
                    print(f"    {l[:200]}")
            return False
    finally:
        if os.path.isfile(test_script):
            os.unlink(test_script)


# =============================================================================
# 主流程
# =============================================================================

def main():
    print()
    print(bold("===== Stata MCP Server 安装配置 ====="))
    print()

    project_root = os.path.dirname(os.path.abspath(__file__))
    print(f"项目目录: {project_root}")
    print()

    # ---- Step 1: 查找 Stata ----
    print(bold("Step 1: 检测 Stata 安装"))
    stata_home, stata_edition = find_stata_installation()

    if stata_home:
        print(f"  {green('✓')} 找到 Stata: {stata_home} ({stata_edition}-64)")
        errors = verify_stata(stata_home, stata_edition)
        if errors:
            for e in errors:
                print(f"  {yellow('⚠')} {e}")
            stata_home = None
    else:
        print(f"  {yellow('⚠')} 未自动找到 Stata")

    if not stata_home:
        print()
        print("  请手动输入 Stata 安装目录（例如 D:/StataNow19）：")
        user_input = input("  > ").strip().strip('"')
        if user_input and os.path.isdir(user_input):
            stata_home = user_input
            stata_edition = _detect_edition(stata_home) or "mp"
            errors = verify_stata(stata_home, stata_edition)
            if errors:
                for e in errors:
                    print(f"  {red('✗')} {e}")
                print(f"  {red('安装验证失败，请确认目录正确')}")
                return 1
        else:
            print(f"  {red('✗')} 无效路径，安装取消")
            return 1

    print()

    # ---- Step 2: 设置虚拟环境 ----
    print(bold("Step 2: Python 虚拟环境"))
    venv_dir = setup_venv(project_root)
    if not venv_dir:
        return 1
    python_exe = get_python_exe(venv_dir)

    if not install_deps(venv_dir, project_root):
        return 1
    print()

    # ---- Step 3: 生成配置 ----
    print(bold("Step 3: 生成 .mcp.json"))
    generate_mcp_json(project_root, python_exe, stata_home, stata_edition)
    print()

    # ---- Step 4: 验证 ----
    print(bold("Step 4: 验证"))
    if not test_server(project_root, python_exe, stata_home, stata_edition):
        print()
        print(f"  {red('✗')} 服务器验证未通过，请检查上面的错误信息")
        return 1
    print()

    # ---- 完成 ----
    print(bold("===== 安装完成 ====="))
    print()
    print("后续步骤：")
    print("  1. 重启 Claude Code（或运行 /reload-plugins）")
    print("  2. 测试：输入 '帮我加载 auto.dta 数据并做描述统计'")
    print()
    print(f"配置已保存到: {os.path.join(project_root, '.mcp.json')}")
    print(f"如需修改 Stata 路径，编辑此文件的 STATA_HOME 环境变量即可")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
