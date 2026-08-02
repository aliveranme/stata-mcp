# stata-mcp · Claude Code 插件

一个插件同时提供 **MCP server**（驱动真实 Stata 的 75 个工具）与 **Stata 编程知识
skill**（语法要点、分析模板、常见陷阱）。

## 组件

| 组件 | 位置 | 说明 |
|------|------|------|
| MCP server | `.mcp.json` | 声明 `stata` server（`npx -y @aliveranme/stata-mcp`），随插件启用自动启动 |
| Skill | `skills/stata/SKILL.md` | Stata 编程指南（含 `references/`：命令地图/分析模板/第三方包） |

## 安装

**前置依赖**（两选一）：真实 Stata（StataNow 19 / Stata 18+，含 `utilities/pystata`）、
Node.js ≥ 18 与 `npx`；无 uv 时需 Python 3.10+。

### 方式一：从市场安装

```bash
# 本仓库即市场（.claude-plugin/marketplace.json）
claude plugin marketplace add aliveranme/stata-mcp
claude plugin install stata-mcp@stata-mcp-marketplace
```

### 方式二：本地安装

```bash
claude plugin install ./plugin
```

### 方式三：测试模式

```bash
claude --plugin-dir ./plugin
```

## 使用前必做：设置 STATA_HOME

插件 `.mcp.json` 用 `${STATA_HOME}` 引用系统环境变量。**先 export 指向你的 Stata
安装目录**（含 `utilities/pystata`），否则 server 启动会 FATAL 退出：

```bash
export STATA_HOME="/Applications/StataNow"      # 你的实际路径
export STATA_EDITION=mp                          # mp / se / be
```

> 必须**先 export 再启动 Claude Code**（Claude Code 与插件 MCP 子进程继承该环境变量）。
> 未设置时 server 会打印 `FATAL: Stata utilities directory not found` 并退出。

## 验证

安装后重启 Claude Code（或 `/reload-plugins`），对话里说：

> 帮我加载 auto.dta 并做描述统计

看到 `stata_use_dataset` → `stata_describe` → `stata_summarize` 即成功。
首次启动要拉取 `fastmcp` + 初始化 Stata DLL，可能 30s+，给足超时。
