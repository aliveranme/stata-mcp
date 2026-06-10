#!/usr/bin/env python3
"""
auto.dta 深度分析脚本
直接通过 pystata 调用 Stata DLL，绕过 MCP Server 的断开问题。
包含：超时回退、错误捕捉、完善图表导出。
"""
import sys
import os
import time
import threading
import traceback

# =============================================================================
# 配置
# =============================================================================
STATA_HOME = r"D:/StataNow19"
STATA_EDITION = "mp"
OUTPUT_DIR = r"F:/Projects/temp/temp/temp"
TIMEOUT_SEC = 90  # 单条命令超时

os.environ["STATA_HOME"] = STATA_HOME
os.environ["STATA_EDITION"] = STATA_EDITION
os.environ["SYSDIR_STATA"] = STATA_HOME

sys.path.insert(0, os.path.join(STATA_HOME, "utilities"))

try:
    from pystata import config
    config.init(STATA_EDITION, splash=False)
    config.stconfig['streamout'] = 'off'
    from pystata.core import stout
    print(f"[OK] Stata {config.stversion} {config.stedition} 初始化成功")
except Exception as e:
    print(f"[FATAL] Stata 初始化失败: {e}")
    sys.exit(1)


# =============================================================================
# 安全执行函数（带超时、错误捕捉）
# =============================================================================

def _drain_output():
    """排空残留输出缓冲"""
    parts = []
    t0 = time.time()
    last_val = time.time()
    while time.time() - t0 < 0.2:
        out = config.get_output()
        if out:
            parts.append(out)
            last_val = time.time()
        if time.time() - last_val > 0.03:
            break
        time.sleep(0.005)
    return "".join(parts)


def run_stata(cmd: str, timeout: int = TIMEOUT_SEC):
    """安全执行 Stata 命令，带超时和错误捕捉。"""
    _drain_output()

    exec_done = threading.Event()
    did_break = False
    result = {"rc": 0, "output": "", "error": None}

    def watchdog():
        nonlocal did_break
        if not exec_done.wait(timeout=timeout):
            print(f"  [TIMEOUT] 命令超时 ({timeout}s)，正在中断...")
            try:
                sb = config.stlib.StataSO_SetBreak
                if sb:
                    sb()
            except:
                pass
            did_break = True

    watch = threading.Thread(target=watchdog, daemon=True)
    watch.start()

    try:
        with stout.RedirectOutput(stout.StataDisplay(), stout.StataError(), stecho=False):
            encoded = config.get_encode_str(cmd)
            rc = config.stlib.StataSO_Execute(encoded, False)
        result["rc"] = rc
    except Exception as e:
        result["rc"] = 999
        result["error"] = str(e)
        print(f"  [CRASH] StataSO_Execute 崩溃: {e}")
    finally:
        exec_done.set()

    if did_break:
        time.sleep(0.1)
        _drain_output()

    # 收集输出
    parts = []
    for _ in range(300):
        out = config.get_output()
        if out:
            parts.append(out)
        else:
            break
        time.sleep(0.001)
    tail = _drain_output()
    if tail:
        parts.append(tail)

    output = "".join(parts)
    if rc != 0 and rc != 3000:
        result["output"] = f"[返回码: {rc}] {cmd[:60]}\n{output.strip()}"
    else:
        result["output"] = output.strip()

    return result


def exec_commands(commands: list, label: str = ""):
    """执行一系列命令并报告状态。"""
    print(f"\n{'='*60}")
    print(f"[{label}] 开始执行 {len(commands)} 条命令")
    print(f"{'='*60}")

    for i, cmd in enumerate(commands):
        print(f"  [{i+1}/{len(commands)}] {cmd[:100]}...", end=" ")
        sys.stdout.flush()
        try:
            res = run_stata(cmd)
            if res["rc"] == 0 or res["rc"] == 3000:
                print("OK")
            else:
                print(f"RC={res['rc']}")
                if res["output"]:
                    for line in res["output"].split("\n")[:3]:
                        print(f"    {line}")
        except Exception as e:
            print(f"ERROR: {e}")
            traceback.print_exc()

    print(f"[{label}] 完成")


# =============================================================================
# 1. 数据加载与准备
# =============================================================================

exec_commands([
    "sysuse auto, clear",
    'generate price_ln = ln(price)',
    'generate weight_ln = ln(weight)',
    'generate weight2 = weight^2',
    'generate mpg2 = mpg^2',
    'generate weight_mpg = weight * mpg',
    'label variable price_ln "ln(Price)"',
    'label variable weight_ln "ln(Weight)"',
    'label variable weight2 "Weight squared"',
    'label variable mpg2 "MPG squared"',
    'label variable weight_mpg "Weight x MPG"',
    "describe",
    "summarize price mpg weight rep78 headroom trunk turn displacement gear_ratio foreign",
], label="数据加载与准备")


# =============================================================================
# 2. 多模型对比
# =============================================================================

