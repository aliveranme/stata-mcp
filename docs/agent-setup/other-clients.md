# 其他 MCP 客户端安装 Stata MCP Server

除 Claude Code / Claude Desktop / Cursor 之外，多数 MCP 客户端共用**同一份 stdio
JSON schema**，改一下文件位置/键名即可复用。安装本 MCP 的两种底层方式不变：

| 方式 | 命令 |
|------|------|
| npm 包 | `npx -y @aliveranme/stata-mcp`（需 STATA_HOME 环境变量） |
| 源码 | `python setup.py`（生成指向本机 venv 的配置） |

两种方式都要保证**环境变量 `STATA_HOME` / `STATA_EDITION`** 传进 server 进程——多数客户端
在配置的 `env` 字段里给。

## 通用 stdio schema（几乎全部客户端）

```json
{
  "mcpServers": {
    "stata": {
      "command": "npx",
      "args": ["-y", "@aliveranme/stata-mcp"],
      "env": { "STATA_HOME": "/Applications/StataNow", "STATA_EDITION": "mp" }
    }
  }
}
```

Claude Desktop / Claude Code / Cline / Roo Code / Windsurf / Cursor 的 `mcpServers`
对象**逐字一致**。下面的差异只在不同客户端的**文件位置 / 键名 / 格式**。

## 按客户端

| 客户端 | 配置位置 | 差异点 |
|--------|----------|--------|
| **Cline**（VS Code） | `~/.cline/mcp.json`；IDE 扩展在 MCP 设置面板 | schema 同通用；本地 stdio 用 `command`+`args` |
| **Roo Code**（VS Code） | 全局 `mcp_settings.json`；项目级 `.roo/mcp.json`（可入库共享） | 支持 `cwd`、`alwaysAllow`、`timeout` 字段；args 支持 `${env:VAR}` 展开 |
| **Continue**（VS Code） | 项目 `.continue/config.yaml` 的 `mcpServers` 列表；或把通用 JSON 拷到 `.continue/mcpServers/mcp.json` 自动识别 | **YAML 列表**；仅 agent 模式可用 |
| **Zed** | `~/.config/zed/settings.json`（`zed: open settings file`） | 键名是 **`context_servers`** 而非 `mcpServers` |
| **Windsurf**（Devin Cascade） | `~/.codeium/windsurf/mcp_config.json` | 支持 `${env:VAR}`、`${file:/path}` 插值；有 MCP Marketplace |

### Zed 示例（键名不同）

```json
{
  "context_servers": {
    "stata": {
      "command": "npx",
      "args": ["-y", "@aliveranme/stata-mcp"],
      "env": { "STATA_HOME": "/Applications/StataNow", "STATA_EDITION": "mp" }
    }
  }
}
```

### Continue 示例（YAML，放 `.continue/config.yaml`）

```yaml
mcpServers:
  - name: stata
    type: stdio
    command: npx
    args: ["-y", "@aliveranme/stata-mcp"]
    env:
      STATA_HOME: /Applications/StataNow
      STATA_EDITION: mp
```

## 验证

在每个客户端里找到 MCP 服务器列表，看到 `stata` 且状态为已连接后，发一句：

> 帮我加载 auto.dta 并做描述统计

正常应看到 `stata_use_dataset` → `stata_describe` → `stata_summarize` 的调用链。

## 其他已实现 MCP 的客户端

MCP 规范的 Extension Support Matrix 还列出：**VS Code GitHub Copilot、Goose（Block）、
Postman、MCPJam、ChatGPT、Cursor、Microsoft 365 Copilot** 等。其中 Goose / MCPJam /
Postman 对数据应用场景有提示价值，但配置细节请以各自官方文档为准（本表未逐一核验）。
