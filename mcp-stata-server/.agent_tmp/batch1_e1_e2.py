"""graph-export 批次 1：E1 图形导出格式矩阵 + E2 尺寸选项。
写结果到文件（Stata RedirectOutput 截获 stdout）。"""
import sys, os, time, json
os.environ['STATA_HOME'] = '/Volumes/ccc/Applications/StataNow'
sys.path.insert(0, os.getcwd())
import server

OUT = '/Volumes/ccc/Projects/stata-mcp/mcp-stata-server/.agent_tmp/ge_out'
os.makedirs(OUT, exist_ok=True)

def txt(r):
    return r.content[0].text if hasattr(r, 'content') else str(r)

def run(label, fn):
    t0 = time.time()
    try:
        r = fn()
        dt = round(time.time() - t0, 2)
        text = txt(r)
        is_err = bool(getattr(r, 'is_error', False))
        crash = any(m in text for m in ('StataSO_Execute 崩溃', 'DLL 无响应', '已自动恢复'))
        if crash: return {'label': label, 'result': 'CRASH', 'detail': text[:300], 'secs': dt}
        if is_err: return {'label': label, 'result': 'ERROR', 'detail': text[:300], 'secs': dt}
        return {'label': label, 'result': 'PASS', 'detail': text[:300], 'secs': dt}
    except Exception as e:
        return {'label': label, 'result': 'EXCEPTION', 'detail': f'{type(e).__name__}: {e}'[:300], 'secs': round(time.time()-t0,2)}

def prep(ds='auto'):
    server.stata_use_example(ds)
    server.stata_run('clear all')
    server.stata_use_example(ds)

def file_info(p):
    if not os.path.exists(p):
        return {'exists': False}
    return {'exists': True, 'size': os.path.getsize(p)}

results = []
prep('auto')

# ============ E1 格式矩阵 ============
for ext in ['png', 'jpg', 'pdf', 'svg', 'eps']:
    p = os.path.join(OUT, f'e1_scatter.{ext}')
    if os.path.exists(p): os.remove(p)
    def go(ext=ext, p=p):
        return server.stata_graph(command='twoway scatter price weight', export=p, replace=True)
    res = run(f'E1 export {ext}', go)
    fi = file_info(p)
    res['file'] = fi
    results.append(res)

# ============ E2 尺寸选项 ============
# png 像素宽度/高度 400x300 —— 应原样下传
p = os.path.join(OUT, 'e2_png_400x300.png')
if os.path.exists(p): os.remove(p)
res = run('E2 png width=400 height=300', lambda: server.stata_graph(command='twoway scatter price weight', export=p, width=400, height=300, replace=True))
res['file'] = file_info(p)
results.append(res)

# pdf 默认 width=800（英寸，>20 应丢弃并提示）
p = os.path.join(OUT, 'e2_pdf_800.pdf')
if os.path.exists(p): os.remove(p)
res = run('E2 pdf width=800 default (inches, dropped)', lambda: server.stata_graph(command='twoway scatter price weight', export=p, replace=True))
res['file'] = file_info(p)
results.append(res)

# pdf width=6 合法英寸 —— 应保留
p = os.path.join(OUT, 'e2_pdf_6.pdf')
if os.path.exists(p): os.remove(p)
res = run('E2 pdf width=6 inches (kept)', lambda: server.stata_graph(command='twoway scatter price weight', export=p, width=6, replace=True))
res['file'] = file_info(p)
results.append(res)

# svg width=800 像素 —— 应原样下传，无丢弃提示
p = os.path.join(OUT, 'e2_svg_800.svg')
if os.path.exists(p): os.remove(p)
res = run('E2 svg width=800 pixels (kept)', lambda: server.stata_graph(command='twoway scatter price weight', export=p, width=800, replace=True))
res['file'] = file_info(p)
results.append(res)

# eps width=800 —— 不支持尺寸，应丢弃并提示
p = os.path.join(OUT, 'e2_eps_800.eps')
if os.path.exists(p): os.remove(p)
res = run('E2 eps width=800 (no-size, dropped)', lambda: server.stata_graph(command='twoway scatter price weight', export=p, width=800, replace=True))
res['file'] = file_info(p)
results.append(res)

# png 但传 quality=80 —— 应丢弃并提示（quality 仅 jpg）
p = os.path.join(OUT, 'e2_png_quality.png')
if os.path.exists(p): os.remove(p)
res = run('E2 png quality=80 (dropped)', lambda: server.stata_graph(command='twoway scatter price weight', export=p, quality=80, replace=True))
res['file'] = file_info(p)
results.append(res)

# jpg 传 quality=80 —— 应保留
p = os.path.join(OUT, 'e2_jpg_q80.jpg')
if os.path.exists(p): os.remove(p)
res = run('E2 jpg quality=80 (kept)', lambda: server.stata_graph(command='twoway scatter price weight', export=p, quality=80, replace=True))
res['file'] = file_info(p)
results.append(res)

# 负宽度 —— 入口应拒绝
res = run('E2 png width=-100 (rejected)', lambda: server.stata_graph(command='twoway scatter price weight', export=os.path.join(OUT, 'e2_neg.png'), width=-100, replace=True))
results.append(res)

with open(os.path.join(OUT, 'batch1_results.json'), 'w') as f:
    json.dump(results, f, ensure_ascii=False, indent=1)

# 汇总打印到文件
with open(os.path.join(OUT, 'batch1_summary.txt'), 'w') as f:
    for r in results:
        f.write(f"{r['label']:<28} {r['result']:<9} {r.get('secs', 0):>6}s  file={r.get('file')}\n")
        f.write(f"    {r.get('detail', '')[:200]}\n")
print("BATCH1 DONE")
