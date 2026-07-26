"""真实 Stata 端到端测试包。

设为 package 是必要的：否则 pytest 会把本目录插进 sys.path 并以 ``conftest``
为模块名导入，与 ``tests/conftest.py`` 撞名，导致 ``tests/`` 里的
``from conftest import abs_path`` 解析到这里 —— 仓库根目录直接跑 pytest 会采集失败。
"""
