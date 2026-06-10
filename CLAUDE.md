# Stata MCP Server

让 Claude Code Agent 通过 MCP Server 直接驱动 Stata，自动完成数据加载、清洗、建模、结果导出全流程。

## 架构

```
stata-mcp/
├── mcp-stata-server/server.py       # MCP 执行层：22 个工具，通过 pystata 调用 Stata DLL
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

## MCP 工具（23 个）

| 类别 | 工具 | 只读? | 说明 |
|------|------|:-----:|------|
| 核心执行 | `stata_run`, `stata_run_do_file` | — | 通用命令执行和 do 文件 |
| 数据管理 | `stata_use_dataset`, `stata_save_dataset`, `stata_set_cwd` | — | 读写 .dta、cd |
| 数据探索 | `stata_describe`, `stata_codebook`, `stata_summarize`, `stata_list`, `stata_tabulate`, `stata_display` | ✓ | 只读探索 |
| 分析 | `stata_regress`, `stata_logistic`, `stata_ttest` | ✓ | OLS / Logit / t 检验 |
| 图形 | `stata_graph` | ✓ | 执行图形命令，支持 scheme 样式和自动导出 |
| 导出 | `stata_export_excel` | — | 数据集或回归结果导出为 .xlsx |
| 包管理 | `stata_install_package`, `stata_find_package`, `stata_list_packages` | — | ssc/net 安装 |
| 翻页 | `stata_more` | ✓ | 大输出分页浏览（缓存 120K chars） |
| 会话 | `stata_status` | ✓ | 数据集 + 工作目录 + 内存 |
| 心跳 | `stata_ping` | ✓ | 快速检测 Stata DLL 存活状态 |

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

### `_parse_command_blocks` 解析器

修复了两个关键问题：
1. **`///` 续行符**不被误判为 `//` 注释过滤
2. **`{ }` 复合块**跨多行合并为单次 `StataSO_Execute` 调用（解决 graph + export 原子性问题）

解析规则：
- `*` 行首 → 注释跳过
- `//` 行（且不是 `///`）→ 注释跳过
- `///` 行尾 → 续行符，合并到下一行
- `{` 出现但 `}` 不在同行的 → 复合块开始，收集直到 `}` 闭合

## Gotchas

- **第三方绘图包（binscatter 等）在 headless 环境会挂起**：Stata DLL 试图创建 GUI 窗口失败。一律使用原生命令（`twoway scatter`、`histogram`），或先用 `set graphics off`。
- **`graph export` 必须和图形命令在同一个 Stata 复合块 `{ }` 内**，或使用 `stata_graph(..., export="path.png")` 一次完成。图形窗口不跨 `StataSO_Execute` 调用持久。
- **`///` 续行符**：现在已被修复支持（版本 v2+），可在 `stata_run` 中使用 `\`\``\` 连接多行长命令。
- **`{ }` 复合块**：用于将多条命令打包为单次执行。典型用途：`capture noisily { twoway ... \n graph export ..., replace }`。
- **`winsor2` 的 `suffix(_w)` 不能和 `replace` 一起用**：选项冲突。要么 `suffix(_w)` 创建新变量，要么 `replace` 覆盖原变量。
- **`stata_more` 只能翻上次命令的缓存**：之间不能插入其他命令，否则 `_last_output` 被覆盖。
- **`.mcp.json` 有用户路径，已 gitignore**：clone 后必须运行 `setup.py` 生成。
- **Stata display 不支持中文字符串直接传参**：使用 Stata 的 `\`"中文\"'` 引号语法替代单引号 `'中文'`。

## 故障检测与恢复策略

### 心跳检测
使用 `stata_ping()` 在每次工具调用前快速验证 Stata DLL 存活。返回 `pong` 时可用。

### 安全执行链（`_execute_safe`）
每一条命令经过三层保护：
```
_ping_stata()          # 预检：DLL 是否存活（2 次尝试 + SetBreak 恢复）
  → _execute_single()  # 执行：60s 超时看门狗 + RedirectOutput
    → rc==999 检测     # 崩溃后恢复：排空缓冲 + SetBreak + 重 ping
      → RC=998 终止    # 若 Stata 无响应，终止后续命令，返回错误信息
```

| 返回码 | 含义 | 行为 |
|:------:|:-----|:-----|
| 0 | 成功 | 正常返回输出 |
| 3000 | 无实质输出（如 r-class） | 返回 "(命令执行成功，无文本输出)" |
| 198/其他 | Stata 命令语法错误 | 返回 `[返回码: N]` + 错误文本 |
| 999 | StataSO_Execute 崩溃 | 自动恢复 Stata 会话 + 提示重试 |
| 998 | DLL 无响应 | 立即终止后续命令 + 提示重启 MCP Server |

