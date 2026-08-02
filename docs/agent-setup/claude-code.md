# Claude Code 安装 Stata MCP Server

Claude Code 是本项目的主要使用场景。Skill（知识层）与 MCP Server（执行层）配合，
在对话里用自然语言驱动真实 Stata。

## 前置条件

- **Stata**：StataNow 19 / Stata 18+（MP / SE / BE），含 `utilities/pystata`
- **Python** 3.10+（推荐 3.12+）；有 `uv` 时由它自动装 `fastmcp`，无需手动
- **Node.js** ≥ 18 与 `npx`（用 npm 安装方式时）
- **Claude Code** 最新版

## 方式 A：npm 包（推荐，无需 clone 仓库）

```bash
# 1. 确认 Stata 路径（含 utilities/pystata 的目录）并导出
export STATA_HOME="/你的/Stata路径"     # 例 /Applications/StataNow 或 C:/Program Files/StataNow/StataNow19
export STATA_EDITION=mp                  # mp / se / be

# 2. 用 CLI 一键注册到 Claude Code（项目级）
claude mcp add stata -- npx -y @aliveranme/stata-mcp
# 或指定 env：
claude mcp add stata --env STATA_HOME=/你的/Stata路径 --env STATA_EDITION=mp -- npx -y @aliveranme/stata-mcp
```

> `claude mcp add` 会自动把 server 写进 `.mcp.json`（项目级）或 `~/.claude.json`（用户级）。
> 也可以用 `claude mcp add --scope user` 注册到所有项目。

**等价的手写 `.mcp.json`**（放项目根目录）：

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

> `npx @aliveranme/stata-mcp` 与 `npx stata-mcp-server`（bin 名）等价，都触发同一个启动器。

## 方式 B：setup.py 一键安装（源码）

```bash
git clone https://gitea.aliveranme.space/aliveranme/stata-mcp.git
cd stata-mcp
python setup.py      # 检测 Stata → 建 venv → 装 fastmcp → 生成 .mcp.json
```

> `setup.py` 会把 server 写进项目根 `.mcp.json`，并保留你已有的其他 MCP Server 配置。
> 装在非标准位置（如外置卷）时先 `export STATA_HOME=/你的/Stata路径` 再跑。

## 验证

1. 重启 Claude Code（或 `/reload-plugins`），看到 `stata` Server 已连接
2. 对话里说：

> 帮我加载 auto.dta 并做描述统计

3. 正常会看到 Agent 走 `stata_use_dataset` → `stata_describe` → `stata_summarize`

也可用 `claude mcp list` 检查注册状态，`claude mcp get stata` 查看详情。

## 常见问题

| 问题 | 处理 |
|------|------|
| Server 启动超时 | 首次启动要拉取 `fastmcp` + 初始化 Stata DLL，可能 30s+；MCP 客户端超时给足 90s |
| 报「STATA_HOME 未找到」 | `STATA_HOME` 必须指向**含 `utilities/pystata`** 的 Stata 安装根目录，且 `STATA_EDITION` 与你安装的版本一致 |
| 工具弹权限确认 | 首次调用 MCP 工具会逐次弹窗；在对话里对 `stata` server 选「始终允许」即可免弹 |
| `claude mcp add` 报环境变量 | 用 `--env` 逐项传入（见方式 A 示例） |
