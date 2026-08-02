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
server.stata_regress('price', 'weight mpg foreign')

# raw etable export to missing dir
r = server.stata_run('etable, export("/Volumes/ccc/Projects/stata-mcp/mcp-stata-server/.agent_tmp/ge_out/no_such_dir/e4raw.xlsx")')
print('--- raw etable to missing dir ---')
print('is_error=', getattr(r, 'is_error', False))
print(txt(r))

# check the dir truly doesn't exist
import glob
print('no_such_dir exists:', os.path.isdir(os.path.join(OUT, 'no_such_dir')))

# try with replace
r = server.stata_run('etable, export("/Volumes/ccc/Projects/stata-mcp/mcp-stata-server/.agent_tmp/ge_out/no_such_dir/e4raw2.xlsx", replace)')
print('--- raw etable to missing dir with replace ---')
print('is_error=', getattr(r, 'is_error', False))
print(txt(r))
