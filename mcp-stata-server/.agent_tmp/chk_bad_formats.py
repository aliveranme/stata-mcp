import sys, os, time
os.environ['STATA_HOME'] = '/Volumes/ccc/Applications/StataNow'
sys.path.insert(0, os.getcwd())
import server
OUT = '/Volumes/ccc/Projects/stata-mcp/mcp-stata-server/.agent_tmp/ge_out'
def txt(r):
    return r.content[0].text if hasattr(r, 'content') else str(r)
def run(label, fn):
    t0 = time.time()
    try:
        r = fn()
        text = txt(r)
        is_err = bool(getattr(r, 'is_error', False))
        crash = any(m in text for m in ('StataSO_Execute 崩溃', 'DLL 无响应', '已自动恢复'))
        kind = 'CRASH' if crash else ('ERROR' if is_err else 'PASS')
        print(f'{label:<30} {kind:<6} {round(time.time()-t0,2)}s  {text[:160].replace(chr(10), " | ")}')
    except Exception as e:
        print(f'{label:<30} EXCEPTION {type(e).__name__}: {e}')
server.stata_use_example('auto')
server.stata_run('clear all')
server.stata_use_example('auto')
for ext in ['gif', 'tif', 'emf', 'wmf', 'jpeg']:
    p = os.path.join(OUT, f'chk_bad.{ext}')
    if os.path.exists(p): os.remove(p)
    run(f'graph export .{ext}', lambda p=p: server.stata_graph(command='twoway scatter price weight', export=p, replace=True))
