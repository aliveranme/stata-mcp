#!/usr/bin/env python3
"""stata-mcp · MCPB 启动器

在 ${__dirname} 下运行 server.py 的 main()。要求：
- Python 3.10+（当前进程的解释器，由 manifest 的 mcp_config.command 指定；
  macOS 系统自带 /usr/bin/python3 是 3.9，不满足）
- fastmcp>=3.2.0（缺失时给出安装指引后退出）
- 真实 Stata（STATA_HOME 指向含 utilities/pystata 的目录，安装时配置）
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

# macOS 系统自带 python3 是 3.9，而 server.py 用 PEP 604 语法（str | None）
if sys.version_info < (3, 10):
    sys.stderr.write(
        f"[stata-mcp] 需要 Python 3.10+，当前是 {sys.version.split()[0]}。\n"
        "  · 用 uv 或 Homebrew 装新版 python，然后改 manifest 的 command，或\n"
        "  · 在 Claude Desktop 扩展里选择带 Python 3.10+ 的解释器。\n"
    )
    sys.exit(1)

try:
    import fastmcp  # noqa: F401
except ImportError:
    sys.stderr.write(
        "[stata-mcp] 未找到 fastmcp。请先安装：\n"
        "  uv pip install 'fastmcp>=3.2.0'    # 或用 pip\n"
        "然后重新连接本扩展。\n"
    )
    sys.exit(1)

from server import main  # noqa: E402

if __name__ == "__main__":
    main()
