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

import os
import shutil
import subprocess
import sys
import tempfile

# Python 版本要求，需与 pyproject.toml 中的 requires-python 保持一致
MIN_PYTHON_VERSION = (3, 10)


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

if sys.platform == "win32":
    STATA_COMMON_PATHS = [
        # StataNow 19.x
        "C:/Program Files/StataNow19.5",
        "C:/Program Files/StataNow19",
        "C:/Program Files (x86)/StataNow19.5",
        "C:/Program Files (x86)/StataNow19",
        "C:/Program Files/StataNow",
        # 嵌套一层的安装布局 —— 正是 CLAUDE.md / README 里写的 STATA_HOME 默认值
        "C:/Program Files/StataNow/StataNow19",
        "C:/Program Files/StataNow/StataNow19.5",
        "C:/Program Files/StataNow/StataNow18",
        "D:/StataNow19.5",
        "D:/StataNow19",
        "D:/StataNow",
        "E:/StataNow19.5",
        "E:/StataNow19",
        "E:/StataNow",
        # Stata 18 / 17
        "C:/Program Files/Stata18",
        "C:/Program Files/Stata17",
        "C:/Program Files (x86)/Stata18",
        "C:/Program Files (x86)/Stata17",
        "D:/Stata18",
        "D:/Stata17",
        "E:/Stata18",
        "E:/Stata17",
        # 显式 edition 子目录（某些安装方式）
        "C:/Program Files/StataMP18",
        "C:/Program Files/StataSE18",
        "C:/Program Files/StataBE18",
        "C:/Program Files/StataMP17",
        "C:/Program Files/StataSE17",
        "C:/Program Files/StataBE17",
        "D:/StataMP18",
        "D:/StataSE18",
        "D:/StataBE18",
        "D:/StataMP17",
        "D:/StataSE17",
        "D:/StataBE17",
    ]
elif sys.platform == "darwin":
    # 必须是含 utilities/pystata 的**安装根目录**，不是 .app bundle 内部 ——
    # StataMP.app/Contents/MacOS 下只有可执行文件与 dylib，没有 utilities。
    STATA_COMMON_PATHS = [
        "/Applications/Stata",
        "/Applications/StataNow",
        "/Applications/Stata19",
        "/Applications/Stata18",
        "/Applications/Stata17",
        os.path.expanduser("~/Applications/Stata"),
        os.path.expanduser("~/Applications/StataNow"),
    ]
else:
    STATA_COMMON_PATHS = [
        "/usr/local/stata",
        "/usr/local/stata19",
        "/usr/local/stata18",
        "/usr/local/stata17",
        "/opt/stata",
        "/opt/stata19",
        "/opt/stata18",
        "/opt/stata17",
    ]

STATA_EDITIONS = ["mp", "se", "be"]


def _edition_artifacts(root, edition):
    """返回该 edition 在当前平台上的特征文件候选（存在任一即视为该版本可用）。

    Stata 的运行时文件命名随平台而变，只查 ``{edition}-64.dll`` 会让检测在
    macOS 与 Linux 上永远失败（实测 macOS 上 ``find_stata_installation``
    因此返回 ``(None, None)``，手动指定根目录也会被 ``verify_stata`` 拒绝）：

    - Windows: ``<root>/mp-64.dll``
    - macOS:   ``<root>/StataMP.app/Contents/MacOS/libstata-mp.dylib``
    - Linux:   ``<root>/libstata-mp.so`` 或可执行文件 ``<root>/stata-mp``
    """
    if sys.platform == "win32":
        return [os.path.join(root, f"{edition}-64.dll")]
    if sys.platform == "darwin":
        app = os.path.join(root, f"Stata{edition.upper()}.app")
        return [
            os.path.join(app, "Contents", "MacOS", f"libstata-{edition}.dylib"),
            app,
        ]
    return [
        os.path.join(root, f"libstata-{edition}.so"),
        os.path.join(root, f"stata-{edition}"),
    ]


def _detect_edition(path):
    """检测路径中可用的 Stata 版本（mp/se/be），优先 mp。"""
    for edition in STATA_EDITIONS:
        if any(os.path.exists(p) for p in _edition_artifacts(path, edition)):
            return edition
    return None


def _edition_with_warning(path):
    """尝试检测 edition；若无法检测则默认 mp 并打印警告。"""
    edition = _detect_edition(path)
    if edition is not None:
        return edition
    print(
        yellow(
            f"⚠ 无法从 {path} 检测 Stata 版本（mp/se/be 的运行时文件均未找到），"
            f"将默认使用 mp。若启动失败请手动设置 STATA_EDITION 环境变量。"
        )
    )
    return "mp"


