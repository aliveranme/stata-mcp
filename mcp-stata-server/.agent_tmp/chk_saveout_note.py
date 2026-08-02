import sys, os
os.environ['STATA_HOME'] = '/Volumes/ccc/Applications/StataNow'
sys.path.insert(0, os.getcwd())
import server
OUT = '/Volumes/ccc/Projects/stata-mcp/mcp-stata-server/.agent_tmp/ge_out'
def txt(r):
    return r.content[0].text if hasattr(r, 'content') else str(r)
server.stata_use_example('auto')
server.stata_run('clear all')
server.stata_use_example('auto')
server.stata_run('set obs 8000')
server.stata_run('gen id = _n')
server.stata_run('gen x1 = rnormal()')
big = os.path.join(OUT, 'chk_big.txt')
if os.path.exists(big): os.remove(big)
r = server.stata_run('list id x1, nolabel', save_output=big)
t = txt(r)
print('full len:', len(t))
print('has 完整输出已保存:', '完整输出已保存' in t)
print('has 已截断:', '已截断' in t)
# 找到 note 位置
i = t.find('完整输出已保存')
print('note idx:', i)
print('CONTEXT:', t[max(0,i-80):i+180] if i>=0 else 'NOT FOUND')
