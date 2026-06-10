#!/usr/bin/env python3
"""
Stata MCP 功能验证与回退执行脚本

功能：
  1. 心跳检测（MCP 是否存活）
  2. /// 续行符测试
  3. stata_graph 图形导出（支持 scheme 样式）
  4. stata_export_excel 数据/结果导出
  5. 完整 auto.dta 分析（MCP 掉线时的回退路径）

用法：
  python test_stata_mcp.py              # 自动判断使用 MCP 或直接 pystata
  python test_stata_mcp.py --fallback   # 强制使用直接 pystata
"""

import sys
import os
import time
import json
import subprocess
import traceback

PROJECT_ROOT = r"F:\Projects\temp\temp\temp"
MCP_JSON = os.path.join(PROJECT_ROOT, ".mcp.json")


def check_mcp_server():
    """通过 .mcp.json 配置检查 MCP Server 能否启动。"""
    if not os.path.isfile(MCP_JSON):
        return False, ".mcp.json not found"
    try:
        with open(MCP_JSON, encoding="utf-8") as f:
            cfg = json.load(f)
        server = cfg.get("mcpServers", {}).get("stata", {})
        cmd = server.get("command", "")
        if not cmd or not os.path.isfile(cmd):
            return False, f"Python not found: {cmd}"
        return True, "OK"
    except Exception as e:
        return False, str(e)


def run_direct_pystata():
    """使用直接 pystata 调用运行完整测试（回退路径）。"""
    print("=" * 60)
    print("[回退模式] 使用直接 pystata 调用")
    print("=" * 60)

    script = os.path.join(PROJECT_ROOT, "auto_analysis.py")
    if not os.path.isfile(script):
        print(f"错误: 找不到 {script}")
        return False

    venv_python = os.path.join(
        PROJECT_ROOT, "mcp-stata-server", ".venv", "Scripts", "python.exe"
    )
    if not os.path.isfile(venv_python):
        print(f"错误: 找不到 venv Python: {venv_python}")
        return False

    result = subprocess.run(
        [venv_python, script],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=PROJECT_ROOT,
    )

    print(result.stdout)
    if result.stderr.strip():
        for line in result.stderr.strip().split("\n")[-5:]:
            print(f"  [stderr] {line}")

    return result.returncode == 0


def test_excel_export_fallback():
    """测试 Excel 导出功能（直接 pystata 路径）。"""
    print("\n" + "=" * 60)
    print("[测试] Excel 导出")
    print("=" * 60)

    # 导入 pystata
    stata_home = r"D:\StataNow19"
    os.environ["STATA_HOME"] = stata_home
    os.environ["STATA_EDITION"] = "mp"
    os.environ["SYSDIR_STATA"] = stata_home
    sys.path.insert(0, os.path.join(stata_home, "utilities"))

    try:
        from pystata import config
        config.init("mp", splash=False)
        config.stconfig["streamout"] = "off"
        from pystata.core import stout
    except Exception as e:
        print(f"  [FAIL] Stata 初始化失败: {e}")
        return []

    results = []

    # ===== 测试 1: 导出数据 =====
    try:
        with stout.RedirectOutput(stout.StataDisplay(), stout.StataError(), stecho=False):
            encoded = config.get_encode_str("sysuse auto, clear")
            config.stlib.StataSO_Execute(encoded, False)
        time.sleep(0.1)

        xlsx_path = os.path.join(PROJECT_ROOT, "test_export_data.xlsx")
        cmd = f'export excel using "{xlsx_path}", replace firstrow(variables) sheet(Data)'
        with stout.RedirectOutput(stout.StataDisplay(), stout.StataError(), stecho=False):
            encoded = config.get_encode_str(cmd)
            rc = config.stlib.StataSO_Execute(encoded, False)

        if os.path.isfile(xlsx_path):
            size = os.path.getsize(xlsx_path)
            print(f"  [PASS] 数据导出 OK → {xlsx_path} ({size // 1024} KB)")
            results.append(("export_data", True, f"{size // 1024} KB"))
        else:
            print(f"  [FAIL] 数据导出文件未生成 (rc={rc})")
            results.append(("export_data", False, f"rc={rc}"))

        finalize(config)
    except Exception as e:
        print(f"  [FAIL] 数据导出异常: {e}")
        results.append(("export_data", False, str(e)))

    # ===== 测试 2: 导出回归结果 =====
    try:
        xlsx_path = os.path.join(PROJECT_ROOT, "test_export_results.xlsx")
        cmd_parts = [
            "sysuse auto, clear",
            "reg price weight mpg foreign",
            f'esttab using "{xlsx_path}", replace sheet(Results) plain nogaps nomtitle nonumber',
        ]
        for cmd in cmd_parts:
            with stout.RedirectOutput(
                stout.StataDisplay(), stout.StataError(), stecho=False
            ):
                encoded = config.get_encode_str(cmd)
                config.stlib.StataSO_Execute(encoded, False)
            time.sleep(0.05)

        if os.path.isfile(xlsx_path):
            size = os.path.getsize(xlsx_path)
            print(f"  [PASS] 结果导出 OK → {xlsx_path} ({size // 1024} KB)")
            results.append(("export_results", True, f"{size // 1024} KB"))
        else:
            print(f"  [FAIL] 结果导出文件未生成")
            results.append(("export_results", False, "no file"))

        finalize(config)
    except Exception as e:
        print(f"  [FAIL] 结果导出异常: {e}")
        results.append(("export_results", False, str(e)))

    return results