def find_stata_installation():
    """查找 Stata 安装目录和版本，返回 (path, edition)。"""
    # 1. 检查环境变量
    env_home = os.environ.get("STATA_HOME")
    if env_home and os.path.isdir(env_home):
        edition = _edition_with_warning(env_home)
        return env_home, edition
    if env_home:
        # 环境变量是文档声明的最高优先级，被静默降级会让用户以为配置的是自己
        # 指定的那套 Stata。外置卷未挂载、路径笔误都会走到这里，而下面的自动
        # 检测可能恰好扫到**另一套** Stata 并写进 .mcp.json。
        print(
            yellow(
                f"⚠ 环境变量 STATA_HOME 指向的目录不存在，已忽略：{env_home}\n"
                "  （外置卷未挂载？路径笔误？）将改用自动检测。"
            )
        )

    # 2. 检查常见路径
    for path in STATA_COMMON_PATHS:
        if os.path.isdir(path):
            edition = _detect_edition(path)
            utilities = os.path.join(path, "utilities", "pystata")
            if edition and os.path.isdir(utilities):
                return path, edition

    # 3. 扫描当前平台的应用基目录（只进入以 "Stata" 开头的目录）
    if sys.platform == "win32":
        prog = os.environ.get("ProgramFiles", "C:/Program Files")
        prog_x86 = os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")
        search_bases = [prog, prog_x86, "D:/", "E:/"]
    elif sys.platform == "darwin":
        search_bases = ["/Applications", os.path.expanduser("~/Applications")]
    else:
        search_bases = ["/usr/local", "/opt"]
    def _looks_like_stata_root(candidate):
        edition = _detect_edition(candidate)
        utilities = os.path.join(candidate, "utilities", "pystata")
        return edition if (edition and os.path.isdir(utilities)) else None

    for base in search_bases:
        if not os.path.isdir(base):
            continue
        try:
            for entry in os.listdir(base):
                # 限定只扫描名称以 Stata 开头的目录，避免遍历无关文件夹
                if not entry.lower().startswith("stata"):
                    continue
                full = os.path.join(base, entry)
                if not os.path.isdir(full):
                    continue
                if edition := _looks_like_stata_root(full):
                    return full, edition
                # 再下探一层：常见的嵌套布局如
                # C:/Program Files/StataNow/StataNow19（也是文档给出的默认值）
                try:
                    for sub in os.listdir(full):
                        if not sub.lower().startswith("stata"):
                            continue
                        nested = os.path.join(full, sub)
                        if os.path.isdir(nested) and (edition := _looks_like_stata_root(nested)):
                            return nested, edition
                except (PermissionError, OSError):
                    continue
        except PermissionError:
            continue

    return None, None


def verify_stata(path, edition="mp"):
    """验证 Stata 安装是否包含必要组件。"""
    artifacts = _edition_artifacts(path, edition)
    pystata = os.path.join(path, "utilities", "pystata")
    errors = []
    if not any(os.path.exists(p) for p in artifacts):
        errors.append(f"缺少 {edition} 版运行时（已查找：{'、'.join(artifacts)}）")
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
        # 只看目录存在不够：中断的创建、或用已删除的 Python 建的 venv 都会留下
        # 一个没有可用解释器的空壳，后续 install_deps 会以未捕获的
        # FileNotFoundError 抛栈退出，而不是给出可操作提示。
        existing_python = get_python_exe(venv_dir)
        if os.path.isfile(existing_python):
            print(f"  {green('✓')} 虚拟环境已存在: {venv_dir}")
            return venv_dir
        print(
            f"  {yellow('⚠')} {venv_dir} 存在但缺少解释器 "
            f"（{existing_python}），判定为残缺环境，正在重建..."
        )
        try:
            shutil.rmtree(venv_dir)
        except OSError as e:
            print(f"  {red('✗')} 无法删除残缺的虚拟环境: {e}")
            print(f"    请手动删除 {venv_dir} 后重新运行 setup.py")
            return None

    print("  正在创建虚拟环境...")
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
    if sys.platform == "win32":
        return os.path.join(venv_dir, "Scripts", "python.exe")
    return os.path.join(venv_dir, "bin", "python")