exec_commands([
    "reg price weight mpg foreign",
    "estimates store m1",
    "reg price weight_ln mpg foreign",
    "estimates store m2",
    "reg price_ln weight_ln mpg foreign",
    "estimates store m3",
    "reg price weight weight2 mpg foreign",
    "estimates store m4",
    "reg price weight mpg foreign weight_mpg",
    "estimates store m5",
    "estimates table m1 m2 m3 m4 m5, stats(N r2 r2_a bic) star(0.1 0.05 0.01)",
], label="多模型对比")


# =============================================================================
# 3. 分组回归（国产 vs 进口）
# =============================================================================

exec_commands([
    "reg price weight mpg if foreign==0",
    "estimates store domestic",
    "reg price weight mpg if foreign==1",
    "estimates store foreign_",
    "estimates table domestic foreign_, stats(N r2 r2_a) star(0.1 0.05 0.01)",
    "reg price weight mpg foreign c.weight#c.foreign c.mpg#c.foreign",
    "testparm c.weight#c.foreign c.mpg#c.foreign",
], label="分组回归（Chow 检验）")


# =============================================================================
# 4. 异方差检验
# =============================================================================

exec_commands([
    "reg price weight mpg foreign",
    "estat hettest",
    "predict res, resid",
    "predict fitted, xb",
    "generate res2 = res^2",
    "reg res2 weight mpg foreign",
    "display `\"BP R2: \"' _result(7)",
    "display `\"LM stat: \"' _result(7) * e(N)",
    "estat imtest, white",
], label="异方差诊断")


# =============================================================================
# 5. 稳健标准误模型
# =============================================================================

exec_commands([
    "reg price weight mpg foreign, robust",
    "estimates store m1_robust",
    "estimates table m1 m1_robust, stats(N r2 r2_a)",
], label="稳健标准误")


# =============================================================================
# 6. 图形生成（核心改进：通过 .do 文件在单次 StataSO_Execute 中完成）
# =============================================================================
# 修复：StataSO_Execute 不支持 \n 作为命令分隔符。
# 改为写入 .do 文件后用 do 命令执行，确保 graph + export 在同一次调用中。
# 脚本使用 set scheme s2color 确保一致的外观风格。

print(f"\n{'='*60}")
print("[图形生成] 开始")
print(f"{'='*60}")

GRAPH_DIR = OUTPUT_DIR
DO_DIR = os.path.join(GRAPH_DIR, "temp_do")
os.makedirs(DO_DIR, exist_ok=True)

# 每个图形的 (文件名, do_file_content)
# do 文件：图形命令 + graph export 一起写入，批量执行
graph_specs = [
    {
        "file": "price_weight_scatter.png",
        "do": (
            'set scheme s2color\n'
            'twoway (scatter price weight, mcolor(navy) msymbol(O))'
            ' (lfit price weight, lcolor(red) lpattern(dash)),'
            ' title("Price vs Weight")'
            ' ytitle("Price ($)") xtitle("Weight (lbs)")'
            ' legend(order(1 "Observed" 2 "Linear fit"))\n'
            f'graph export "{GRAPH_DIR}/price_weight_scatter.png", replace width(800)\n'
            'graph drop _all\n'
        ),
    },
    {
        "file": "price_mpg_scatter.png",
        "do": (
            'set scheme s2color\n'
            'twoway (scatter price mpg, mcolor(dkgreen) msymbol(O))'
            ' (lfit price mpg, lcolor(orange) lpattern(dash)),'
            ' title("Price vs MPG")'
            ' ytitle("Price ($)") xtitle("Mileage (mpg)")'
            ' legend(order(1 "Observed" 2 "Linear fit"))\n'
            f'graph export "{GRAPH_DIR}/price_mpg_scatter.png", replace width(800)\n'
            'graph drop _all\n'
        ),
    },
    {
        "file": "price_histogram.png",
        "do": (
            'set scheme s2color\n'
            'histogram price, frequency color(navy%40) lcolor(navy) lwidth(thin)'
            ' title("Price Distribution") ytitle(Frequency) xtitle("Price ($)")\n'
            f'graph export "{GRAPH_DIR}/price_histogram.png", replace width(800)\n'
            'graph drop _all\n'
        ),
    },
    {
        "file": "weight_histogram.png",
        "do": (
            'set scheme s2color\n'
            'histogram weight, frequency color(orange%40) lcolor(orange) lwidth(thin)'
            ' title("Weight Distribution") ytitle(Frequency) xtitle("Weight (lbs)")\n'
            f'graph export "{GRAPH_DIR}/weight_histogram.png", replace width(800)\n'
            'graph drop _all\n'
        ),
    },
    {
        "file": "price_weight_by_origin.png",
        "do": (
            'set scheme s2color\n'
            'twoway (scatter price weight if foreign==0, mcolor(navy) msymbol(O))'
            ' (lfit price weight if foreign==0, lcolor(red))'
            ' (scatter price weight if foreign==1, mcolor(orange) msymbol(D))'
            ' (lfit price weight if foreign==1, lcolor(green) lpattern(dash)),'
            ' title("Price vs Weight by Origin")'
            ' ytitle("Price ($)") xtitle("Weight (lbs)")'
            ' legend(order(1 "Domestic" 2 "Domestic fit" 3 "Foreign" 4 "Foreign fit"))\n'
            f'graph export "{GRAPH_DIR}/price_weight_by_origin.png", replace width(800)\n'
            'graph drop _all\n'
        ),
    },
    {
        "file": "price_mpg_by_origin.png",
        "do": (
            'set scheme s2color\n'
            'twoway (scatter price mpg if foreign==0, mcolor(navy) msymbol(O))'
            ' (lfit price mpg if foreign==0, lcolor(red))'
            ' (scatter price mpg if foreign==1, mcolor(orange) msymbol(D))'
            ' (lfit price mpg if foreign==1, lcolor(green) lpattern(dash)),'
            ' title("Price vs MPG by Origin")'
            ' ytitle("Price ($)") xtitle("Mileage (mpg)")'
            ' legend(order(1 "Domestic" 2 "Domestic fit" 3 "Foreign" 4 "Foreign fit"))\n'
            f'graph export "{GRAPH_DIR}/price_mpg_by_origin.png", replace width(800)\n'
            'graph drop _all\n'
        ),
    },
    {
        "file": "residual_plot.png",
        "do": (
            'set scheme s2color\n'
            'twoway (scatter res fitted, mcolor(red) msymbol(O))'
            ' (lfit res fitted, lcolor(black) lpattern(dot)),'
            ' title("Residual vs Fitted")'
            ' ytitle(Residuals) xtitle("Fitted values")'
            ' legend(order(1 "Residual" 2 "Linear fit"))\n'
            f'graph export "{GRAPH_DIR}/residual_plot.png", replace width(800)\n'
            'graph drop _all\n'
        ),
    },
    {
        "file": "avplot_weight.png",
        "do": (
            'set scheme s2color\n'
            'avplot weight, mcolor(navy)'
            ' title("Added Variable Plot: Weight")\n'
            f'graph export "{GRAPH_DIR}/avplot_weight.png", replace width(800)\n'
            'graph drop _all\n'
        ),
    },
]

