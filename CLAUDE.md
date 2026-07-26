# Stata MCP Server

让 Claude Code Agent 通过 MCP Server 直接驱动 Stata，自动完成数据加载、清洗、建模、结果导出全流程。

## 架构

```
stata-mcp/
├── mcp-stata-server/server.py       # MCP 执行层：33 个工具，通过 pystata 调用 Stata DLL
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

## MCP 工具（33 个）

> **能力边界不在工具数上**：`stata_run` 执行任意命令、`stata_help` 查任意命令的
> 官方语法，二者即「全量内置命令支持」。专用工具（回归/面板/IV/生成变量等）是
> 给高频命令加结构化参数与校验的**便利层**，不是能力上限。

| 类别 | 工具 | 只读? | 说明 |
|------|------|:-----:|------|
| 核心执行 | `stata_run`, `stata_run_do_file` | — | 通用命令执行和 do 文件 |
| 数据管理 | `stata_use_dataset`, `stata_save_dataset`, `stata_set_cwd` | — | 读写 .dta、cd |
| 数据生成 | `stata_generate`, `stata_egen` | — | 创建变量（改数据集，非只读） |
| 数据探索 | `stata_describe`, `stata_codebook`, `stata_summarize`, `stata_list`, `stata_tabulate`, `stata_correlate`, `stata_display` | ✓ | 只读探索 |
| 估计 | `stata_regress`, `stata_logistic`, `stata_probit`, `stata_poisson`, `stata_ttest`, `stata_xtreg`, `stata_ivregress` | ✓ | OLS/Logit/Probit/Poisson/t 检验/面板/IV |
| 后估计 | `stata_margins`, `stata_test`, `stata_predict` | ✓* | 边际效应/Wald 检验/预测（`stata_predict` 会创建变量，非只读） |
| 图形 | `stata_graph` | — | 执行图形命令并可选导出文件（destructiveHint=True，可覆盖文件） |
| 导出 | `stata_export_excel` | — | 数据集导出为 .xlsx（replace 默认 False）；回归结果自动转为 CSV |
| 包管理与帮助 | `stata_install_package`, `stata_find_package`, `stata_list_packages`, `stata_help` | — / ✓ | 安装、`net search` 找包、`ado dir` 列包、`stata_help` 查任意命令帮助（只读） |
| 翻页 | `stata_more` | ✓ | 大输出分页浏览（缓存 120K chars） |
| 会话 | `stata_status` | ✓ | 数据集 + 工作目录 + 内存 |
| 心跳 | `stata_ping` | ✓ | 快速检测 Stata DLL 存活状态 |

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `STATA_HOME` | `C:\Program Files\StataNow\StataNow19` | Stata 安装目录。优先级：环境变量 > `setup.py` 自动检测 > 该默认值；手动安装时可在 `.mcp.json` 中覆盖。 |
| `STATA_EDITION` | `mp` | 版本 (mp/se/be) |
| `STATA_ALLOWED_ROOTS` | 未设置 | 路径沙箱白名单，分号分隔（例 `C:/data;D:/projects`）。**未设置时不限制任何绝对路径**，`_is_path_allowed` 直接放行 —— 文档中「沙箱校验」的说法只在配置了此变量时才有实际约束力。 |
| `STATA_ALLOW_UNC` | 未设置（=拒绝） | 设为 `1` 时允许 UNC 网络路径（`\\server\share`），默认拒绝。 |

## 关键设计决策

### pystata 而非 subprocess

- **直接 DLL 调用**：pystata 通过 ctypes 加载 `mp-64.dll`，绕过进程开销
- **真会话持久**：数据在工具调用间保持，无需反复 `use`
- **串行化**：`_stata_lock` 确保所有命令在单线程中顺序执行（Stata DLL 非线程安全）

### RedirectOutput 必要性

每个 `StataSO_Execute` 调用必须包裹在 `RedirectOutput(StataDisplay, StataError)` 中。
否则 Stata 输出直接写入 `sys.stdout`（即 MCP stdio 通道），污染 JSON-RPC 协议 -> 终端崩溃。

### 输出收集（指数退避）

```
落盘前: _materialize_block   — 多行块写入临时 do 文件转为 include（单行原样通过）
执行前: _drain_output(50ms)  — 短排空残留缓冲（指数退避 1→20ms）
执行中: StataSO_Execute       — 同步调用，60s 超时看门狗（长命令可显式传 timeout）
执行后: 快轮询(指数退避 1→20ms) — 收集主体输出，3次空转即退出
        _drain_output()       — 智能清尾：小输出 50ms | 大输出 100ms
        截断 120K chars        — 防止 MCP 缓冲溢出
        自动分页 4K chars       — 大输出自动分页，支持 stata_more 翻页
