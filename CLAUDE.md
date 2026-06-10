# Stata MCP Server

让 Claude Code Agent 通过 MCP Server 直接驱动 Stata，自动完成数据加载、清洗、建模、结果导出全流程。

## 架构

```
stata-mcp/
├── mcp-stata-server/server.py       # MCP 执行层：20 个工具，通过 pystata 调用 Stata DLL
├── .claude/skills/stata/SKILL.md    # 知识层：Stata 语法、模板、陷阱、Agent 协作规范
├── setup.py                         # 安装层：检测 Stata、创建 venv、生成 .mcp.json
├── .gitignore                       # 忽略 .mcp.json(生成) .venv dta/log/smcl
```

**关键链路**：`Claude + SKILL.md → 调用 MCP 工具 → server.py → pystata → Stata DLL`

**会话持久**：Stata 在 MCP Server 启动时初始化一次，所有工具调用共享同一会话，数据跨调用保持。

## 命令

| 命令 | 说明 |
|------|------|
| `python setup.py` | 一键安装（检测 Stata → venv → fastmcp → .mcp.json → 验证） |
| `cd mcp-stata-server && source .venv/Scripts/activate && python server.py` | 调试模式启动 |
| `uv pip install fastmcp && uv pip freeze > requirements.txt` | 添加新依赖 |

## MCP 工具（20 个）

| 类别 | 工具 | 只读? | 说明 |
|------|------|:-----:|------|
| 核心执行 | `stata_run`, `stata_run_do_file` | — | 通用命令执行和 do 文件 |
| 数据管理 | `stata_use_dataset`, `stata_save_dataset`, `stata_set_cwd` | — | 读写 .dta、cd |
| 数据探索 | `stata_describe`, `stata_codebook`, `stata_summarize`, `stata_list`, `stata_tabulate`, `stata_display` | ✓ | 只读探索 |
| 分析 | `stata_regress`, `stata_logistic`, `stata_ttest` | ✓ | OLS / Logit / t 检验 |
| 图形 | `stata_graph` | ✓ | 执行图形命令（无显示窗口） |
| 包管理 | `stata_install_package`, `stata_find_package`, `stata_list_packages` | — | ssc/net 安装 |
| 翻页 | `stata_more` | ✓ | 大输出分页浏览（缓存 120K chars） |
| 会话 | `stata_status` | ✓ | 数据集 + 工作目录 + 内存 |

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `STATA_HOME` | `C:\Program Files\StataNow\StataNow19` | Stata 安装目录（setup.py 自动检测时优先于 env var） |
| `STATA_EDITION` | `mp` | 版本 (mp/se/be) |

## 关键设计决策

### pystata 而非 subprocess

- **直接 DLL 调用**：pystata 通过 ctypes 加载 `mp-64.dll`，绕过进程开销
- **真会话持久**：数据在工具调用间保持，无需反复 `use`
- **串行化**：`_stata_lock` 确保所有命令在单线程中顺序执行（Stata DLL 非线程安全）

### RedirectOutput 必要性

每个 `StataSO_Execute` 调用必须包裹在 `RedirectOutput(StataDisplay, StataError)` 中。
否则 Stata 输出直接写入 `sys.stdout`（即 MCP stdio 通道），污染 JSON-RPC 协议 -> 终端崩溃。

### 输出收集

```
执行前: _drain_output()     — 排空残留缓冲（200ms 上限 + 30ms 安静退出）
执行中: StataSO_Execute     — 同步调用，60s 超时看门狗
执行后: 快轮询(300×1ms)     — 收集主体输出
        _drain_output()     — 收集尾部输出（复用同一函数）
        截断 120K chars     — 防止 MCP 缓冲溢出
        自动分页 4K chars    — 大输出自动分页，支持 stata_more 翻页
```

## Gotchas

- **第三方绘图包（binscatter 等）在 headless 环境会挂起**：Stata DLL 试图创建 GUI 窗口失败。一律使用原生命令（`twoway scatter`、`histogram`），或先用 `set graphics off`。
- **`StataSO_Execute` 不支持多行**：内部按 `\n` 拆分为逐条命令调用，每条独立输出收集。`cmd\nestat vif` 在代码层执行两次 `_execute_single`。
- **`winsor2` 的 `suffix(_w)` 不能和 `replace` 一起用**：选项冲突。要么 `suffix(_w)` 创建新变量，要么 `replace` 覆盖原变量。
- **`graph export` 必须紧接着图形命令**：图形窗口不持久，分两次调用时中间可能丢失。需在同一个 `stata_run` 或紧随的调用中完成。
- **`stata_more` 只能翻上次命令的缓存**：之间不能插入其他命令，否则 `_last_output` 被覆盖。
- **`//` 和 `*` 注释的行在 `_run_stata_command` 中被过滤跳过**，不会传向 Stata。
- **`.mcp.json` 有用户路径，已 gitignore**：clone 后必须运行 `setup.py` 生成。

## 错误处理策略

```
Stata 命令执行异常链:
  input_validation (length check)
    → _execute_single()
        → _drain_output()  # 清洁缓冲
        → watchdog (60s)    # time-thread.Event → StataSO_SetBreak
        → RedirectOutput    # 防 stdout 污染
        → StataSO_Execute   # try/except
        → _drain_output(if break)  # break 后排空错误残渣
        → 输出收集 + 截断
    → _run_stata_command 收集各命令结果
    → 返回分页文本 或 原始文本
```

- 命令错误（返回码非 0）→ 返回 `[返回码: N]` + 错误文本，不崩溃
- DLL 崩溃（`StataSO_Execute` 异常）→ 返回 `CRASH` 消息，看门狗触发 `SetBreak`
- MCP Server 崩溃 → Claude Code 自动重连，Stata 重启（数据丢失）

## 输出大小管理

- `stata_list` 默认仅显示前 10 条
- `stata_list(n=0)` 显示全部（>4K 字符自动分页）
- `stata_more(page=N)` 翻页，`page=0` 显示全部
- 输出上限 120K 字符（Claude Code 约束）
- 任何时候优先 `summarize` / `tabulate` / `codebook` 而非 `list`