### MCP 断线处理
当 MCP Server 崩溃时（DLL 崩溃或连接断开），`_execute_safe` 返回 **RC=998** + 明确错误信息，**不会自动执行任何脚本**。Agent 收到错误后应：

1. **分析错误信息**：判断是 DLL 崩溃、超时还是语法错误
2. **调整策略**：简化命令、避免复杂图形、或切换到更轻量的操作
3. **若需恢复**：提示用户重启 Claude Code（`! exit` → 重新启动）

### 错误处理策略

```
Stata 命令执行异常链:
  input_validation (length check)
    → _parse_command_blocks()   # 解析 /// 续行和 { } 复合块
    → _execute_single()
        → _drain_output()       # 清洁缓冲
        → watchdog (60s)        # time-thread.Event → StataSO_SetBreak
        → RedirectOutput        # 防 stdout 污染
        → StataSO_Execute       # try/except
        → _drain_output(if break)  # break 后排空错误残渣
        → 输出收集 + 截断
    → 收集各命令结果
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

## 工具调用效率

### 批量命令优先
- 每次 `stata_run` 可在 `\n` 后跟多条命令，全部在一个往返中完成
- 推荐：`stata_run("regress mpg weight\nestat vif\nestat hettest")` — 3 条一次往返
- 不推荐：3 次独立 `stata_run`（3 倍往返开销，约 3 x 1.5s）
- 适用于：多步建模（加载→清洗→回归→诊断）

### 并行工具调用
- 数据探索类工具（describe、summarize、codebook、tabulate）**互不依赖**
- 应该一次性并行发送，而非逐条等待
- 回归/图形等工具需要前序结果，必须顺序执行

### 图形导出最佳实践
```stata
* ✅ 推荐：使用 stata_graph 的 export 参数（自动用 .do 文件包裹）
stata_graph(command="twoway scatter price weight", export="graph.png", scheme="economist")

* ✅ 也支持：在 stata_run 中用 { } 复合块（自动合并为单次执行）
stata_run("capture noisily {
    set scheme s2color
    twoway scatter price weight
    graph export 'graph.png', replace width(800)
}")

* ❌ 不要分两步做（图形窗口丢失）
stata_run("scatter price weight")
stata_run("graph export graph.png, replace")   ← 可能失败：r(601)
```

### 输出分页工作流
- `stata_list(n=0)` — 大输出（>4K chars）自动返回第 1 页
- `stata_more(page=N)` — 翻页（`page=0` 时返回全部未截断的文本）
- `stata_more` 缓存的是上次命令的完整输出，中间不要插入其他 `stata_run`

## 已修复的崩溃历史

| 触发 | 根因 | 修复 |
|------|------|------|
| 任意命令 | `StataSO_Execute` stdout 泄漏→JSON-RPC 污染 | `RedirectOutput` + `streamout='off'` |
| `binscatter` | headless 无图窗→Stata 挂起 | 60s 看门狗 + `StataSO_SetBreak` |
| `winsor2` 选项冲突 | 级联错误→DLL 崩溃 | `_drain_output` 缓冲隔离 + 逐命令错误处理 |
| 高并发调用 | threading.Lock 竞态 | 改用 `threading.Event` |
| `///` 续行被过滤 | 解析器把 `///` 当 `//` 注释跳过 | `_parse_command_blocks` 区隔 `///` vs `//` |
| graph + export 分步失败 | 两次 `StataSO_Execute` 间图形窗口丢失 | `_parse_command_blocks` 支持 `{ }` 复合块原子执行 |
| 中文字符在 Stata 报错 | 单引号 `'` 被 Stata 解释为宏引用 | 改用 `\`"中文\"'` 语法 |
| 复杂 twoway 图形崩溃 | headless 环境多 overlay 图形过载（需再验证） | 推荐使用轻量图形 + 导出 |

## 权限配置

```json
{
  "enableAllProjectMcpServers": true,  // MCP 工具免弹窗
  "permissions": {
    "allow": [
      "WebFetch"  // 不限域名 Web 抓取
    ]
  }
}
```

## 已知局限

- **`setup.py` 的 `test_server` 函数**：使用 `exec()` + 文本替换避开 `main` 块，易因空格变动失效。将来应改用 `importlib` 直接验证工具注册。
- **无 CI/lint 配置**：无 `mypy`、`ruff`、`pre-commit`。server.py 混合中英文标识符。
- **日志仅 stderr**：MCP 传输中断后日志丢失，未配置文件日志处理器。
- **`stata_graph` 的 export 参数依赖临时 .do 文件**：在极低磁盘空间环境下可能失败。
- **`stata_export_excel` 的 results=True 需要先运行过回归模型**：会尝试使用 `esttab` 导出估计结果；若 `esttab` 未安装则回退到原始数据导出。