# server.py 从 fastmcp.tools.base 导入 ToolResult，该模块 3.2.0 才出现 ——
# 低版本 import 即崩。安装命令必须自带下界：venv 里若已有更低版本，uv/pip 会报
# already-satisfied rc=0，install_deps 打印 ✓ 成功，直到 Step 4 才以截尾 stderr
# 暴露 ModuleNotFoundError，诊断远离根因。requirements.txt 与 pyproject.toml
# 里的同一下界没有任何自动路径会消费，故在此显式重复（有测试守住二者一致）。
FASTMCP_SPEC = "fastmcp>=3.2.0"


def install_deps(venv_dir, project_root):
    """安装 FastMCP。使用 uv 或 pip。"""
    server_dir = os.path.join(project_root, "mcp-stata-server")

    # 先尝试 uv（更可靠）
    if shutil.which("uv"):
        print(f"  使用 uv 安装 {FASTMCP_SPEC}...")
        try:
            result = subprocess.run(
                ["uv", "pip", "install", FASTMCP_SPEC, "--python", get_python_exe(venv_dir)],
                cwd=server_dir,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except subprocess.TimeoutExpired:
            print(f"  {yellow('⚠')} uv 安装超时（180 秒），尝试 pip...")
        else:
            if result.returncode == 0:
                print(f"  {green('✓')} fastmcp 已安装")
                return True
            print(f"  {yellow('⚠')} uv 安装失败，尝试 pip...")

    # 回退到 pip（需先 ensurepip）
    python_exe = get_python_exe(venv_dir)
    try:
        subprocess.run(
            [python_exe, "-m", "ensurepip", "--default-pip"],
            capture_output=True, text=True, timeout=60,
        )
        result = subprocess.run(
            [python_exe, "-m", "pip", "install", FASTMCP_SPEC, "--quiet"],
            cwd=server_dir,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired as e:
        # TimeoutExpired 不是 OSError，不捕获会以裸 traceback 退出，而 pip 要拉
        # pydantic/uvicorn/starlette 一串依赖，慢网络超时完全现实。
        print(f"  {red('✗')} 安装超时（{e.timeout} 秒）—— 网络可能较慢")
        print("    请检查网络后重跑 setup.py，或手动执行："
              f"\n      {python_exe} -m pip install {FASTMCP_SPEC}")
        return False
    if result.returncode != 0:
        print(f"  {red('✗')} 安装失败:\n{result.stderr}")
        return False

    print(f"  {green('✓')} fastmcp 已安装")
    return True


# =============================================================================
# Step 3: 生成 .mcp.json
# =============================================================================

def _backup_mcp_json(path, reason):
    """把无法使用的 .mcp.json 备份为 .bak 并说明原因。"""
    backup = path + ".bak"
    try:
        shutil.copy2(path, backup)
        print(f"  {yellow('⚠')} 现有 .mcp.json {reason}，已备份为 {backup}")
    except OSError:
        print(f"  {yellow('⚠')} 现有 .mcp.json {reason}，将被覆盖")


def generate_mcp_json(project_root, python_exe, stata_home, stata_edition="mp"):
    """写入 .mcp.json 中的 stata 条目，保留文件里的其他内容。返回是否写入成功。

    不能整文件覆盖：同一个 .mcp.json 里可能还配置了别的 MCP Server，
    stata 条目上可能有用户为适配客户端手加的键（type / cwd / disabled），
    而 stata.env 里也可能有按本函数末尾提示添加的 STATA_ALLOWED_ROOTS /
    STATA_ALLOW_UNC —— 重跑 setup.py 不能把它们抹掉。

    写入走「同目录临时文件 + os.replace 原子替换」：截断直写在中途失败
    （磁盘满、Ctrl-C、进程被杀）会留下空文件或半截 JSON，而下次重跑时下面的
    备份逻辑会把残骸备份走并只重建 stata 条目 —— 原始数据永久丢失，备份反而
    误导。原子替换保证用户看到的要么是旧文件、要么是完整的新文件。
    """
    import json

    server_script = os.path.join(project_root, "mcp-stata-server", "server.py")
    server_script = os.path.normpath(server_script).replace("\\", "/")

    mcp_json_path = os.path.join(project_root, ".mcp.json")

    existing = {}
    if os.path.isfile(mcp_json_path):
        try:
            with open(mcp_json_path, encoding="utf-8") as f:
                loaded = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            _backup_mcp_json(mcp_json_path, f"无法解析（{e}）")
        else:
            if isinstance(loaded, dict):
                existing = loaded
            else:
                # 合法 JSON 但顶层不是对象（[]、null、字符串…）—— 同样是「用不了
                # 的既有内容」，必须走与解析失败相同的备份路径，不能静默覆盖。
                _backup_mcp_json(
                    mcp_json_path, f"顶层不是 JSON 对象（{type(loaded).__name__}）"
                )

    servers = existing.get("mcpServers")
    if not isinstance(servers, dict):
        if "mcpServers" in existing:
            _backup_mcp_json(mcp_json_path, "的 mcpServers 不是 JSON 对象")
        servers = {}

    # 保留 stata 条目上用户自加的键（type/cwd/…）与环境变量（如沙箱白名单），
    # 只更新本脚本负责的那几项。
    prev_stata = servers.get("stata")
    entry = dict(prev_stata) if isinstance(prev_stata, dict) else {}
    env = entry.get("env")
    env = dict(env) if isinstance(env, dict) else {}
    env["STATA_HOME"] = stata_home.replace("\\", "/")
    env["STATA_EDITION"] = stata_edition

    entry["command"] = python_exe.replace("\\", "/")
    entry["args"] = [server_script]
    entry["env"] = env
    servers["stata"] = entry
    existing["mcpServers"] = servers

    tmp_path = mcp_json_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_path, mcp_json_path)
    except OSError as e:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        print(f"  {red('✗')} 写入 .mcp.json 失败（{e}）—— 原文件未改动")
        print(f"    请检查磁盘空间与 {mcp_json_path} 的写权限后重试。")
        return False

    others = [k for k in servers if k != "stata"]
    print(f"  {green('✓')} .mcp.json 已更新")
    print(f"    Stata: {stata_home}")
    print(f"    Python: {python_exe}")
    if others:
        print(f"    已保留其他 MCP Server: {', '.join(others)}")

    extra_env = [k for k in env if k not in ("STATA_HOME", "STATA_EDITION")]
    if extra_env:
        print(f"    已保留自定义环境变量: {', '.join(extra_env)}")
    else:
        print(f"  {yellow('可选环境变量')} （可手动添加至 .mcp.json 的 stata.env 中）：")
        print("    STATA_ALLOWED_ROOTS  分号分隔的路径沙箱白名单，例: C:/data;D:/projects")
        print("    STATA_ALLOW_UNC      设为 1 允许 UNC 网络路径（默认禁止）")

    return True


# =============================================================================
# Step 4: 验证
# =============================================================================

def test_server(project_root, python_exe, stata_home, stata_edition="mp"):
    """测试 MCP Server 能否正常加载。

    使用 importlib 加载 server 模块并枚举工具数量。
    在子进程中执行以避免污染主进程状态。
    临时脚本写入系统临时目录，避免在项目目录中残留。
    """
    server_script = os.path.join(project_root, "mcp-stata-server", "server.py")
    env = os.environ.copy()
    env["STATA_HOME"] = stata_home
    env["STATA_EDITION"] = stata_edition

    print("  正在测试服务器...")

    # 写入系统临时目录，测试结束后显式删除
    test_script = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix="_stata_mcp_bootstrap.py", delete=False, encoding="utf-8"
        ) as f:
            test_script = f.name
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
                lines = [
                    ln for ln in result.stderr.strip().split("\n") if "TOOLS:" not in ln
                ]
                for ln in lines[-3:]:
                    print(f"    {ln[:200]}")
            return False
    finally:
        if test_script and os.path.isfile(test_script):
            try:
                os.unlink(test_script)
            except OSError:
                pass


