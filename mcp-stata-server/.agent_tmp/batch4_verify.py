"""graph-export 批次 4：E5 sheet 校验 + E6 分页/读取边界 + 细节确认。"""
import sys, os, json
os.environ['STATA_HOME'] = '/Volumes/ccc/Applications/StataNow'
sys.path.insert(0, os.getcwd())
import server

OUT = '/Volumes/ccc/Projects/stata-mcp/mcp-stata-server/.agent_tmp/ge_out'

def txt(r):
    return r.content[0].text if hasattr(r, 'content') else str(r)

def run(label, fn):
    import time
    t0 = time.time()
    try:
        r = fn()
        text = txt(r)
        is_err = bool(getattr(r, 'is_error', False))
        crash = any(m in text for m in ('StataSO_Execute 崩溃', 'DLL 无响应', '已自动恢复'))
        if crash: return {'label': label, 'result': 'CRASH', 'detail': text[:400], 'secs': round(time.time()-t0,2)}
        if is_err: return {'label': label, 'result': 'ERROR', 'detail': text[:400], 'secs': round(time.time()-t0,2)}
        return {'label': label, 'result': 'PASS', 'detail': text[:400], 'secs': round(time.time()-t0,2)}
    except Exception as e:
        return {'label': label, 'result': 'EXCEPTION', 'detail': f'{type(e).__name__}: {e}'[:400], 'secs': round(time.time()-t0,2)}

results = []
server.stata_use_example('auto')
server.stata_run('clear all')
server.stata_use_example('auto')

# E5: sheet_mode modify 后的第二张表读回
xm = os.path.join(OUT, 'e5_sheetmode.xlsx')
res = run('E5 read back sheet Data2 via Stata', lambda: server.stata_run(f'clear\nimport excel using "{xm}", firstrow sheet("Data2")\ndescribe'))
results.append(res)

# E5: sheet_mode replace 覆盖一张表
xm2 = os.path.join(OUT, 'e5_sheetmode2.xlsx')
if os.path.exists(xm2): os.remove(xm2)
res = run('E5 export excel sheetmode replace', lambda: server.stata_export_excel(xm2, sheet='Main', replace=True))
results.append(res)
res = run('E5 sheet_mode replace overwrite same sheet', lambda: server.stata_export_excel(xm2, sheet='Main', sheet_mode='replace'))
results.append(res)
res = run('E5 verify Main sheet obs after sheet replace', lambda: server.stata_run(f'clear\nimport excel using "{xm2}", firstrow sheet("Main")\ndescribe'))
results.append(res)

# E6: 大数据集 + stata_more 翻页（同一进程内）
server.stata_run('clear')
server.stata_run('set obs 8000')
server.stata_run('gen id = _n')
server.stata_run('gen x1 = rnormal()')
server.stata_run('gen x2 = rnormal()')
res = run('E6 list 8000 page1', lambda: server.stata_run('list id x1 x2, nolabel'))
results.append(res)
res = run('E6 stata_more page2', lambda: server.stata_more(page=2))
results.append(res)
res = run('E6 stata_more page0 all', lambda: server.stata_more(page=0))
results.append(res)

# 大文件 read_file(action=read) —— 应被 80KB 工具上限拦下，提示走 resources/read
big_list = os.path.join(OUT, 'e6_big_list.txt')
if os.path.exists(big_list): os.remove(big_list)
res = run('E6 list save_output again', lambda: server.stata_run('list id x1 x2, nolabel', save_output=big_list, timeout=120))
results.append(res)
res = run('E6 read_file read on 590KB file (blocked expected)', lambda: server.stata_read_file(big_list, action='read'))
results.append(res)

# 小文件 read_file read —— 正常 base64
png = os.path.join(OUT, 'e3_scatter.png')
res = run('E6 read_file read on 23KB png (base64 ok)', lambda: server.stata_read_file(png, action='read'))
results.append(res)

with open(os.path.join(OUT, 'batch4_results.json'), 'w') as f:
    json.dump(results, f, ensure_ascii=False, indent=1)
with open(os.path.join(OUT, 'batch4_summary.txt'), 'w') as f:
    for r in results:
        f.write(f"{r['label']:<44} {r['result']:<9} {r.get('secs', 0):>6}s\n")
        f.write(f"    {r.get('detail', '')[:250]}\n")
print("BATCH4 DONE")
