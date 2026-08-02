# Claude Desktop 安装 Stata MCP Server

Claude Desktop 通过 `claude_desktop_config.json` 配置本地 stdio MCP server。Claude
Desktop 的 stdio 子进程**只继承有限的平台环境子集**，所以 `STATA_HOME` / `STATA_EDITION`
必须显式写在配置的 `env` 字段里。

## 前置条件

- **Stata**：StataNow 19 / Stata 18+（含 `utilities/pystata`）
- **Node.js** ≥ 18 与 `npx`（npm 方式）或源码 `setup.py`
- **Claude Desktop** 最新版

## 步骤

### 1. 打开配置

- **macOS**：`~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**：`%APPDATA%\Claude\claude_desktop_config.json`
  （`C:\Users\<you>\AppData\Roaming\Claude\claude_desktop_config.json`）

UI 入口：菜单栏 **Claude 菜单 → Settings…**（不是窗口内设置）→ 左侧 **Developer**
选项卡 → **Edit Config** 按钮（不存在则自动创建）。

### 2. 写入配置

```json
{
  "mcpServers": {
    "stata": {
      "command": "npx",
      "args": ["-y", "@aliveranme/stata-mcp"],
      "env": {
        "STATA_HOME": "C:/Program Files/StataNow/StataNow19",
        "STATA_EDITION": "mp"
      }
    }
  }
}
```

> - macOS 把 `STATA_HOME` 换成你的路径（如 `/Applications/StataNow`）；Windows 路径
>   在 JSON 里用双反斜杠（`C:\\Program Files\\...`）转义。
> - `command` 用绝对路径最稳：若 `npx` 不在 PATH 里，写成 `"C:\\Program Files\\nodejs\\npx.cmd"`。
> - 也可以改用源码方式：`command` 指向 `setup.py` 建的 venv python，`args` 指向
>   `server.py`（此时仍需 `env` 里的 `STATA_HOME`）。

### 3. 重启 Claude Desktop

改完配置后**完全退出并重启** Claude Desktop（`Cmd+Q`，不是关窗口）。启动后在对话里发：

> 帮我加载 auto.dta 并做描述统计

看到 `stata_use_dataset` → `stata_describe` → `stata_summarize` 的调用链即成功。

## 排障

| 问题 | 处理 |
|------|------|
| `spawn npx ENOENT` | `npx` 不在 PATH；改用绝对路径（见上） |
| Server 未连接 / 工具为空 | 查看日志：macOS `~/Library/Logs/Claude`、Windows `%APPDATA%\Claude\logs`（`mcp-server-stata.log`） |
| 报 STATA_HOME 找不到 | `env` 里没传或路径不含 `utilities/pystata`；`STATA_EDITION` 与你安装的版本一致 |
| 首次启动慢 | 要拉取 `fastmcp` + 初始化 Stata DLL，可能 30s+，给足超时 |

## 更推荐的新路径：桌面扩展（.mcpb）

Claude Desktop 现推荐用 **Settings → Extensions** 一键安装本地 MCP server（`.mcpb`
打包格式，自带 Node.js 运行时，无需用户装 npx）。手动 `claude_desktop_config.json`
仍受支持且有效，但扩展是官方当前推荐路径 —— 本项目可发布 `.mcpb` 扩展，见
「插件 / 扩展分发可行性」文档。
