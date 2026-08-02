"""检查 E2 尺寸选项的完整提示文本。"""
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

def show(label, p, **kw):
    if os.path.exists(p): os.remove(p)
    r = server.stata_graph(command='twoway scatter price weight', export=p, **kw)
    t = txt(r)
    print('='*70)
    print('LABEL:', label, 'is_error=', getattr(r, 'is_error', False))
    print(t)
    print()

show('pdf width=800 (default, expect drop hint)', os.path.join(OUT, 'chk_pdf800.pdf'))
show('pdf width=6 (expect no hint)', os.path.join(OUT, 'chk_pdf6.pdf'))
show('png quality=80 (expect drop hint)', os.path.join(OUT, 'chk_png_q.png'), quality=80)
show('eps width=800 (expect drop hint)', os.path.join(OUT, 'chk_eps800.eps'), width=800)
show('svg width=800 (expect no hint)', os.path.join(OUT, 'chk_svg800.svg'), width=800)
show('png width=400 height=300', os.path.join(OUT, 'chk_png400.png'), width=400, height=300)
