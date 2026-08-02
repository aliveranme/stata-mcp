"""graph-export 批次 2：E3 资源回传 + E4 etable 导出。"""
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
        if crash: return {'label': label, 'result': 'CRASH', 'detail': text[:400], 'secs': dt}
        if is_err: return {'label': label, 'result': 'ERROR', 'detail': text[:400], 'secs': dt}
        return {'label': label, 'result': 'PASS', 'detail': text[:400], 'secs': dt}
    except Exception as e:
        return {'label': label, 'result': 'EXCEPTION', 'detail': f'{type(e).__name__}: {e}'[:400], 'secs': round(time.time()-t0,2)}

results = []
server.stata_use_example('auto')
server.stata_run('clear all')
server.stata_use_example('auto')

# ============ E3 资源回传 ============
png = os.path.join(OUT, 'e3_scatter.png')
if os.path.exists(png): os.remove(png)
res = run('E3 graph export png', lambda: server.stata_graph(command='twoway scatter price weight', export=png, replace=True))
results.append(res)

res = run('E3 read_file info on png', lambda: server.stata_read_file(png, action='info'))
results.append(res)

res = run('E3 read_file read (base64) on png', lambda: server.stata_read_file(png, action='read'))
results.append(res)

res = run('E3 list_resources', lambda: server.stata_list_resources())
results.append(res)

# 未登记文件读取 —— 应优雅报错
unreg = os.path.join(OUT, 'e3_never_written.png')
res = run('E3 read unregistered file (error expected)', lambda: server.stata_read_file(unreg, action='info'))
results.append(res)

# save_dataset + 读回 dta
dta = os.path.join(OUT, 'e3_auto_save.dta')
if os.path.exists(dta): os.remove(dta)
res = run('E3 save_dataset', lambda: server.stata_save_dataset(dta, replace=True))
results.append(res)

res = run('E3 read dta info', lambda: server.stata_read_file(dta, action='info'))
results.append(res)

# 读回 dta 数据完整性：clear 后用 use 读回，对比 obs/var
res = run('E3 read back dta in Stata', lambda: server.stata_run(f'clear all\nuse "{dta}"\ndescribe'))
results.append(res)

# ============ E4 etable ============
server.stata_run('clear all')
server.stata_use_example('auto')
res = run('E4 regress (prep)', lambda: server.stata_regress('price', 'weight mpg foreign'))
results.append(res)

for ext in ['docx', 'xlsx', 'pdf', 'tex']:
    p = os.path.join(OUT, f'e4_etable.{ext}')
    if os.path.exists(p): os.remove(p)
    def go(ext=ext, p=p):
        return server.stata_etable(export=p, replace=True)
    res = run(f'E4 etable export {ext}', go)
    res['file_size'] = os.path.getsize(p) if os.path.exists(p) else None
    results.append(res)

# etable 不支持 csv —— 应入口拦下
res = run('E4 etable export csv (blocked expected)', lambda: server.stata_etable(export=os.path.join(OUT, 'e4_etable.csv'), replace=True))
results.append(res)

# etable 导出到不存在目录 —— 应优雅报错
res = run('E4 etable export to missing dir', lambda: server.stata_etable(export=os.path.join(OUT, 'no_such_dir', 'e4.xlsx'), replace=True))
results.append(res)

with open(os.path.join(OUT, 'batch2_results.json'), 'w') as f:
    json.dump(results, f, ensure_ascii=False, indent=1)

with open(os.path.join(OUT, 'batch2_summary.txt'), 'w') as f:
    for r in results:
        f.write(f"{r['label']:<36} {r['result']:<9} {r.get('secs', 0):>6}s  size={r.get('file_size')}\n")
        f.write(f"    {r.get('detail', '')[:250]}\n")
print("BATCH2 DONE")
