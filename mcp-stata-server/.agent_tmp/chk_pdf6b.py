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
p = os.path.join(OUT, 'chk_pdf6b.pdf')
if os.path.exists(p): os.remove(p)
r = server.stata_graph(command='twoway scatter price weight', export=p, width=6)
print(txt(r))
print('SIZE:', os.path.getsize(p))
