import sys, os
os.environ['STATA_HOME'] = '/Volumes/ccc/Applications/StataNow'
sys.path.insert(0, os.getcwd())
import server
def txt(r):
    return r.content[0].text if hasattr(r, 'content') else str(r)
server.stata_use_example('auto')
server.stata_run('clear all')
server.stata_use_example('auto')
server.stata_run('set obs 8000')
server.stata_run('gen id = _n')
server.stata_run('gen x1 = rnormal()')
r = server.stata_run('list id x1, nolabel')
p1 = txt(r)
print('PAGE1 has truncation notice:', '已截断' in p1)
print('--- PAGE1 tail ---')
print(p1[-120:])
print()
r = server.stata_more(page=31)
p31 = txt(r)
print('PAGE31 has truncation notice:', '已截断' in p31)
print('--- PAGE31 tail ---')
print(p31[-200:])
