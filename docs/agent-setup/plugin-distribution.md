# 插件 / 扩展分发可行性评估

> 结论先行：**Claude Code 插件市场是可行且推荐的下一分发路径**（skill + MCP server
> 可打包进同一插件，发布 = 一个 GitHub 仓库即市场，无需打包 npm 依赖）。Claude
> Desktop 官方推荐 `.mcpb` 桌面扩展。Cursor / VS Code 走扩展打包（见下文逐平台表）。
> 当前 npm + setup.py 两种安装方式继续有效，插件化是**加一条**更省事的分发渠道。

## 各平台插件市场可行性

| 平台 | 市场机制 | 能否打包 MCP server | 可行性 | 落地方式 |
|------|----------|:---:|:---:|----------|
| **Claude Code** | `/plugin` + `marketplace.json`；GitHub 仓库即市场 | ✅ 插件根 `.mcp.json` 或 `plugin.json` 内联 `mcpServers` | **高（推荐）** | 插件带 `skills/stata/SKILL.md` + `.mcp.json` 声明 `npx -y @aliveranme/stata-mcp` |
| **Claude Desktop** | Settings → Extensions（`.mcpb` 桌面扩展，官方推荐） | ✅ `.mcpb` 打包自含运行时 | 中 | 发布 `.mcpb`；或继续手写 `claude_desktop_config.json` |
| **Cursor** | 自有插件市场 `cursor.com/marketplace` + Extension API | ✅ `vscode.cursor.mcp.registerServer()` 程序化注册（本地 stdio/远程） | 中高 | Extension API 注册 或 cursor.directory「Add to Cursor」/团队市场一键装 |
| **VS Code** | Visual Studio Marketplace / open-vsx（VSIX 发布，无 MCP 专项限制） | ✅ 官方 provider API `vscode.lm.registerMcpServerDefinitionProvider`（**proposed**） | 中 | 扩展内注册 stdio provider；open-vsx 已有同类实例（如 `DeepEcon/stata-mcp`） |
| **Cline / Roo Code / Windsurf** | 各有 MCP 面板，无插件市场分发 | — | 低 | 复用通用 stdio schema 手动配 |

## Claude Code 插件（首选路径）

### 为什么可行（官方机制实证）

- **一个插件能同时带 skill + MCP server**：目录布局同时容纳 `.claude-plugin/plugin.json`
  （manifest）、`skills/<name>/SKILL.md`、`.mcp.json`（MCP server 定义）。本机已装官方
  插件 `chrome-devtools-mcp` / `exa` 都是「MCP server + 多个 skills」的组合，`mongodb`
  插件也被官方市场描述为「bundles MCP server + skills」。
- **不打包 npm 依赖**：插件 `.mcp.json` 的 `command`/`args` 直接用 `npx -y <pkg>` /
  `python -m …`，运行时拉取 —— 官方市场 `playwright`（`npx @playwright/mcp`）、
  `context7`（`npx -y @upstash/context7-mcp`）都是这个模式。
- **发布 = GitHub 仓库**：含 `.claude-plugin/marketplace.json` 的 git 仓库即市场；
  用户 `claude plugin marketplace add <owner>/<repo>` 后 `claude plugin install <name>@<marketplace>`。
  官方市场 `claude-plugins-community` 接受第三方提交（表单申请 + 自动化安全筛查）。

### 本项目打包方案

```
stata-mcp-plugin/
├── .claude-plugin/
│   ├── plugin.json          # manifest：name/version/description/author
│   └── marketplace.json     # 市场清单（若本仓库直接作为市场）
├── skills/stata/SKILL.md    # 复用现有 .claude/skills/stata/（含 references/）
├── .mcp.json                # 声明 stata MCP server
└── README.md
```

`.mcp.json` 内容（复刻 playwright/context7 官方模式，直接引用已发布的 npm 包）：

```json
{
  "mcpServers": {
    "stata": {
      "command": "npx",
      "args": ["-y", "@aliveranme/stata-mcp"],
      "env": {
        "STATA_HOME": "${env:STATA_HOME}",
        "STATA_EDITION": "mp"
      }
    }
  }
}
```

> ⚠️ `STATA_HOME` 是用户机器相关的路径，**不能写死在插件里**。用 `${env:STATA_HOME}`
> 引用用户系统的环境变量（Claude Code 支持 `${env:VAR}` 替换），并在插件 README 里
> 说明「先 export STATA_HOME 指向你的 Stata 安装目录」。若用户未设该变量，也可在
> `SessionStart` hook 里引导设置。

### 落地步骤（未来）

1. `claude plugin init stata-mcp --with mcp --with skill` 脚手架
2. 迁入现有 `skills/stata/`（SKILL.md + references/）
3. `.mcp.json` 声明 stata server；`plugin.json` 补 author/description
4. `claude plugin validate` 校验 → `claude plugin install ./stata-mcp-plugin` 本地验证
5. 发布：推到 GitHub（本仓库或独立仓库），加 `.claude-plugin/marketplace.json`
6. （可选）提交到 `claude-plugins-community` 官方社区市场

## Claude Desktop：.mcpb 桌面扩展

Claude Desktop 现推荐 **Settings → Extensions** 一键安装本地 MCP server，`.mcpb`
打包格式自带 Node.js 运行时，用户无需装 npx/python。手动 `claude_desktop_config.json`
仍受支持但非推荐路径。本项目可后续用 `mcpb pack` 产出 `.mcpb`（见
`docs/agent-setup/claude-desktop.md` 的说明）。

## Cursor / VS Code

**Cursor** —— 有自己的扩展 API 与插件市场：
- `vscode.cursor.mcp.registerServer(config)` / `unregisterServer(name)` 程序化注册本地
  stdio 或远程 HTTP(S)/SSE server，用户装扩展即自动出现在 Customize 的 MCP 列表，**免改
  mcp.json**。
- 插件目录可含 `mcp.json`（folder-based discovery，`.cursor-plugin/plugin.json`）；市场
  插件（Render/Datadog 等）已内置 MCP config。
- 分发：`cursor.com/marketplace` 一键装 / 团队市场 / cursor.directory「Add to Cursor」。
- 注意：Cursor 是 VS Code 派生编辑器但**没有** VS Code 的 `vscode.lm.registerMcpServerDefinitionProvider`；
  要用它自己的 `vscode.cursor.mcp.*` 命名空间。

**VS Code** —— 官方一等支持（API 仍标 **proposed/实验性**）：
- 扩展贡献点 `contributes.mcpServerDefinitionProviders` + 运行时
  `vscode.lm.registerMcpServerDefinitionProvider()`，provider 返回
  `McpStdioServerDefinition(command, args, env)` 启动本地进程；官方示例
  `mcp-extension-sample` 可逐字参考。
- 发布走 VSIX（VS Marketplace / open-vsx），无 MCP 专项限制；open-vsx 已有打包型实例
  （含与本项目主题相同的 `DeepEcon/stata-mcp`）。
- 用户安装扩展后自动出现在 Extensions 视图的 "MCP SERVERS"。

**对本项目的意义**：Cursor 扩展（注册本地 stdio server）是性价比次高的分发路径；VS Code
扩展依赖 proposed API，等稳定后再做。两者都要处理 `STATA_HOME` 用户路径 —— 用
`${env:STATA_HOME}` 引用或引导用户设置。

## 建议

1. **近期**：先做 Claude Code 插件（收益最大、机制最成熟、已有全部组件）。
2. **中期**：`.mcpb` 给 Claude Desktop。
3. **观望**：Cursor / VS Code 扩展视用户分布决定。
