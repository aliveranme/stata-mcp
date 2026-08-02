# Stata MCP Server · 各 Agent 安装教程

本 MCP server 通过 **stdio** 运行在本地，可接入所有支持 MCP 的 Agent / 客户端。
两种底层安装方式选一，然后在对应 Agent 里配置连接。

## 安装方式（先二选一）

| 方式 | 优点 | 命令 |
|------|------|------|
| **npm 包**（推荐） | 无需 clone 仓库，一条命令 | `npx -y @aliveranme/stata-mcp` |
| **setup.py 源码** | 本地 venv、跨平台检测 Stata | `python setup.py` |

两种方式都要让 Agent 把 **`STATA_HOME` / `STATA_EDITION`** 环境变量传给 server 进程
（多数 Agent 在配置的 `env` 字段里给）。

> 前置依赖：真实 Stata（StataNow 19 / Stata 18+，含 `utilities/pystata`）、Python 3.10+
> （npm 方式有 `uv` 时自动装 `fastmcp`）。

## 各 Agent 教程

| Agent / 客户端 | 教程 | 配置要点 |
|------|------|----------|
| **Claude Code**（主要） | [claude-code.md](./claude-code.md) | `.mcp.json` 或 `claude mcp add`；项目级/用户级 |
| **Claude Desktop** | [claude-desktop.md](./claude-desktop.md) | `claude_desktop_config.json`；stdio env 有限需显式写 `env` |
| **Cursor** | [cursor.md](./cursor.md) | `.cursor/mcp.json` 或 `~/.cursor/mcp.json`；Customize 里启用 |
| **其他 MCP 客户端**（Cline / Roo Code / Continue / Zed / Windsurf） | [other-clients.md](./other-clients.md) | 通用 `mcpServers` schema；Zed 用 `context_servers` 键名，Continue 用 YAML |

## 插件 / 扩展市场分发（未来路径）

[plugin-distribution.md](./plugin-distribution.md) 评估了以插件形式通过各 Agent 插件市场
分发本 MCP 的可行性：**Claude Code 插件（skill + MCP 打包，GitHub 仓库即市场）为首选**，
其次 Cursor 扩展（`vscode.cursor.mcp.registerServer`）与 Claude Desktop `.mcpb`。npm +
setup.py 手动安装继续有效。

## 验证（所有 Agent 通用）

配好并启用后，在 Agent 对话里发一句：

> 帮我加载 auto.dta 并做描述统计

正常应看到 `stata_use_dataset` → `stata_describe` → `stata_summarize` 的调用链。
首次启动要拉取 `fastmcp` 并初始化 Stata DLL，可能 30s+，请给客户端足够超时。