# =============================================================================
# 主流程
# =============================================================================

def main():
    # 先检查 Python 版本，避免创建不兼容的虚拟环境
    if sys.version_info < MIN_PYTHON_VERSION:
        print(
            red(
                f"✗ Python 版本过低：需要 Python {MIN_PYTHON_VERSION[0]}.{MIN_PYTHON_VERSION[1]} 及以上，"
                f"当前为 Python {sys.version_info.major}.{sys.version_info.minor}。"
            )
        )
        print("  请使用符合要求的 Python 解释器重新运行 setup.py。")
        return 1

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
            stata_edition = _edition_with_warning(stata_home)
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
    if not generate_mcp_json(project_root, python_exe, stata_home, stata_edition):
        return 1
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
    print("如需修改 Stata 路径，编辑此文件的 STATA_HOME 环境变量即可")
    print()
    print("环境变量说明：")
    print("  STATA_HOME           Stata 安装目录（默认自动检测）")
    print("  STATA_EDITION        Stata 版本 mp/se/be（默认 mp）")
    print("  STATA_ALLOWED_ROOTS  路径沙箱白名单，分号分隔（可选，不设则无沙箱限制）")
    print("  STATA_ALLOW_UNC      设为 1 允许 UNC 网络路径（可选，默认禁止）")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
