# Cursor 安装 Stata MCP Server

Cursor 通过 `.cursor/mcp.json`（项目级）或 `~/.cursor/mcp.json`（全局）配置 stdio
MCP server。启用/禁用在侧边栏 **Customize** 里用 toggle 开关——配置文件里没有
`enabled`/`trusted` 字段，信任与启用在 UI 表达。

## 前置条件

- **Stata**：StataNow 19 / Stata 18+（含 `utilities/pystata`）
- **Node.js** ≥ 18 与 `npx`（npm 方式）
- **Cursor** 最新版

## 步骤

### 1. 建配置

项目级 `.cursor/mcp.json`（放项目根）：

```json
{
  "mcpServers": {
    "stata": {
      "command": "npx",
      "args": ["-y", "@aliveranme/stata-mcp"],
      "env": {
        "STATA_HOME": "/Applications/StataNow",
        "STATA_EDITION": "mp"
      }
    }
  }
}
```

> 想全局可用就放 `~/.cursor/mcp.json`，schema 相同。`env` 支持 `${env:NAME}` 引用系统
> 环境变量（如 `"STATA_HOME": "${env:STATA_HOME}"`）。

### 2. 启用

打开侧边栏 **Customize** → 找到 `stata` server → 用 toggle 开启。

> 默认 Cursor 在使用 MCP 工具前会请求批准（Approval）；Run Mode 的 allowlist 工具可
> 直接运行。也可以在 CLI 用 `cursor mcp` 命令启用/禁用。

### 3. 验证

在 Cursor 对话里发：

> 帮我加载 auto.dta 并做描述统计

看到 `stata_use_dataset` → `stata_describe` → `stata_summarize` 的调用链即成功。

## 一键安装：cursor.directory

社区在 [cursor.directory](https://cursor.directory) 发布 MCP server，并给每个 server
一个 **"Add to Cursor"** 按钮，点击即自动写入 `.cursor/mcp.json` 并启用。把本 server
提交到 cursor.directory 后，用户就不用手写配置（见「插件 / 扩展分发可行性」文档）。

## 常见问题

| 问题 | 处理 |
|------|------|
| MCP 工具要逐次批准 | Customize 里对 `stata` 开启；Run Mode 下走 allowlist/classifier 规则 |
| server 没出现 | 确认 `.cursor/mcp.json` 在项目根或 `~/.cursor/mcp.json`，且 schema 的键是 `mcpServers` |
| 报 STATA_HOME 找不到 | `env` 里路径不含 `utilities/pystata`，或 `STATA_EDITION` 不匹配 |
| 首次启动慢 | 拉取 `fastmcp` + 初始化 Stata DLL，可能 30s+，给足超时 |
