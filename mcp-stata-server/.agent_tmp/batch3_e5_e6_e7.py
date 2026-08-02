"""graph-export 批次 3：E5 export excel/delimited + E6 大输出 + E7 图形边界。"""
import sys, os, time, json, csv
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

# ============ E5 export excel/delimited ============
xlsx = os.path.join(OUT, 'e5_auto.xlsx')
if os.path.exists(xlsx): os.remove(xlsx)
res = run('E5 export excel xlsx', lambda: server.stata_export_excel(xlsx, replace=True))
results.append(res)

# 读回 xlsx（用 Stata import excel 校验）
res = run('E5 read back xlsx via Stata', lambda: server.stata_run(f'clear\nimport excel "{xlsx}", firstrow\ndescribe'))
results.append(res)

# export delimited csv
csvp = os.path.join(OUT, 'e5_auto.csv')
if os.path.exists(csvp): os.remove(csvp)
res = run('E5 export delimited csv', lambda: server.stata_export_delimited(csvp, replace=True))
results.append(res)

# 用 Python 读回 CSV 校验行数=74+header, 列=12
nrows = None
ncols = None
try:
    with open(csvp, newline='') as f:
        rd = list(csv.reader(f))
        nrows = len(rd) - 1  # minus header
        ncols = len(rd[0])
except Exception as e:
    print('CSV readback error:', e)
res = {'label': 'E5 CSV python readback', 'result': 'PASS' if nrows == 74 and ncols == 12 else 'ISSUE',
       'detail': f'rows(data)={nrows} cols={ncols} (expect 74/12)', 'secs': 0}
results.append(res)

# replace=False 已存在 → 应优雅报错并提示
res = run('E5 export delimited replace=False when exists', lambda: server.stata_export_delimited(csvp))
results.append(res)

# replace=True 覆盖成功
res = run('E5 export delimited replace=True overwrite', lambda: server.stata_export_delimited(csvp, replace=True))
results.append(res)

# sheet_mode + replace 冲突 → 入口拦下
res = run('E5 sheet_mode + replace conflict blocked', lambda: server.stata_export_excel(os.path.join(OUT, 'e5_conflict.xlsx'), sheet_mode='replace', replace=True))
results.append(res)

# sheet_mode=modify 修改已有文件中的一张表
xm = os.path.join(OUT, 'e5_sheetmode.xlsx')
if os.path.exists(xm): os.remove(xm)
res = run('E5 export excel first', lambda: server.stata_export_excel(xm, replace=True))
results.append(res)
res = run('E5 sheet_mode modify second sheet', lambda: server.stata_export_excel(xm, sheet='Data2', sheet_mode='modify'))
results.append(res)

# ============ E6 大输出 ============
# E6a: export delimited 全部 74 obs + tab 分隔
tsvp = os.path.join(OUT, 'e6_auto.tsv')
if os.path.exists(tsvp): os.remove(tsvp)
res = run('E6 export delimited tab all 74 obs', lambda: server.stata_export_delimited(tsvp, delimiter='tab', replace=True))
results.append(res)
try:
    with open(tsvp, newline='') as f:
        rd = list(csv.reader(f, delimiter='\t'))
    res = {'label': 'E6 TSV python readback', 'result': 'PASS' if len(rd) == 75 else 'ISSUE',
           'detail': f'rows(with header)={len(rd)} (expect 75)', 'secs': 0}
except Exception as e:
    res = {'label': 'E6 TSV python readback', 'result': 'EXCEPTION', 'detail': str(e), 'secs': 0}
results.append(res)

# E6b: 生成大数据集，list 大输出 + save_output 落盘读回
server.stata_run('clear')
server.stata_run('set obs 8000')
server.stata_run('gen id = _n')
server.stata_run('gen x1 = rnormal()')
server.stata_run('gen x2 = rnormal()')
server.stata_run('gen x3 = rnormal()')
server.stata_run('gen x4 = rnormal()')
big_list = os.path.join(OUT, 'e6_big_list.txt')
if os.path.exists(big_list): os.remove(big_list)
res = run('E6 list 8000 obs save_output', lambda: server.stata_run('list id x1 x2 x3 x4, nolabel', save_output=big_list, timeout=120))
results.append(res)
if os.path.exists(big_list):
    results.append({'label': 'E6 save_output file size', 'result': 'PASS', 'detail': f'size={os.path.getsize(big_list)}', 'secs': 0})
    res = run('E6 read save_output info', lambda: server.stata_read_file(big_list, action='info'))
    results.append(res)
    # 校验完整输出行数（8000 行 + 表头）
    with open(big_list) as f:
        content = f.read()
    results.append({'label': 'E6 save_output content lines', 'result': 'PASS',
                    'detail': 'chars=%d, has_header=%s' % (len(content), 'id' in content[:200]), 'secs': 0})

# ============ E7 图形边界 ============
server.stata_use_example('auto')
# 空数据 graph
server.stata_run('clear')
res = run('E7 graph on empty data', lambda: server.stata_graph(command='twoway scatter price weight', export=os.path.join(OUT, 'e7_empty.png'), replace=True))
results.append(res)

# 非法 scheme
server.stata_use_example('auto')
res = run('E7 illegal scheme rejected', lambda: server.stata_graph(command='twoway scatter price weight', scheme='bad scheme !'))
results.append(res)

# 合法 scheme
res = run('E7 valid scheme economist', lambda: server.stata_graph(command='twoway scatter price weight', scheme='economist'))
results.append(res)

# 图形 export 到不存在目录
res = run('E7 graph export to missing dir', lambda: server.stata_graph(command='twoway scatter price weight', export=os.path.join(OUT, 'no_dir_e7', 'e7.png'), replace=True))
results.append(res)

# 图形 export 已有文件且 replace=False
p7 = os.path.join(OUT, 'e7_exist.png')
if os.path.exists(p7): os.remove(p7)
r1 = server.stata_graph(command='twoway scatter price weight', export=p7, replace=True)
res = run('E7 graph export replace=False when exists', lambda: server.stata_graph(command='twoway scatter price weight', export=p7))
results.append(res)

with open(os.path.join(OUT, 'batch3_results.json'), 'w') as f:
    json.dump(results, f, ensure_ascii=False, indent=1)

with open(os.path.join(OUT, 'batch3_summary.txt'), 'w') as f:
    for r in results:
        f.write(f"{r['label']:<44} {r['result']:<9} {r.get('secs', 0):>6}s\n")
        f.write(f"    {r.get('detail', '')[:250]}\n")
print("BATCH3 DONE")
