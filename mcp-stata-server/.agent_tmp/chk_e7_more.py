import sys, os, time
os.environ['STATA_HOME'] = '/Volumes/ccc/Applications/StataNow'
sys.path.insert(0, os.getcwd())
import server
OUT = '/Volumes/ccc/Projects/stata-mcp/mcp-stata-server/.agent_tmp/ge_out'
def txt(r):
    return r.content[0].text if hasattr(r, 'content') else str(r)
server.stata_use_example('auto')
server.stata_run('clear all')
server.stata_use_example('auto')

# 0 obs 但变量存在 —— 更纯粹的"空数据图"
server.stata_run('clear')
server.stata_run('gen x = 1')
server.stata_run('gen y = 2')
p = os.path.join(OUT, 'e7b_zerobs.png')
if os.path.exists(p): os.remove(p)
r = server.stata_graph(command='twoway scatter y x', export=p, replace=True)
print('=== E7 zero-obs with vars present ===')
print('is_error=', getattr(r, 'is_error', False))
print(txt(r))
print()

# 语法合法但 scheme 不存在
server.stata_use_example('auto')
r = server.stata_graph(command='twoway scatter price weight', scheme='nonexistentscheme')
print('=== E7 valid-format but nonexistent scheme ===')
print('is_error=', getattr(r, 'is_error', False))
print(txt(r))
print()

# export 参数非法控制字符
r = server.stata_graph(command='twoway scatter price weight', export='/tmp/x\x00.png')
print('=== E7 export path with null byte ===')
print('is_error=', getattr(r, 'is_error', False))
print(txt(r))