```

### 超时看门狗

超时看门狗在后台线程中运行，默认 60s（通过 `timeout` 参数可覆盖，上限 1800s）。因 Stata DLL 非线程安全，看门狗调用 `StataSO_SetBreak` 存在极小并发风险；已通过二次确认、break 冷却机制降低触发概率。如需执行包安装、复杂回归或大循环，显式传入 `timeout=120` 或更高。

### 安全护栏

- `_has_dangerous_command_prefix` 逐行做**行首**匹配，拦截 `!`、`shell`、`winexec`、`python:`、`python (`、裸 `python`，以及一切 `mata` 开头的命令。如确需此类操作，请通过操作系统或 Stata 界面直接执行，不要经由 Stata MCP。
- **接受自由文本命令的工具都必须过这层护栏**：目前是 `stata_run` 与 `stata_graph(command=)`。`stata_graph` 曾遗漏该检查，实测 `stata_graph(command='!touch /tmp/x')` 能真实创建文件 —— 该参数会被原样拼进执行串，导出模式下还会进入临时 do 文件。新增此类参数时务必同步加检查。
- **Mata 与内嵌 Python 同等禁止**：Mata 是可执行任意代码的子语言，块内 `_stata("...")` 能调用任意 Stata 命令（含 `!` shell out），`unlink()` / `fopen()` 能直接读写文件，而行首前缀匹配对块内代码完全无效。为策略一致，`mata describe` 这类只读子命令也一并拒绝。
- 结构化参数的校验强度**不一致，且必须如此**：`condition` / `options` 只拒绝换行、回车、空字节、分号（要留出表达式与宏展开的空间）；`varlist` 另外拒绝 `!`、`|`、`&`、反引号、`$`、`/`、`,` 和独立的 `using`。
- **`varlist` 的额外三条不是洁癖，是路径沙箱的一部分**：varlist 会被拼进 `export excel <varlist> using "<已校验路径>"`。实测 `varlist='mpg using /evil/out.xlsx, replace //'` 可构造出 `export excel mpg using /evil/out.xlsx, replace // using "<安全路径>"` —— `//` 把经 `_validate_path` 校验的路径整段注释掉，数据落到攻击者指定位置。`/`、`,`、`using` 在合法 varlist 里都没有用途，直接拒绝。
- `condition` 不需要这层限制：它总是拼在命令中部（`summarize price if <condition>`），而 `!` 只有出现在**行首**才 shell out；实测 `condition='price > 0 | !touch /tmp/x'` 会被 Stata 当变量名解析失败（r(111)），不会执行命令。
- 路径参数会拒绝空字节、双引号、分号、UNC 路径（默认）及越界的相对路径 `..`。`use_dataset`/`run_do_file` 的相对路径在锁内用 Stata cwd 解析并经沙箱权威校验（见「路径安全校验」）。安装源仅允许 `ssc` 或字符受限的 `https://` URL（禁止 `)`、`(`、空白、引号、`;`、`` ` ``、`$`，防止提前闭合 `from()`）。`export excel` 的 `sheet` 名用双引号包裹并拒绝 `"`、换行、回车、空字节、分号；`)` 是**故意允许**的（`sheet("Q1 (2024)")` 是常见写法，值在引号内对 Stata 安全）。`scheme` 用正向白名单（字母、数字、下划线、连字符）—— 黑名单曾漏掉 `,`，而 `set scheme` 支持逗号后的选项。

### Ping 缓存与失效

每次命令执行前的 `_ping_stata()` 心跳检测有 **2 秒缓存**：同一会话的连续快速调用跳过重复 ping，实测将多步分析流程（如 `regress → estat vif → estat hettest`）的总延迟降低 30-50%。

```
命令 1 → ping(实时) → execute → 命令 2 → ping(缓存命中) → execute → 命令 3 → ping(缓存命中) → execute
                              ↑ 2 秒内不再重复 ping
```

**缓存失效**：当 ping 失败或崩溃恢复失败时，缓存立即清零，确保下次调用重新执行心跳而非使用过期缓存。这在 Stata DLL 崩溃后防止连续调用继续命中无响应的 DLL。

### 多行块执行：`StataSO_Execute` 是单命令接口

**这是本项目最容易踩的坑。** `StataSO_Execute` 不接受脚本，只接受一条命令：
换行不是命令分隔符，而被当成同一条命令的续写。实测（Stata 19.5 MP）后果分三级：

| 输入 | 后果 |
|------|------|
| `display 1\ndisplay 2` | 只执行第一条，第二条成为参数 → r(198) |
| `capture noisily {...}` | `code follows on the same line as open brace` |
| `if {...}`、`program define ... end` | **Stata 进入等待输入状态，会话挂死，`SetBreak` 也无法恢复** |

因此 `_materialize_block` 把**多行块写入 Stata 临时 do 文件后 `include` 执行**
（官方 `pystata.stata.run` 对多行输入同样如此）。用 `include` 而非 `do`，
是为了让块内局部宏对后续命令可见，贴近「在命令行逐条敲」的语义。
临时文件由 `sfi.SFIToolkit.getTempFile()` 提供，Stata 在会话结束时自动清理。

**单行仍走 `StataSO_Execute` 快路径**：实测单行约 12ms，include 约 257ms，
相差 20 倍。所以 `_parse_command_blocks` 把多行输入拆成单行分别执行的设计依然
重要 —— 只有无法拆分的块（`{ }`、`end` 配对）才需要落盘。

### `_parse_command_blocks` 解析器

把输入切成「能独立执行的最小单位」，让绝大多数命令走快路径：

1. **`///` 续行符**不被误判为 `//` 注释过滤，合并后是单行 → 快路径
2. **`{ }` 复合块**整块收集（循环、条件、graph+export 原子块）
3. **`end` 配对块**整块收集（`program` / `input` / `mata`）—— 否则首行挂死会话；
   `program` 的 `drop` / `dir` / `list` 子命令不进入定义模式，已排除

解析规则：
- `*` 行首 → 注释跳过
- `//` 行（且不是 `///`）→ 注释跳过
- `///` 行尾 → 续行符，合并到下一行
- `{` 出现但 `}` 不在同行的 → 复合块开始，收集直到 `}` 闭合
- `program` / `input` / `mata` 开头 → 收集直到单独一行 `end`
- **输入结束时块仍未闭合 → 抛 `UnbalancedBlockError`**，不把残缺块送去执行。
  Stata 收到孤立的 `{` 或未配对的 `program` 会进入等待输入状态并**挂死会话**
  （实测 `capture noisily {` 单独一行即可复现，`SetBreak` 救不回）。异常携带
  `blocks`（已完整解析的块）与 `pending`（未闭合块的已累积文本），供安全护栏
  继续检查 —— 危险命令恰是最容易未闭合的一类（`mata:` / `python:` 单独出现
  即开启 end 块），丢弃已知内容会让护栏对最该拦的输入失效。

### 文件存在性检查

`stata_use_dataset` 与 `stata_run_do_file` 在命令执行前对文件做预存在性检查：
- 绝对路径直接检查。
- 相对路径优先使用 **Stata 当前工作目录** 解析（与后续 Stata 执行路径一致），在 `_stata_lock` 保护内完成。
- 若无法获取 Stata cwd，回退到 Python 进程当前目录。

### 路径安全校验（两层）

文件路径参数经两层校验，确保「校验路径 == 执行路径」：
- **入口预检 `_validate_path`**（进锁前，基于 Python cwd）：拒绝空字节、双引号、分号、换行、回车；拒绝 UNC（默认，`STATA_ALLOW_UNC=1` 开启）；相对路径不得超出 Python cwd；沙箱初筛。
- **权威校验 `_resolve_stata_path_locked`**（锁内，基于 Stata cwd）：用 Stata 实际工作目录解析相对路径为绝对路径，再经 `_check_abs_path_safety` 做沙箱 + UNC 权威校验，并把命令中嵌入的 Python-cwd 路径替换为 Stata 绝对路径。此层消除 Python cwd 与 Stata cwd 不一致导致的沙箱绕过（`use_dataset`/`run_do_file` 的 `require_file` 路径）。

## Gotchas

- **第三方绘图包（binscatter 等）在 headless 环境会挂起**：Stata DLL 试图创建 GUI 窗口失败。服务器启动时自动执行 `set graphics off`，且在 `stata_graph` 导出复合块开头也注入 `set graphics off`。建议优先使用原生命令（`twoway scatter`、`histogram`）。
- **图形导出用 `stata_graph(..., export="path.png")`**：它把 graph 与 export 放进同一复合块原子执行。实测图形对象其实**能**跨 `StataSO_Execute` 调用存活（分两步也可成功），但复合块少一次往返且语义更清晰，仍是推荐写法。
- **`graph export` 的 `width()`/`height()` 单位随格式而变**：位图（png/tif/gif）是像素，矢量（pdf/eps/ps/svg/emf/wmf）是英寸且必须落在 0.5–20。对 .pdf 传 `width(800)` 会 r(198)。`stata_graph` 已按扩展名自动处理：矢量格式下超范围的取值被忽略并在返回信息中说明。
- **图形导出成功与否以文件为准，不看返回码**：复合块用 `capture noisily` 包裹，rc 恒为 0。`stata_graph` 比对导出前后的 `st_mtime_ns` 判定是否真的写入 —— 只看「文件存在」会把「replace=False 且文件已存在」误判为成功。
- **图形导出后自动清理**：`graph drop _all` 在复合块外单独执行，确保即使图形命令出错，缓存的图形对象也会被清理。
- **`stata_graph` 与 `stata_export_excel` 的 `replace` 默认值为 `False`**（而非 True），写文件前需确保目标文件不存在或显式传入 `replace=True`。安全性优先于向后兼容。
- **`stata_export_excel(results=True)`** 自动输出为 CSV（不支持 xlsx），若 `estout` 未安装则返回明确错误并提示手动安装（**不自动安装**——见下）。
- **`///` 续行符**：现在已被修复支持（版本 v2+），可在 `stata_run` 中使用 `///` 连接多行长命令。
- **`{ }` 复合块与循环可以直接写**：`forvalues` / `foreach` / `if` 块、`program define ... end` 都能在 `stata_run` 里正常使用（走临时 do 文件，见上）。注意 Stata 语法要求 `{` 之后换行 —— `forvalues i=1/3 { display \`i' }` 写在一行会 r(198)，这是 Stata 本身的规则。
- **`stata_find_package` 走 `net search`（联网，约 1 秒）**：`ssc` 没有 `search` 子命令。只想看某个已知包的详情用 `stata_run("ssc describe <包名>")`；只搜本机帮助用 `stata_run("search <词>, local")`。
- **`winsor2` 的 `suffix(_w)` 不能和 `replace` 一起用**：选项冲突。要么 `suffix(_w)` 创建新变量，要么 `replace` 覆盖原变量。
- **裸 `cd`（不带参数）会切换到 home，不是显示当前目录**：同 Unix shell，它切换并把新目录打印出来，看着像查询实为修改。查当前目录一律用 `display c(pwd)`。`stata_status` 曾因此在标注只读的情况下悄悄重置用户 `set_cwd` 的结果。
- **`stata_list_packages` 用 `ado dir` 而非 `ado describe`**：后者输出每个包的完整文档（实测本机 49516 字符 / 13 页），前者 4330 字符即给出同样的包清单。看单个包详情用 `stata_run("ado describe <包名>")`。
- **`stata_more` 只能翻上次命令的缓存**：之间不能插入其他命令，否则 `_last_output` 被覆盖。
- **`.mcp.json` 有用户路径，已 gitignore**：clone 后必须运行 `setup.py` 生成。
- **Stata display 不支持中文字符串直接传参**：使用 Stata 的 `"中文"'` 引号语法替代单引号 `'中文'`。

## 故障检测与恢复策略

### 心跳检测
使用 `stata_ping()` 在每次工具调用前快速验证 Stata DLL 存活。返回 `pong` 时可用。

### 安全执行链（`_execute_safe`）
每一条命令经过三层保护：
```
_ping_stata()          # 预检：DLL 是否存活（2 次尝试 + SetBreak 恢复）
  → _execute_single()  # 执行：60s 超时看门狗 + RedirectOutput
    → rc==999 检测     # 崩溃后恢复：排空缓冲 + SetBreak + 重 ping
      → RC=997 恢复   # 恢复成功：标记非致命，中止命令链、提示重试（不报「内部崩溃」）
      → RC=998 终止    # 若 Stata 无响应，终止后续命令，返回错误信息
```

| 返回码 | 含义 | 行为 |
|:------:|:-----|:-----|
| 0 | 成功 | 正常返回输出 |
| 3000 | 无实质输出（如 r-class） | 返回 "(命令执行成功，无文本输出)" |
| 198/其他 | Stata 命令语法错误 | 返回 `[返回码: N]` + 错误文本 |
| 997 | 崩溃后已自动恢复 | 非致命：中止命令链、提示「请重试命令」，**不**标记 isError、不报「内部崩溃」 |
| 999 | StataSO_Execute 崩溃（`_execute_single` 原始码） | 经 `_execute_safe` 后不会向上返回：恢复成功转 997，恢复失败转 998 |
| 998 | DLL 无响应 | 立即终止后续命令 + 提示重启 MCP Server |

### MCP 断线处理
当 MCP Server 崩溃时（DLL 崩溃或连接断开），`_execute_safe` 返回 **RC=998** + 明确错误信息，**不会自动执行任何脚本**。Agent 收到错误后应：

1. **分析错误信息**：判断是 DLL 崩溃、超时还是语法错误
2. **调整策略**：简化命令、避免复杂图形、或切换到更轻量的操作
3. **若需恢复**：提示用户重启 Claude Code（`! exit` → 重新启动）

### 错误处理策略

```
Stata 命令执行异常链:
  input_validation (length check, dangerous prefix filter)
    → _parse_command_blocks()   # 解析 /// 续行和 { } 复合块
    → _execute_single()
        → _drain_output()       # 清洁缓冲
        → watchdog (默认 60s)   # threading.Event → StataSO_SetBreak
        → RedirectOutput        # 防 stdout 污染
        → StataSO_Execute       # try/except
        → _drain_output(if break)  # break 后排空错误残渣
        → 输出收集 + 截断
    → 收集各命令结果
    → 若 had_error → 返回 ToolResult(is_error=True) 告知 MCP 客户端
    → 否则返回分页文本 或 原始文本
```

- 命令错误（返回码非 0）→ 返回 `[返回码: N]` + 错误文本，不崩溃，**已标记为 MCP isError=true**
- DLL 崩溃（`StataSO_Execute` 异常）→ 返回 `CRASH` 消息，看门狗触发 `SetBreak`
- MCP Server 崩溃 → Claude Code 自动重连，Stata 重启（数据丢失）
- 输入验证失败 → 返回 `_make_error_result(...)`，同样标记为 isError=true

## 输出大小管理

- `stata_list` 默认仅显示前 10 条
- `stata_list(n=0)` 显示全部（>4K 字符自动分页）
- `stata_more(page=N)` 翻页，`page=0` 显示全部
- **输出硬上限 120K 字符**（Claude Code 约束）：超出部分在**收集时**即被裁掉并附可操作提示，`_last_output` 缓存与 `stata_more(page=0)` 同样受此上限约束 —— 超限的后半段是真的丢了，翻页也找不回来。
  这条上限一度形同虚设：旧实现先整块写入缓冲再判断总长，只能停止继续收集，拦不住已进入缓冲的部分。实测 19980 obs 的 `list` 单次返回 1,270,888 字符，超上限 10.6 倍。
- 大数据集上**不要**用 `stata_list(n=0)`，先 `in_range` 限定观测或改用汇总命令
- 任何时候优先 `summarize` / `tabulate` / `codebook` 而非 `list`

## 工具调用效率

### 批量命令优先
- 每次 `stata_run` 可在 `\n` 后跟多条命令，全部在一个往返中完成
- 推荐：`stata_run("regress mpg weight\nestat vif\nestat hettest")` — 3 条一次往返
- 省的是 **MCP 协议往返**，不是 Stata 执行时间：实测 Stata 侧批量 3 条 34ms、
  独立 3 次 35ms，几乎无差；单条命令在 Stata 内约 12ms。所以不必为了压缩
  Stata 耗时而把不相关的命令硬凑到一起 —— 拆开执行的错误定位更清晰。
- 适用于：本就属于同一步骤的多条命令（加载→清洗→回归→诊断）

### 并行工具调用
- 数据探索类工具（describe、summarize、codebook、tabulate）**互不依赖**
- 应该一次性并行发送，而非逐条等待
- 回归/图形等工具需要前序结果，必须顺序执行

### 图形导出最佳实践
```stata
* ✅ 推荐：使用 stata_graph 的 export 参数（自动包成 { } 复合块，原子执行）
*    注：复合块本身是多行，同样经 _materialize_block 落临时 do 文件后 include，
*    文件在命令执行完立即删除
stata_graph(command="twoway scatter price weight", export="graph.png", scheme="economist")

* ✅ 也支持：在 stata_run 中用 { } 复合块（整块写入临时 do 文件执行）
stata_run("capture noisily {
    set scheme s2color
    twoway scatter price weight
    graph export 'graph.png', replace width(800)
}")

* ⚠️ 分两步实测也能成功（图形对象跨调用存活），但多一次往返，且错误定位更散
stata_run("scatter price weight")
stata_run("graph export graph.png, replace")

* ❌ 矢量格式别传像素尺寸
stata_graph(command="twoway scatter price weight", export="fig.pdf", width=800)
  ← width() 对 pdf 以英寸计（0.5–20），800 会被忽略并提示；要指定就传 width=6
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
| 高并发调用 | Stata DLL 非线程安全，并发调用会竞态崩溃 | 使用 `threading.Lock` 将所有命令串行化，并配合 `SetBreak` 恢复 |
| `///` 续行被过滤 | 解析器把 `///` 当 `//` 注释跳过 | `_parse_command_blocks` 区隔 `///` vs `//` |
| 中文字符在 Stata 报错 | 单引号 `'` 被 Stata 解释为宏引用 | 改用 `\`"中文\"'` 语法 |
| 复杂 twoway 图形崩溃 | headless 环境多 overlay 图形过载（需再验证） | 推荐使用轻量图形 + 导出 |
| `{ }` 复合块全部失败 | `StataSO_Execute` 是单命令接口，多行被拼成一行 → `code follows on the same line as open brace` | `_materialize_block` 把多行块写入临时 do 文件后 `include` |
| `if { }`、`program define` 挂死会话 | 首行使 Stata 进入等待输入状态，`SetBreak` 无法恢复 | 同上；`_parse_command_blocks` 另按 `end` 收集 `program`/`input`/`mata` 块 |
| 图形导出失败被报成功 | 复合块的 `capture noisily` 吞掉错误使 rc 恒为 0 | 改以导出前后 `st_mtime_ns` 判定文件是否真被写入 |
| `.pdf` 导出 r(198) | 矢量格式 `width()` 以英寸计（0.5–20），却收到像素值 800 | `_graph_size_options` 按扩展名区分单位，超范围则忽略并提示 |
| `stata_find_package` 全不可用 | `ssc` 无 `search` 子命令 → r(198) invalid subcommand | 改用 `net search` |
| `stata_export_excel(results=True)` 探测失效 | `capture which estout` 装与不装都返回 rc=0 | 改用裸 `which estout`（装=0，未装=111） |
| `stata_status` 悄悄重置工作目录 | 裸 `cd` 会切到 home，却被当成查询命令使用 | 改用 `display c(pwd)` |
| 无数据时回「执行成功，无文本输出」 | Stata 对空数据集的 summarize 既不报错也无输出 | `_describe_empty_result` 用 `c(N)` 判定并给出载入指引 |
| `stata_graph(command='!…')` 可执行主机命令 | 危险前缀检查只在 `stata_run` 做，`stata_graph` 的自由文本 command 漏检 | `stata_graph` 接入同一护栏 |
| `mata:` 块可绕过全部前缀检查 | 行首匹配对块内 `_stata("!…")` 无效；多行块修复后 mata 从「挂死」变为「可用」 | 与 `python` 同等禁止一切 `mata` 开头命令 |
| 多行块的临时 do 文件累积 | sfi 临时文件仅在会话结束时清理，而 MCP server 长驻（实测 50 块 → 50 文件） | `_cleanup_temp_block` 在 `finally` 中即用即删 |
| `varlist` 可改写导出路径绕过沙箱 | varlist 允许 `/`、`,`、`using`，`//` 能注释掉已校验的目标路径 | `_validate_varlist` 拒绝这三种记号 |
| 导出把上次的陈旧文件报成功 | rc=997 命令未执行，旧文件仍在，仅判断「文件存在」 | `stata_export_excel` 改用 `_file_written_since` 比对 mtime（与 `stata_graph` 一致） |
| 输出上限 120K 形同虚设 | 先整块 `write` 再判断总长，只能停止继续收集（实测单次返回 1,270,888 字符） | 按剩余空间裁剪后再写入，并给出可操作的截断提示 |
| 超时被报成「未指定的错误」 | 看门狗 break 后 Stata 返回通用 rc=1 | 输出末尾显式点明超时秒数与调大 `timeout` 的建议 |
| 注释/续行可绕过全部危险前缀护栏 | 护栏在**原始文本**行首匹配，而执行的是 `_parse_command_blocks` 剥掉 `/* */`、拼接 `///` 之后的文本。实测 `sh/*x*/ell …` 与 `sh///\n ell …` 原文中根本不含 `shell` 一词 | `_validate_command_blocks` 改为对**解析后的执行块**逐块检查 |
| 多块聚合输出无上限 | `MAX_OUTPUT_CHARS` 只作用于单个块，N 个大块 = N×120K（实测 3 条 list → 360,263 字符） | `_run_stata_command` 在聚合后再截断一次 |
| 块内 `///` 破坏块结构 | 续行合并无条件写入 `buffer[-1]`，在块内会把块的上一行与本行拼成一行；`forvalues {` 因此 r(198)，`program define` 更让 `end` 配对失效而**挂死会话** | 仅在 `in_continuation` 为真时才并入上一行；`end` 块内不提前 flush |
| `stata_ping` 崩溃时回「pong」 | 失败路径返回普通字符串且丢弃诊断，`degraded` 只藏在末尾 | 失败改 `isError=true` 并带出原始诊断；同时补上漏写的 `global _last_ping_time`（缓存回写此前是死代码） |
| `fastmcp>=3.0.0` 是假下界 | `fastmcp.tools.base` 在 3.2.0 才出现，实测 3.0.0/3.1.0 均 `ModuleNotFoundError` | 提升到 `>=3.2.0`（pyproject 与 requirements 同步） |
| `setup.py` 在 macOS/Linux 完全不可用 | edition 检测只查 Windows 的 `{edition}-64.dll`，macOS 候选路径还指向 app bundle 内部 | `_edition_artifacts` 按平台给出特征文件；候选路径改为含 `utilities/pystata` 的安装根目录 |
| 重跑 `setup.py` 抹掉其他 MCP 配置 | `generate_mcp_json` 无条件整文件覆盖 | 改为读取后只更新 `stata` 条目，保留其他 server 与自定义 env |
| 未闭合的 `{` / `program` 挂死会话 | 解析器把残缺块原样发出（旧注释称「让 Stata 报语法错」，实测它不报错而是等待输入） | 解析出口抛 `UnbalancedBlockError`；`_precheck_command` 在入口拦下并给可操作提示 |
| `program define … ///` 挂死会话 | `_opens_end_block` 判定的是当前扫描行，而开启行带 `///` 时 `program` 一词落在被合并的上一行；`has_cont` 分支的 `continue` 又绕过了唯一设置 `in_end_block` 的语句 | 改为判定 `buffer[-1]`（续行合并后的完整命令）。与「块内出现 `///`」互为镜像 |
| `title("'90s")` 让 `///` 失效 | 复合引号定界符写反：把 `"'`（Stata 的**结束**符）当成开启符，于是普通字符串里一出现 `"'` 就翻转状态 | 开启符改为 `` `" ``、结束符改为 `"'`，与 Stata 语法一致 |
| 空 `depvar` 静默算错 | `_validate_identifier` 对空值一律放行，`regress("", "weight")` 拼出 `regress  weight`，Stata 把 weight 当因变量跑出**另一个回归**并返回成功 | 加 `required=True`，必填参数拒绝空值 |
| `scheme` 可注入 `set scheme` 的选项 | 黑名单漏掉 `,`，而 `set scheme` 支持 `, permanently` | 改用正向白名单 `_SCHEME_NAME_RE` |

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

- **CI 实际不运行**：`.github/workflows/test.yml` 是 GitHub Actions 格式，而本仓库的 `origin` 是自建 Gitea（`gitea.aliveranme.space`）。除非该 Gitea 启用 Actions 并注册了 runner，推送**不会触发任何检查**。这份 workflow 目前只是「若迁到 GitHub 即可用」的配置，**不能当作质量门禁**。
  - 它描述的内容：ubuntu-latest × py3.10/3.11/3.12，跑 `ruff check .` 加对仓库根 `setup.py` 的单独 lint，再跑 `pytest --cov`（ruff 规则见 `pyproject.toml` 的 `[tool.ruff]`）。
  - 提交前请在本地手动执行：`cd mcp-stata-server && .venv/bin/python -m pytest tests/ -q && .venv/bin/python -m ruff check server.py tests/ && .venv/bin/python -m ruff check --config pyproject.toml ../setup.py`
  - 即便迁到 GitHub，也只覆盖 Linux；本项目主要面向 Windows（`STATA_HOME` 默认即 Windows 路径），Windows 与 macOS 都无覆盖。
  - 测试用 `conftest.abs_path()` 构造平台原生绝对路径，不要硬编码 `C:/` —— POSIX 下 `os.path.isabs("C:/x")` 为假，路径会被当相对路径拼上 cwd。
- **无类型检查**：无 `mypy`、`pre-commit`。server.py 混合中英文标识符。
- **日志写入文件**：server.py 已将日志同时输出到 stderr 和 `mcp-stata-server/logs/stata-mcp.log`，MCP 传输中断后仍可排查。
- **`stata_export_excel` 的 results=True 需要先运行过回归模型**：会用 `esttab` 导出估计结果；执行前先用裸 `which estout` 探测（**不能加 `capture`** —— 那会吞掉错误使 rc 恒为 0，探测形同虚设），**estout 缺失则直接报错**，提示用 `stata_install_package("estout", source="ssc")` 手动安装。**绝不内嵌 `ssc install`** —— headless MCP 环境下 SSC 网络请求会阻塞 `StataSO_Execute`，看门狗 `SetBreak` 无法干净中断网络 I/O，会损坏 DLL 状态导致后续调用全部卡死。包安装请走专用的 `stata_install_package`（用户可控时机、可显式传 timeout）。
- **超时看门狗线程安全**：Stata DLL 不提供官方线程安全的中断机制。看门狗在命令超时时调用 `StataSO_SetBreak`，与执行线程的 `StataSO_Execute` 存在极小并发风险。当前通过串行锁、降低默认超时（60s）、二次确认和连续 break 熔断降低风险，但不能完全保证在高负载下避免状态损坏。建议长命令显式拆分或使用更大的 timeout 参数。
- **工具错误语义**：错误结果（Stata 返回码非 0、输入验证失败、DLL 崩溃）通过 `ToolResult(is_error=True)` 告知 MCP 客户端。成功工具结果仍以普通字符串返回。若使用 `mcp.list_tools` 或类似客户端，需注意区分返回类型。