for spec in graph_specs:
    filename = spec["file"]
    do_content = spec["do"]
    do_path = os.path.join(DO_DIR, filename.replace(".png", ".do"))

    print(f"  生成 {filename}...", end=" ")
    sys.stdout.flush()

    try:
        # 写 .do 文件
        with open(do_path, "w", encoding="utf-8") as f:
            f.write(do_content)

        # 单次 StataSO_Execute 执行 .do 文件
        do_cmd = f'do "{do_path}"'
        res = run_stata(do_cmd)

        if res["rc"] == 0 or res["rc"] == 3000:
            fpath = os.path.join(GRAPH_DIR, filename)
            if os.path.isfile(fpath) and os.path.getsize(fpath) > 1024:
                print(f"OK ({os.path.getsize(fpath)//1024} KB)")
            else:
                print(f"文件异常或不存在 ({res['output'][:80]})")
        else:
            print(f"失败: {res['output'][:120]}")
    except Exception as e:
        print(f"CRASH: {e}")
        traceback.print_exc()


# =============================================================================
# 7. 模型诊断统计量汇总
# =============================================================================

exec_commands([
    "reg price weight mpg foreign",
    "estat vif",
    "predict cooksd, cooksd",
    "display `\"Max Cooks D: \"'",
    "summarize cooksd, detail",
    "display `\"ALL DONE\"'",
], label="模型诊断汇总")


# =============================================================================
# 8. Excel 导出（数据 + 回归结果）
# =============================================================================

print(f"\n{'='*60}")
print("[Excel 导出]")
print(f"{'='*60}")

# 导出数据
xlsx_data = os.path.join(OUTPUT_DIR, "auto_data.xlsx")
res = run_stata(
    f'export excel using "{xlsx_data}", replace firstrow(variables)'
)
if os.path.isfile(xlsx_data):
    print(f"  数据导出 → auto_data.xlsx  ({os.path.getsize(xlsx_data)//1024} KB)")
else:
    print(f"  数据导出失败: {res['output'][:80]}")

# 导出回归结果（预测值 + 残差）
exec_commands([
    "reg price weight mpg foreign",
    "predict fitted, xb",
    "predict residual, resid",
    f'export excel price weight mpg foreign fitted residual using "{OUTPUT_DIR}/auto_results.xlsx", replace firstrow(variables)',
], label="回归结果导出")

print(f"\n{'='*60}")
print("[完成] 所有分析、图形和表格已生成")
print(f"图形文件保存在: {OUTPUT_DIR}")
print(f"{'='*60}")
print()
print("生成的文件列表:")
for f in sorted(os.listdir(OUTPUT_DIR)):
    if f.endswith(".png") or f.endswith(".xlsx"):
        size = os.path.getsize(os.path.join(OUTPUT_DIR, f))
        print(f"  {f}  ({size//1024} KB)")
