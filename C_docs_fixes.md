# 文档与配置示例修复报告

## 修复内容

本次修复仅涉及文档和配置示例文件，未修改源码 `server.py` 或 `setup.py`。

### 1. STATA_HOME 默认值统一

`server.py` 中实际默认值为 `C:\Program Files\StataNow\StataNow19`。已将以下文件中的默认值统一为该值，并明确说明 `setup.py` 会自动检测或可由用户手动设置：

- **README.md**
  - 手动安装示例：`set STATA_HOME=D:/StataNow19` → `set STATA_HOME="C:/Program Files/StataNow/StataNow19"`
  - 环境变量表格：默认值改为 `C:\Program Files\StataNow\StataNow19`，说明增加"环境变量优先级最高；未设置时由 `setup.py` 自动检测"。
- **CLAUDE.md**
  - 环境变量表格说明更新为"优先级：环境变量 > `setup.py` 自动检测 > 该默认值；手动安装时可在 `.mcp.json` 中覆盖"。
- **.mcp.json.example**
  - `"STATA_HOME": "D:/StataNow19"` → `"STATA_HOME": "C:/Program Files/StataNow/StataNow19"`，`<repo-path>` 占位符保持不变。

### 2. SKILL.md 增加 `!` 系统命令风险提示

在"与 Agent 协作规范"部分新增第 10 条：

> 10. **谨慎使用 `!` 系统命令**：`stata_run` 可执行任意 Stata 命令，包括 `!` 开头的操作系统命令（如 `! del file.txt`）。在未明确告知用户风险前，不要主动构造删除、修改系统文件或执行 shell 的命令。

### 3. README.md `stata_graph` 花括号提示

工具列表中 `stata_graph` 的说明增加：

> 注意：`export` 模式会自动用 `{ }` 包装命令，请勿在 `command` 中手动包含未转义的 `}`。

## 检查方法

由于当前环境无法直接调用项目目录下的 `git diff`（WSL 未挂载 Windows 盘），通过 `grep` 核对关键内容已确认修改生效：

- `README.md:53`: `set STATA_HOME="C:/Program Files/StataNow/StataNow19"`
- `README.md:195`: `| STATA_HOME | C:\Program Files\StataNow\StataNow19 | ...`
- `README.md:118`: `stata_graph` 说明包含"未转义的 `}`"
- `CLAUDE.md:46`: `| STATA_HOME | C:\Program Files\StataNow\StataNow19 | 优先级：环境变量 > setup.py 自动检测 ...`
- `.mcp.json.example:7`: `"STATA_HOME": "C:/Program Files/StataNow/StataNow19"`
- `SKILL.md:392`: 新增"谨慎使用 `!` 系统命令"条目

## 验收

- [x] README.md / CLAUDE.md / .mcp.json.example 关于 STATA_HOME 默认值统一为 `C:\Program Files\StataNow\StataNow19`
- [x] SKILL.md 已新增 `!` 系统命令风险提示
- [x] 未修改 `server.py` 或 `setup.py`
