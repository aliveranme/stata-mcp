import sys, os
os.environ['STATA_HOME'] = '/Volumes/ccc/Applications/StataNow'
sys.path.insert(0, os.getcwd())
import server
OUT = '/Volumes/ccc/Projects/stata-mcp/mcp-stata-server/.agent_tmp/ge_out'
big = os.path.join(OUT, 'chk_big.txt')
# 该文件在上一个进程登记过 —— 本进程是全新会话，需重新登记
r = server.stata_register_file(big)
print('register:', getattr(r, 'is_error', False), r.content[0].text[:120] if hasattr(r,'content') else str(r)[:120])
try:
    data = server._stata_file_resource('chk_big.txt' if False else big)
    print('resource read bytes:', len(data))
    print('starts with list header:', data[:60])
except Exception as e:
    print('resource read EXCEPTION:', type(e).__name__, str(e)[:200])
