# Stata MCP Server (NPM)

MCP server 通过 [pystata](https://www.stata.com/python/) 驱动真实 Stata：数据加载/清洗/建模/诊断/图形导出，75 个工具覆盖完整计量工作流。

## 前置依赖

- **Python 3.10+**
- **真实 Stata**（StataNow 19 / Stata 18，任意版本 mp/se/be）—— pystata 由 Stata 自带，从 `STATA_HOME/utilities` 加载
- 可选：`uv`（自动安装 fastmcp 依赖）；无 uv 时需 `pip install fastmcp>=3.2.0`

## 使用

```bash
# 环境变量（必填）
export STATA_HOME=/Applications/StataNow
export STATA_EDITION=mp

# MCP 客户端配置（stdio 传输）
# command: npx -y @aliveranme/stata-mcp
```

## 配置 MCP 客户端（Claude Code 示例）

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

## 安全

- 危险命令护栏（shell-out / 文件销毁 / 代码执行）与宏混淆防御
- 可选路径沙箱：`STATA_ALLOWED_ROOTS=/allowed/dir:/other`（分号分隔）

完整文档见 https://github.com/aliveranme/stata-mcp