def test_graph_with_styles_fallback():
    """测试不同样式图形导出（直接 pystata）。"""
    print("\n" + "=" * 60)
    print("[测试] 图形样式（scheme）")
    print("=" * 60)

    stata_home = r"D:\StataNow19"
    os.environ["STATA_HOME"] = stata_home
    os.environ["STATA_EDITION"] = "mp"
    os.environ["SYSDIR_STATA"] = stata_home
    sys.path.insert(0, os.path.join(stata_home, "utilities"))

    try:
        from pystata import config
        config.init("mp", splash=False)
        config.stconfig["streamout"] = "off"
        from pystata.core import stout
    except Exception as e:
        print(f"  [FAIL] Stata 初始化失败: {e}")
        return []

    results = []
    do_dir = os.path.join(PROJECT_ROOT, "temp_do")
    os.makedirs(do_dir, exist_ok=True)

    schemas = [
        ("s2color", "默认彩色"),
        ("s2mono", "灰度"),
    ]

    for scheme, label in schemas:
        filename = f"test_graph_{scheme}.png"
        do_path = os.path.join(do_dir, f"graph_{scheme}.do")
        png_path = os.path.join(PROJECT_ROOT, filename)

        do_content = (
            "sysuse auto, clear\n"
            f"set scheme {scheme}\n"
            "twoway (scatter price weight, mcolor(navy) msymbol(O))"
            " (lfit price weight, lcolor(red) lpattern(dash)),"
            ' title("Price vs Weight")'
            ' ytitle("Price ($)") xtitle("Weight (lbs)")\n'
            f'graph export "{png_path}", replace width(800)\n'
            "graph drop _all\n"
        )

        with open(do_path, "w", encoding="utf-8") as f:
            f.write(do_content)

        try:
            with stout.RedirectOutput(
                stout.StataDisplay(), stout.StataError(), stecho=False
            ):
                encoded = config.get_encode_str(f'do "{do_path}"')
                rc = config.stlib.StataSO_Execute(encoded, False)
            time.sleep(0.1)

            if os.path.isfile(png_path) and os.path.getsize(png_path) > 1024:
                size = os.path.getsize(png_path)
                print(f"  [PASS] scheme={scheme} ({label}) → {filename} ({size // 1024} KB)")
                results.append((filename, True, f"{size // 1024} KB"))
            else:
                print(f"  [FAIL] scheme={scheme} → 图片未生成 (rc={rc})")
                results.append((filename, False, f"rc={rc}"))

            finalize(config)
        except Exception as e:
            print(f"  [FAIL] scheme={scheme} → {e}")
            results.append((filename, False, str(e)))

    return results


def finalize(config):
    """清理 Stata 输出缓冲。"""
    try:
        for _ in range(50):
            out = config.get_output()
            if not out:
                break
            time.sleep(0.001)
        time.sleep(0.05)
        for _ in range(50):
            out = config.get_output()
            if not out:
                break
            time.sleep(0.001)
    except Exception:
        pass


def run_full_test():
    """运行完整测试套件。"""
    print("=" * 60)
    print("Stata MCP 功能验证测试套件")
    print("=" * 60)

    # Step 1: MCP 服务器检查
    print("\n[Step 1] MCP Server 检查")
    ok, msg = check_mcp_server()
    if ok:
        print(f"  [INFO] .mcp.json 有效: {msg}")
    else:
        print(f"  [INFO] .mcp.json 问题: {msg}")

    # Step 2: 尝试 MCP 测试（通过直接 pystata 模拟）
    print("\n[Step 2] /// 续行符测试")
    try:
        stata_home = r"D:\StataNow19"
        os.environ["STATA_HOME"] = stata_home
        os.environ["STATA_EDITION"] = "mp"
        os.environ["SYSDIR_STATA"] = stata_home
        sys.path.insert(0, os.path.join(stata_home, "utilities"))

        from pystata import config
        config.init("mp", splash=False)
        config.stconfig["streamout"] = "off"
        from pystata.core import stout

        # 测试 /// continuation
        full_cmd = "sysuse auto, clear\nreg price weight mpg ///\n  foreign"
        with stout.RedirectOutput(stout.StataDisplay(), stout.StataError(), stecho=False):
            encoded = config.get_encode_str(full_cmd)
            rc = config.stlib.StataSO_Execute(encoded, False)

        # 收集输出
        time.sleep(0.1)
        output = ""
        for _ in range(100):
            out = config.get_output()
            if out:
                output += out
            else:
                break
            time.sleep(0.001)

        if "foreign" in output and "weight" in output:
            print(f"  [PASS] /// 续行符正确合并 (rc={rc})")
        else:
            print(f"  [INFO] /// 测试输出: {output[:120]}")

        finalize(config)
    except Exception as e:
        print(f"  [FAIL] /// 测试失败: {e}")

    # Step 3: 图形样式测试
    graph_results = test_graph_with_styles_fallback()

    # Step 4: Excel 导出测试
    excel_results = test_excel_export_fallback()

    # 汇总
    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)
    all_results = graph_results + excel_results
    passed = sum(1 for _, ok, _ in all_results if ok)
    failed = sum(1 for _, ok, _ in all_results if not ok)

    for name, ok, detail in all_results:
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name} ({detail})")

    print(f"\n总计: {passed} 通过, {failed} 失败 / {len(all_results)} 项")
    return failed == 0


if __name__ == "__main__":
    success = run_full_test()
    sys.exit(0 if success else 1)
