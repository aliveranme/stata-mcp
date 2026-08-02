# Stata MCP Server

让 Claude Code Agent 通过 MCP Server 直接驱动 Stata，自动完成数据加载、清洗、建模、结果导出全流程。

## 架构

```
stata-mcp/
├── mcp-stata-server/server.py       # MCP 执行层：75 个工具，通过 pystata 调用 Stata DLL
├── mcp-stata-server/tool_modules/   # 便利工具模块（数据重构/扩展估计/后估计），register() 装配
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

## MCP 工具（75 个）

> **能力边界不在工具数上**：`stata_run` 执行任意命令、`stata_help` 查任意命令的
> 官方语法，二者即「全量内置命令支持」。专用工具（回归/面板/IV/生成变量等）是
> 给高频命令加结构化参数与校验的**便利层**，不是能力上限。

| 类别 | 工具 | 只读? | 说明 |
|------|------|:-----:|------|
| 核心执行 | `stata_run`, `stata_run_do_file` | — | 通用命令执行；`run_do_file` 执行前自动拆出 `ssc install` 单独安装（已装跳过）。`stata_run` 另有 `save_output`：完整输出（不受 120K 裁剪）落盘并登记为文件资源 |
| 数据管理 | `stata_use_dataset`, `stata_import`, `stata_save_dataset`, `stata_set_cwd` | — | 读写 .dta、cd；`stata_import` 覆盖官方 import 族（excel/delimited/sas/spss/dbase/parquet，按扩展名推断）。`save_dataset` 成功后自动登记为资源 |
| 面板/时序 | `stata_xtset` | — | 声明/查询/清除 `xtset`(面板) 与 `tsset`(纯时序) —— 是 `stata_xtreg` 的前提 |
| 示例数据 | `stata_use_example` | — | `sysuse`(本地) / `webuse`(联网) 加载官方示例数据集；`action="list"` 列出可用 |
| 数据生成 | `stata_generate`, `stata_egen` | — | 创建变量（改数据集，非只读）；支持官方 `[type]` 存储类型与 `[if] [in]` |
| 数据重构 | `stata_merge`, `stata_append`, `stata_reshape`, `stata_collapse`, `stata_frame`, `stata_replace`, `stata_drop`, `stata_keep`, `stata_rename`, `stata_recode`, `stata_destring` | — | 横向合并(1:1/m:1/1:m/m:m)、纵向追加(可多文件)、长宽转换、按组聚合、多数据集 frame；后六个是变量级清洗：`replace`/`recode` 覆盖原变量，`drop`/`keep` 支持「删变量」或「删观测」两种形态（二选一），`destring` 强制 `replace` 或 `generate()` 二选一 |
| 数据校验 | `stata_verify` | ✓ | `count`/`assert`/`duplicates`/`isid`/`missing` 五合一（missing 走 `misstable summarize`） |
| 数据探索 | `stata_describe`, `stata_codebook`, `stata_summarize`, `stata_list`, `stata_tabulate`, `stata_correlate`, `stata_display` | ✓ | 只读探索 |
| 估计 | `stata_regress`, `stata_logistic`, `stata_probit`, `stata_poisson`, `stata_ttest`, `stata_xtreg`, `stata_ivregress`, `stata_logit`, `stata_mlogit`, `stata_nbreg`, `stata_qreg`, `stata_mixed` | ✓ | OLS/Logit/Probit/Poisson/t 检验/面板/IV + 扩展族：`logit`（原始系数，`logistic` 是 OR）、`mlogit` 多分类、`nbreg` 负二项、`qreg` 分位（`quantile` 默认 0.5）、`mixed` 多水平（`random` 以 `\|\|` 开头） |
| 后估计 | `stata_margins`, `stata_test`, `stata_predict`, `stata_estat`, `stata_estimates`, `stata_lincom`, `stata_nlcom`, `stata_hausman` | ✓* | 边际效应/Wald 检验/预测（`stata_predict` 会创建变量，非只读）；`stata_estat` 诊断(vif/hettest/ovtest/ic)；`stata_estimates` 存取与并排比较模型；`lincom` 线性组合、`nlcom` 非线性组合（delta 法）、`hausman` 模型比较（需先 `estimates store` 两个模型） |
| 返回值 | `stata_return_list` | ✓ | 一次列出 `r()`/`e()`/`c()` 全部返回值，不必逐个 `display` |
| 图形 | `stata_graph`, `stata_scheme` | — / ✓* | 绘图并可选导出（选项按格式自动适配，见下）；`stata_scheme` 列出/查询/设置主题（`action="set"` 非只读）。`stata_graph` 导出成功后自动登记为资源 |
| 导出 | `stata_export_excel`, `stata_export_delimited`, `stata_etable` | — | 数据集导出为 .xlsx 或 CSV/TSV/自定义分隔符（replace 默认 False）；**回归表导出优先用 `stata_etable`**（官方 `etable`，无第三方依赖，直出 .docx/.xlsx/.pdf/.tex）。`export_excel(results=True)` 是旧路径：依赖第三方 estout 且只能产出 CSV。导出成功即登记为资源 |
| 文件资源回传 | `stata_read_file`, `stata_register_file`, `stata_list_resources` | ✓ / — | 导出工具成功后会**登记输出文件**，远程客户端可经 MCP 资源协议（`resources/read` 读 `stata-file:///<路径>`）或 `stata_read_file`（base64）取回图表/Excel/CSV/dta 的**实际内容**，而不只是路径。安全边界：**只读登记过的文件**，未登记报错并提示登记方式 |
| 包管理与帮助 | `stata_install_package`, `stata_uninstall_package`, `stata_describe_package`, `stata_find_package`, `stata_list_packages`, `stata_help` | — / ✓ | 装/卸（`ado uninstall` 本地安全）/查详情（本地 `ado describe` 或联网 `ssc describe`）/`net search` 找包/`ado dir` 列包/`stata_help` 查任意命令帮助 |
| 会话生命周期 | `stata_clear`, `stata_snapshot` | — | `clear` 按 scope 重置（data/estimates/graphs/panels/all）；`snapshot` 包 Stata 原生快照 save/list/restore/erase，同会话内数据阶段间快速回退 |
| 长任务控制 | `stata_background`, `stata_task_status`, `stata_task_cancel`, `stata_task_result`, `stata_task_list` | — / ✓ | 后台执行长任务（大循环/复杂回归/联网，单块最长 3600s），立即返回任务号；进度轮询、显式取消、取结果。后台任务仍持 `_stata_lock`，运行期间其他调用会等待 |
| 翻页 | `stata_more` | ✓ | 大输出分页浏览（缓存 120K chars） |
| 会话 | `stata_status` | ✓ | 数据集 + 工作目录 + **frame** + **面板/时序设定** + **已存/活跃估计** + 内存 —— 覆盖 Agent 调 `xtreg`/`margins`/`predict` 前需确认的全部前提 |
| 心跳 | `stata_ping` | ✓ | 快速检测 Stata DLL 存活状态 |
| 服务器日志 | `stata_read_log` | ✓ | 读取本 MCP Server 的运行日志（tail/path），排查远程客户端看不到的服务器侧问题 |

### 语法位置对齐（`[varlist] [if] [in] [, options]`）

包装具体命令的工具都应能表达该命令官方语法的每个位置，否则便利层反而成了能力
天花板。统一约定：

- **`condition` → `if`、`in_range` → `in`**，由 `_filter_clause` 拼接，顺序固定为
  `if` 在前、`in` 在后、二者都在**逗号之前**（拼到逗号后 Stata 当未知选项报 r(198)）。
- **`options`** 是长尾官方选项的自由文本逃生舱（经 `_validate_no_injection`）；
  高频选项才给独立参数。
- **例外（并非缺口）**：`test` 作用于已存储的估计结果，官方就不接受 `if`/`in`；
  `display` 没有 `, options` 子句（格式指令写在表达式里）；`describe`/`save` 同样
  没有 `if`/`in`。
- **`stata_list` 的 `in` 由它自己的 `in_range`/`n` 逻辑负责**，只把 `condition`
  交给 `_filter_clause` —— 否则会拼出 `list … in 1/20 in 1/20`。

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `STATA_HOME` | `C:\Program Files\StataNow\StataNow19` | Stata 安装目录。优先级：环境变量 > `setup.py` 自动检测 > 该默认值；手动安装时可在 `.mcp.json` 中覆盖。 |
| `STATA_EDITION` | `mp` | 版本 (mp/se/be) |
| `STATA_ALLOWED_ROOTS` | 未设置 | 路径沙箱白名单，分号分隔（例 `C:/data;D:/projects`）。**两重限制**：① 未设置时不限制任何绝对路径，`_is_path_allowed` 直接放行；② 设置后**既校验工具的路径参数**（`stata_use_dataset("越界路径")` 被拒），**也审计自由文本命令的引号路径**（`stata_run('use "越界路径"')` 同样被拒）—— 审计在 `_run_stata_command` 锁内做权威校验，只审数据命令的引号路径（裸单 token 可能是 varlist 跳过、宏路径 fail-open）。需要强制隔离请在操作系统层面限制本进程可访问的目录。 |
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
        _drain_output()       — 智能清尾三档：干净退出的小输出 5ms
                                | 未干净退出的小输出 50ms | ≥10K 大输出 100ms
        截断 120K chars        — 防止 MCP 缓冲溢出
        自动分页 4K chars       — 大输出自动分页，支持 stata_more 翻页
```

### 超时看门狗

超时看门狗在后台线程中运行，默认 60s（通过 `timeout` 参数可覆盖，钳制在 10–1800s）。因 Stata DLL 非线程安全，看门狗调用 `StataSO_SetBreak` 存在极小并发风险；缓解手段是**锁内二次确认**（`break_guard` 把「确认命令未完成」与「发出 break」合成原子步骤，主线程置位完成事件同样要拿这把锁）。此前本节还写有「break 冷却机制」，代码中从未实现，已删除该说法。如需执行包安装、复杂回归或大循环，显式传入 `timeout=120` 或更高。

### 安全护栏

- `_has_dangerous_command_prefix` 逐行做**行首**匹配，拦截 shell-out / 文件销毁 / 代码执行四族的**全写与最小缩写**：`!`、`sh`/`shell`/`xsh`/`xshell`、`winex`/`winexec`、`unix`/`unixc`/`unixcmd`、`era`/`erase`、`rmd`/`rmdir`、`java`、`plugin`、`python`/`python:`/`python (`、以及一切 `mata` 开头的命令。缩写形态是真实旁路：真机确认 `sh whoami`、`era /tmp/x`（erase，**删文件**）曾原样穿过旧护栏。如确需此类操作，请通过操作系统或 Stata 界面直接执行，不要经由 Stata MCP。
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

### do 文件执行前拆出 `ssc install`

do 文件常在开头写 `ssc install foo`，内联执行会让整段脚本卡在网络请求上（见「SSC 网络」条目）。`stata_run_do_file` 执行前先处理：

1. **best-effort 读文件**（Python 侧，按 `_normalize_path` 的绝对路径）。读不到（如 Stata cwd 与 Python cwd 不一致的相对路径）→ 不拆分，退回 `do "path"` 原样执行，由 `require_file` 锁内权威路径兜底。
2. `_extract_ssc_installs` 扫描**行首** `ssc install <pkg>[, replace]`（含 `qui`/`cap`/`noi` 前缀组合），按包名去重；安装行**改成注释保留行号**。`{ }` 块内（函数维护花括号深度，深度 >0 时不匹配 —— 块内安装是**有条件**执行的，`if _rc != 0 { ssc install foo }` 提到脚本之前就变成了无条件安装）、`ssc describe`/`uninstall`、缩写 `inst` 都不匹配 —— 未命中就随原文内联，安全兜底。
3. `_prepare_ssc_installs` 逐个处理：无 `replace` 时先 `which pkg` 探测，**已装则跳过不重复联网**；缺失才 `ssc install`（带 timeout，超时可被看门狗干净中断）。
4. 清理后的脚本写 **Stata 临时 do 文件**执行（`finally` 中 `_cleanup_temp_block` 即用即删），返回信息前置「预装/跳过报告」。
5. **文件中无 `ssc install` → 完全走原路径，行为逐字不变**（不写临时文件、不加报告头）。

拆分只处理 `ssc install`（`net install` 的第三方 URL 未纳入，避免 URL 注入复杂度）。因预装要经 `_run_stata_command` 自行抢锁，拆分/读取都在锁外做 —— do 文件按约定传绝对路径，Python-cwd 与 Stata-cwd 解析一致；即便相对路径读错文件，最坏是预装了错误的包（无害）或漏拆（退回内联），do 主体执行仍走各自的权威路径。

### 路径安全校验（两层）

文件路径参数经两层校验，确保「校验路径 == 执行路径」：
- **入口预检 `_validate_path`**（进锁前，基于 Python cwd）：拒绝空字节、双引号、分号、换行、回车；拒绝 UNC（默认，`STATA_ALLOW_UNC=1` 开启）；相对路径不得超出 Python cwd；沙箱初筛。
- **权威校验 `_resolve_stata_path_locked`**（锁内，基于 Stata cwd）：用 Stata 实际工作目录解析相对路径为绝对路径，再经 `_check_abs_path_safety` 做沙箱 + UNC 权威校验，并把命令中嵌入的 Python-cwd 路径替换为 Stata 绝对路径。此层消除 Python cwd 与 Stata cwd 不一致导致的沙箱绕过（`use_dataset`/`run_do_file` 的 `require_file` 路径）。

### 文件资源回传（MCP resources）

导出工具（`stata_graph` / `stata_export_excel` / `stata_etable` / `stata_export_delimited` /
`stata_save_dataset` / `stata_run(save_output=)`）在**确认文件真正写入后**调用
`_register_resource` 登记；资源模板 `stata-file:///{path*}` 只服务登记过的文件。

- **为什么是 `{path*}` 而非 `{path}`**：`{path}` 占位符只匹配单个路径段（不含 `/`），
  POSIX/Windows 绝对路径必然含 `/`，会全部匹配失败。`{path*}` 是 RFC 6570 通配符，
  跨段匹配，实测两者行为差异显著。
- **安全边界就是注册表**：不查注册表的话，远程客户端可用任意 URI 请求 `stata-file:///...`，
  变成服务器端任意文件读取原语。`stata_read_file` 与资源模板共用 `_read_registered_file`，
  先查注册表 → 校验大小上限（16MB）→ 才读二进制。
- **读取上限**：单次资源读取钳在 `_MAX_RESOURCE_READ_BYTES`（16MB），防止把超大文件
  一次性读进内存撑爆 MCP 传输。
- **登记 ≠ 复制**：文件留在磁盘原位，登记只是把路径记进注册表（含 mime/size/来源/URI）。
  `stata_clear(scope="all")` 清空注册表但**不删文件**。
- `stata_run(save_output=)` 是配套：超大输出在内存里截断 120K，完整文本写进文件并登记，
  远程客户端可经资源协议取回完整输出。

### 会话生命周期（clear / snapshot）

- **`stata_clear` 不重启 DLL**：pystata 的 shutdown/init 循环有崩溃风险，`clear all` +
  `capture frame drop _all` + `estimates clear` + `graph drop _all` + `xtset, clear` 已覆盖
  可观测的会话状态。scope 细分让 Agent 只清需要的部分，不必整锅端。
- **`stata_snapshot` 包 Stata 原生 snapshot**：同一会话内数据阶段的快速回退。只快照
  内存数据集（估计结果/宏不在其中），是「多会话隔离」的轻量近似 —— 真正隔离需多个
  Stata 实例（受许可证约束，见已知局限）。

### 长任务控制（后台任务）

后台任务**不改变并发性**（Stata DLL 单线程，`_bg_worker` 同样持 `_stata_lock`），改变的是
**交互模型**：提交方立即拿到 task_id，不必在 MCP 请求里阻塞数分钟；进度可轮询、任务可
显式取消，单块超时上限放宽到 3600s。

- **取消语义**：`_bg_cancel` 置位 `cancel_requested`；任务正卡在 `StataSO_Execute` 里
  （`in_execute`）时同时 `_set_break()` —— 与超时看门狗的跨线程 SetBreak 是同一个已接受的
  并发风险。块与块之间靠命令返回后检查取消标志终止，因此**晚到的 break 不会被下一条
  命令消费**（不再复现「打断下一条无辜命令」的历史缺陷）。
- **进度粒度**：以「命令块」为单位（当前块/总块数），do 文件整体是一条命令块，粒度到此为止。

### 实战压力测试发现与修复（2026-08，6 路并行真实 Stata 19.5 实测）

用 Stata 自带数据集（auto/census/bpwide/nlswork/hsng2 等）并行跑了 6 类场景
（数据管理/探索/估计/面板时序/图形导出/后台会话），~240 次真实调用。系统整体
稳定（零 DLL 崩溃/挂死/看门狗误伤），发现并修复以下不足：

| 发现 | 修复 |
|------|------|
| `stata_qreg` docstring 推荐 `vce(bootstrap)`，实测 r(198)（官方正解是 `bsqreg`） | docstring 改为 `vce(iid)`/`vce(robust)`，注明 bootstrap 走 `stata_run("bsqreg ...")` |
| `stata_hausman` docstring 示例用 `fe=True`，实际签名是 `effects="fe"` | docstring 修正 |
| `stata_reshape` 非数值 j（bpwide 的 before/after）两个方向都要 `options="string"`，docstring 未提及；r(498) 未收录 | docstring 注明；r(498) 加入返回码释义表 |
| 估计/后估计工具无法覆盖 60s 看门狗（无 timeout 参数） | 19 个估计工具统一补 `timeout` 参数（钳制 10–1800） |
| `snapshot save` 附加的 `snapshot list` 把创建记录打印两遍 | 去掉附加 list（save 输出已含编号） |
| `stata_background("")` 接受空命令 | 入口拒绝空/纯空白 |
| `stata_clear` 后误报「请先载入数据」 | 清空返回确认文本 |
| `stata_list` 匹配 0 行返回「无文本输出」，与数据缺失无法区分；空数据上 r(198) | 0 匹配给「未列出任何观测」提示；空数据给友好指引 |
| 120K 截断提示被收集层与聚合层各追加一次（双提示）；且只出现在最后一页，首页无从得知 | 聚合层去重；`_paginate` 增 `truncated` 标志，页首提示 |
| 图形导出到不存在目录报「failed to export format」；真实原因（r111）埋在「文件为空」之后；空数据集导出空白图无警告 | 错误先列 Stata 真实原因；目录不存在给出提示 |
| `stata_etable` 已传 replace 仍报 r(602)「文件已存在」（真实根因：目录不存在） | replace + r602 时提示目录可能不存在 |
| 重复登记资源覆盖原始来源 | `_register_resource` 保留首登来源 |
| 后台任务取消丢已执行输出 | 取消分支并入当前块输出，标注「已执行 N/M 块」 |
| 测试运行污染生产日志（pytest 的 traceback 进 logs/stata-mcp.log，`stata_read_log` 读到噪音） | conftest 摘掉文件 handler |
| 看门狗超时文案「看门狗超时会走这里」是调试口吻 | 改为面向用户的中文说明 |
| 后台任务期间主线程 stdout 会被 RedirectOutput 捕获进任务结果（进程级替换；MCP 生产走 stdio 无实际影响） | `stata_background` docstring 明确「任务运行期间不要向进程 stdout 写内容」 |

## Gotchas

- **第三方绘图包（binscatter 等）在 headless 环境会挂起**：Stata DLL 试图创建 GUI 窗口失败。服务器启动时自动执行 `set graphics off`，且在 `stata_graph` 导出复合块开头也注入 `set graphics off`。建议优先使用原生命令（`twoway scatter`、`histogram`）。
- **图形导出用 `stata_graph(..., export="path.png")`**：它把 graph 与 export 放进同一复合块原子执行。实测图形对象其实**能**跨 `StataSO_Execute` 调用存活（分两步也可成功），但复合块少一次往返且语义更清晰，仍是推荐写法。
- **`graph export` 的选项按格式而变，且不是「位图 vs 矢量」二分**。依据 [G-2] `graph export` 的 `override_options` 表与各 [G-3] `*_options` 条目，并在 Stata 19.5 MP（macOS）逐条实测：

  | 格式 | 本环境可用 | `width()`/`height()` | `quality()` | `mag()` | `fontface()` |
  |------|:---:|------|:---:|:---:|:---:|
  | `.png` / `.tif` / `.gif` | 仅 png | **像素**，8–16000 | ✗ | ✗ | ✗ |
  | `.jpg` | ✓ | **像素** | ✓ 1–100 | ✗ | ✗ |
  | `.svg` | ✓ | **像素**（官方写作 `width(#px\|#in)`，无后缀默认 px；输出头为 `width="800px"`） | ✗ | ✗ | ✓ |
  | `.pdf` | ✓ | **英寸**，0.5–20 | ✗ | ✓ 1–10000 | ✓ |
  | `.eps` / `.ps` | ✓ | **不支持**（ps 另有 `pagewidth()`，仅 `pagesize(custom)` 时相关） | ✗ | ✓ | ✓ |
  | `.emf` / `.wmf` | ✗ | **无任何 override_options**（`help emf_options` 不存在；wmf 更不在官方格式表里） | ✗ | ✗ | ✗ |

  不适用的选项传给 Stata 会 r(198)，而复合块的 `capture` 会吞掉错误 → 导出无声失败。故 `stata_graph` 一律**先丢弃再提示**；取值是否越界则交给 Stata 报错（诊断更精确，也不随版本漂移）。
  **格式可用性是运行时属性**：`emf`/`wmf` 仅 Windows、`gif` 仅 Mac GUI、`tif` 不支持 console 模式。本 MCP 以 headless console 运行，实测 `gif`/`emf`/`wmf` 报 `Stata for Unix cannot create ... files`、`tif` 报 `translator Graph2tif not found`。
  `.jpeg` **不是**官方后缀（实测 `translator Graph2jpeg not found`），不要当 `.jpg` 处理。

- **`stata_graph` 默认不改动 scheme**：`scheme` 留空时不发 `set scheme`。Stata 19 的默认是 `stcolor`（实测 `c(scheme)`），旧实现硬编码 `s2color` 且每次调用都设置、结束不还原，等于静默覆盖用户主题。查询/切换主题用 `stata_scheme`。

- **`export excel` 的 `sheet(..., replace)` 与文件级 `replace` 互斥**：实测 `invalid syntax; option sheet(...,replace) may not be combined with option replace`。语义上也确实冲突 —— 文件级 replace 重建整个文件，不可能有工作表冲突。`stata_export_excel` 在入口拦下并说明二选一。

- **导出命令对「筛选后 0 条观测」报的是 Excel 行数上限**：`observations must be between 1 and 1048576` —— 下界是 1，所以 0 条也越界，诊断与真实原因毫无关系。实测 `if foreign == 1 in 1/10`（auto 前 10 条全为国产车）即可复现。`_empty_selection_hint` 在传了筛选条件时把它翻译成「筛选未匹配到任何观测」，并点明 `if` + `in` 是「**前 n 条观测里**满足条件的」。

- **`export delimited` 的 `delimiter(tab)` 与 `delimiter("tab")` 等价**：实测都产出制表符，Stata 不会把 `"tab"` 当三字符分隔符。代码取官方文档的无引号写法。
- **图形导出成功与否以文件为准，不看返回码**：复合块用 `capture noisily` 包裹，rc 恒为 0。`stata_graph` 比对导出前后的 `st_mtime_ns` 判定是否真的写入 —— 只看「文件存在」会把「replace=False 且文件已存在」误判为成功。
- **图形导出后只清匿名图**：`graph drop Graph` 在复合块外单独执行（放块外是为了即使绘图命令出错也能清理）。**不能用 `_all`** —— 具名图正是「我要在后续命令里引用它」的显式表达，`_all` 会把它们一起摧毁，于是多面板工作流第二次导出必然失败：
  ```
  stata_graph("scatter price weight, name(g1)")
  stata_graph("scatter price mpg, name(g2)")
  stata_graph("graph combine g1 g2", export="a.png")        # _all 时此处清空 g1/g2
  stata_graph("graph combine g1 g2, cols(1)", export="b.png")  # 源图已不存在
  ```
  真机确认（Stata 19.5 MP）：匿名图名为 `Graph`（`graph combine` 的结果同样叫 `Graph`），`graph drop Graph` 只删它、具名图存活且 rc=0；修复后上面四步全部成功，`graph dir` 仍列出 `g1 g2`。匿名图不会累积 —— 每次绘图都覆盖同名的那一个。具名图需要手动清理时用 `stata_run("graph drop _all")`。
- **`stata_graph` 与 `stata_export_excel` 的 `replace` 默认值为 `False`**（而非 True），写文件前需确保目标文件不存在或显式传入 `replace=True`。安全性优先于向后兼容。
- **`stata_export_excel(results=True)`** 自动输出为 CSV（不支持 xlsx），若 `estout` 未安装则返回明确错误并提示手动安装（**不自动安装**——见下）。
- **`///` 续行符**：现在已被修复支持（版本 v2+），可在 `stata_run` 中使用 `///` 连接多行长命令。
- **`{ }` 复合块与循环可以直接写**：`forvalues` / `foreach` / `if` 块、`program define ... end` 都能在 `stata_run` 里正常使用（走临时 do 文件，见上）。注意 Stata 语法要求 `{` 之后换行 —— `forvalues i=1/3 { display \`i' }` 写在一行会 r(198)，这是 Stata 本身的规则。
- **`stata_find_package` 走 `net search`（联网，实测 0.6–2 秒）**：`ssc` 没有 `search` 子命令。只搜本机帮助用 `stata_run("search <词>, local")`。
  - **宽泛的多词查询输出极大**：实测 `net search difference in differences` 默认返回 94,236 字符（24 页）。用 `scope="toc"` 收窄到 12,160 字符（约 1/8），`pkg` / `nosj` 只能减到 8 万量级。
  - **`match_any`（官方 `or`）显著变慢**：同一查询默认 2.3s，加 `or` 后 **29.9s**（13 倍）。默认的「全部关键词都要命中」既快又准。
  - **无匹配默认不算错误**：返回 `no matches` 且 `isError=False`。需要程序化判定时传 `error_if_none=True`（官方 `errnone`，转 rc=111）。
- **包生命周期已补全 `uninstall` / `describe`**：`stata_uninstall_package` 是 `ado uninstall`（**纯本地**删文件，实测约 20ms，无网络阻塞，与 `install` 对称）。`stata_describe_package` 默认 `source="installed"` 走本地 `ado describe`（约 12ms）；`source="ssc"` 走联网 `ssc describe`（约 1–7s，装前查详情用）—— 网络路径与 `install` 同属**网络阻塞**操作（阻塞串行锁、看门狗对其无效，见下方 estout 条目的实测澄清），故独立成工具、用户可控时机。`update` 由 `install` 的 `replace=True` 覆盖（`ssc install pkg, replace` 即重装最新）。
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
| `.svg` 导出静默产出废图 | 上一条的「矢量=英寸」二分是错的：svg 的 `width()` 实为**像素**。合法的 `width=800` 被当越界丢弃；而 `width=6` 被当英寸放行，产出 6 像素的图且文件写出成功、工具回报「已导出 5.1 KB」 | 拆成三类：`_INCH_GRAPH_EXTS`（pdf/emf/wmf）、`_NO_SIZE_GRAPH_EXTS`（eps/ps）、其余（位图与 svg）按像素原样下传 |
| `.eps`/`.ps` 传合法英寸值仍导出失败 | 二者**完全不支持** `width()`/`height()`，传任何值都是 `option width() not allowed` → r(198)，错误又被复合块的 `capture` 吞掉 | 归入 `_NO_SIZE_GRAPH_EXTS`，丢弃全部尺寸选项并提示 |
| E2E 测试失败被自动化当成通过 | `pystata.config.init()` 之后进程退出码恒为 0（实测连 `sys.exit(3)` 都返回 0，Stata 运行时接管了解释器退出路径） | `tests_e2e/conftest.py` 在 `pytest_unconfigure` 里用 `os._exit(exitstatus)` 收口（挂 `pytest_sessionfinish` 会截掉终端汇总） |
| 每次绘图都静默改掉用户主题 | `stata_graph` 的 `scheme` 硬编码默认 `"s2color"`，每次调用都 `set scheme s2color` 且不还原；而 Stata 19 的默认是 `stcolor` | 默认改为空 = 不发 `set scheme`；主题的查询与切换独立成 `stata_scheme` |
| `.emf`/`.wmf` 被当英寸格式 | 「矢量=英寸」的二分把二者也算了进去，实际 `help emf_options` **不存在**、wmf 更不在官方格式表里，都不接受任何 override option | 移入 `_NO_SIZE_GRAPH_EXTS`；`_INCH_GRAPH_EXTS` 收窄为仅 `.pdf` |
| `quality`/`mag`/`fontface` 无法使用 | 官方 `*_options` 里的这些选项从未暴露，用户只能绕道 `stata_run` 手写 `graph export` | 新增 `_graph_format_options`，按格式适用性下传或丢弃并提示 |
| `export excel` 撞工作表时无路可走 | 官方解法是 `sheet(..., modify\|replace)`，而工具只暴露了 sheet 名；且该选项与文件级 `replace` 互斥（r(198)） | 新增 `sheet_mode` 参数并在入口拦下互斥组合、说明二选一 |
| 筛选落空被报成「Excel 行数超限」 | `export excel`/`export delimited` 对 0 条观测报 `observations must be between 1 and 1048576`，与真实原因无关 | `_empty_selection_hint` 在传了筛选条件时翻译成「未匹配到任何观测」，并点明 `if`+`in` 的语义 |
| 官方 8 种导出方法只覆盖 1 种 | 仅有 `export excel`，最高频的 `export delimited`（CSV/TSV）完全缺失 | 新增 `stata_export_delimited`，含 `delimiter`/`novarnames`/`nolabel`/`datafmt`/`quote` 与 `if`-`in` |
| `stata_ttest` 一直在发非法命令 | 不传 `byvar` 时拼出裸 `ttest price`，实测 **r(100) by() option required**。单元测试只比对字符串（`cmd == "ttest price, level(90)"`）照样全绿 —— 只有真机 E2E 才暴露 | 新增 `compare_to` 参数覆盖官方四种形式（单样本 `== #` / 按组 `by()` / 配对 `== v2` / 非配对 `, unpaired`）；两者都不给时在入口拦下并说明 |
| `[in]` 观测范围在 14 个工具里无法表达 | 官方语法普遍是 `cmd … [if] [in] [, options]`，而只有 `stata_list` 有 `in_range` | 抽出 `_filter_clause(condition, in_range)` 统一拼接（`if` 在前、`in` 在后、都在逗号之前），并给 14 个工具补上参数 |
| `describe` 的 `simple` 会丢弃 varlist | 旧实现 `simple=True` 时直接输出 `describe, simple`，用户请求两个变量却拿到**全部**变量清单；官方语法 `describe [varlist] [, options]` 两者本可共存 | 改为 `describe price mpg, simple` |
| 长尾官方选项无出口 | `test`/`generate`/`egen`/`use`/`save`/`list`/`tabulate`/`summarize`/`codebook`/`describe` 都没有 `options` 逃生舱，用户只能绕道 `stata_run` | 统一补 `options` 自由文本参数（经 `_validate_no_injection`）；`generate`/`egen` 另补官方的 `[type]` 存储类型位置（白名单校验） |
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
| 「SSC 网络请求损坏 DLL」被误诊 | 早先据「用户报告卡死了」记为 DLL 损坏，但从未复现。实测（Stata 19.5 MP，macOS）`ssc install` 耗时 3–13s 波动、慢网络更久，整段独占 `_stata_lock`——表现为「卡住」但**不损坏 DLL**：装超过 timeout 时看门狗 `SetBreak` 会干净中断（rdrobust 在 10s 被 break，会话健康、包不残留），网络正常时干净完成，多场景复现无一崩溃 | 无代码改动：`stata_install_package` 独立成工具的设计依旧正确（隔离长阻塞的网络操作），并加 `timeout` 参数供用户兜底；仅把文档中「损坏 DLL / 卡死」改正为「网络阻塞太久」 |
| 通用前缀绕过危险命令护栏 | `_has_dangerous_command_prefix` 只做**行首**匹配，而 `capture`/`quietly`/`noisily`/`by g:` 是可套在任意命令前的通用前缀。真机确认 `capture shell touch <f>` 与 `quietly mata: _stata("shell touch <f>")` 真实创建了文件 | 新增 `_strip_command_prefixes` 归一化出「真正被执行的命令」，原文与剥离后各判一次 |
| `#delimit ;` 绕过护栏并让脚本崩解 | 它把命令分隔符从换行改成 `;`，此后 `!` 永不在行首。真机确认 `#delimit ;` + `display 3 ; !touch <f> ;` 真实创建文件；同时行导向的解析器会把这类脚本切成碎块 | 护栏先按**顶层分号**切分再逐段匹配（字符串内的 `;` 不切）；入口另行拒绝 `#delimit` 并指向 `stata_run_do_file` |
| `stata_import` 的 `condition`/`in_range` 可绕过路径沙箱 | 它是唯一把 `[if]`/`[in]` 拼在 `using "<已校验路径>"` **之前**的工具，而两个子句只过 `_validate_no_injection`（不拒 `/`、`"`、`using`）。实测 `condition='1 using "<越界>" //'` 用 `//` 注释掉沙箱路径读取任意文件 | 新增 `_validate_filter_expr`（`_validate_no_injection` 的超集，只在**字符串之外**拒绝 `//`、`/*`、`*/`、独立 `using` 与未闭合引号），接入全部 36 处入口而非只补该工具 |
| 缩进的 `*` 注释被当代码扫描 | 解析器只认第 1 列（注释还写着「必须位于第 1 列」），真机反证顶层与循环体内的缩进注释都合法。于是注释里的 `{` 让合法循环抛 `UnbalancedBlockError`、`}` 把块提前切碎 | 注释判定移到**逻辑行**层面并允许缩进 |
| `* 注释 ///` 反而执行了被注释掉的命令 | Stata 中 `///` 会把下一行并入注释（真机确认不输出），而解析器在 `*` 处提前 return、`has_cont` 恒为 False，下一行被当独立命令执行 | 新增 `in_comment_line` 状态跟踪注释的续行链；同时保证 `display 1 ///` + `* 2` 里的 `*` 仍是乘号（真机输出 2） |
| `/*` 换行 `*/` 被解析成两条命令 | 它是官方**行连接符**（`///` 出现前的写法），解析器却 `buffer.append` 成新行。实测 `regress price weight /*\n*/ mpg foreign` 被劈开后只跑一个回归元，给出**另一个模型**并「成功」 | 跨行块注释结束后的内容并入上一行（原样拼接，`e(cmdline)` 与原生逐字一致） |
| `quietly program define` 等前缀写法挂死会话 | `_opens_end_block` 只看首 token，前缀让 `program`/`input`/`mata` 块判定失效，开启行被单独送执行 | 复用 `_strip_command_prefixes` 后再判定 |
| `ssc install` 探测不持锁访问 DLL | `_prepare_ssc_installs` 的 `which` 探测在锁外直接调 `_execute_safe`，违反串行化不变式（对照 estout 探测显式加锁）；探测返回 997/998 时还会当成「未安装」继续逐个联网安装 | 探测包进 `with _stata_lock`；997/998 时中止并报告 |
| 普通错误不中止命令链，后续块覆盖磁盘数据 | 只有 997/998 会 break，r(601)/r(198) 与看门狗超时（rc=1）都继续执行。`use` 失败后的 `collapse` 在**上一个**内存数据集上聚合、`save … , replace` 把错误数据落盘 | 首错即停（对齐 Stata 自身的 do 文件语义），并提示 `capture` 是原生的继续执行方式 |
| GBK/Big5 的 do 文件抛未捕获异常 | `stata_run_do_file` 只 `except OSError`，而 `UnicodeDecodeError` 继承自 `ValueError` | 一并捕获，退回 `do "path"` 原样执行 |
| 返回码释义大半是错的 | `STATA_RC_MESSAGES` 像是凭印象填的：rc 9 标成「变量类型不匹配」（真值是 assert 失败，type mismatch 其实是 rc **109**）、rc 4 标成「内存不足」（真值是数据未保存）、rc 5 标成「变量不存在」（真值是 not sorted）、rc 199 标成「选项语法错误」（真值是命令不存在），459/601/2000 等高频码缺失。该文本拼在 Stata 原文**之前**，是 Agent 首先读到的一行 | 逐条真机触发核对后重写全表，补 100/109/133/301/459/601/602/603/900/2000；未核对的码删除，退化为「未知返回码(N)」+ Stata 原文 |
| 含空格的路径在 merge/append 里必然失败 | `_split_using_paths` 按空白切分以支持多文件，于是 `/Users/x/My Drive/…` 被劈成两半；append 的第二个碎片还会按 Python cwd 解析成另一个真实路径 | `merge` 改为 `single=True` 不切分；`append` 改为双引号感知切分（`"a b.dta" "c.dta"`） |
| `stata_verify` 标只读却能删数据 | `duplicates` 分支把 `options` 原样当子命令，`drop` 删除观测、`tag()` 创建变量，而遵循 MCP 注解的客户端会对只读工具跳过确认 | 拒绝这两个子命令并指向 `stata_run`（工具名即契约：校验就该是只读的） |
| `stata_import` 的 sheet/cellrange/encoding/varnames 可逃逸括号 | 与 `stata_export_excel` 的 `sheet`（走 `_validate_sheet_name` 明确拒 `"`）不对称，import 侧混在 `_validate_no_injection` 里，`S1") cellrange(A1:A1) //` 可注入任意选项 | sheet 改走 `_validate_sheet_name`；cellrange/varnames 用正向白名单；encoding 拒引号与括号 |
| `stata_install_package` 的 timeout 无钳制 | docstring 与 CLAUDE.md 都称 10–1800s，代码却直接透传：`timeout=1` 会架起 1 秒看门狗（安装实测需 3–13s，必然被 break） | 与 `stata_run` 一致钳制为 `max(10, min(timeout, 1800))` |
| `{ }` 块内的 `ssc install` 被无条件预装 | docstring 与 CLAUDE.md 都声称块内不匹配，代码却无块跟踪。`if _rc != 0 { ssc install foo }` 被提到脚本之前变成无条件安装 | `_extract_ssc_installs` 维护花括号深度，深度 >0 时不匹配（数错则退回内联，安全兜底） |
| 重跑 `setup.py` 抹掉 stata 条目上的自定义键 | 上一轮只搬运了 `env`，`servers["stata"] = {...}` 仍整条重建，用户为适配 MCP 客户端手加的 `type`/`cwd` 每次重跑都消失且无提示 | 改为在既有条目上就地更新本脚本负责的 `command`/`args`/`env` |
| 写 `.mcp.json` 中途失败会毁掉用户配置 | 唯一写入路径是 `open(path, "w")` 截断后 `json.dump`，磁盘满/Ctrl-C 会留下空文件或半截 JSON；而下次重跑时读取侧的备份逻辑会把残骸备份走并只重建 stata 条目 —— 原始数据永久丢失，备份反而误导 | 改为同目录临时文件 + `os.replace` 原子替换；失败时删临时文件、报「原文件未改动」并让 `main()` 返回 1 |
| 合法 JSON 但顶层非 dict 时静默丢弃用户数据 | 备份保护只挂在 `except (OSError, JSONDecodeError)` 上；`json.load` 成功而 `isinstance(loaded, dict)` 为假（`[]`/`null`/字符串）时既不备份也不提示 | 抽出 `_backup_mcp_json`，非 dict 顶层与非 dict `mcpServers` 都走同一备份路径 |
| 安装的是裸 `fastmcp`，绕开 `>=3.2.0` 下界 | `server.py` 从 `fastmcp.tools.base` 导入 `ToolResult`（3.2.0 才有），而唯一的自动安装路径不引用 requirements.txt：venv 里若已有更低版本，uv/pip 报 already-satisfied rc=0，安装步骤打印 ✓ 成功，直到 Step 4 才以截尾 stderr 暴露 ModuleNotFoundError | 提取 `FASTMCP_SPEC = "fastmcp>=3.2.0"` 供两条安装路径共用，并加测试守住它与 requirements/pyproject 一致 |
| 慢网络下 `setup.py` 裸 traceback 退出 | 五处 `subprocess.run` 都传了 timeout 却无一捕获 `TimeoutExpired`（它继承自 Exception 而非 OSError） | `install_deps` 捕获并给出重试与手动安装命令；uv 路径超时改走 pip 回退 |
| `STATA_HOME` 无效时被静默忽略 | 环境变量是文档声明的最高优先级，目录不存在（外置卷未挂载、路径笔误）时直接落入自动检测，可能把**另一套** Stata 写进 `.mcp.json` | 加黄色警告指明被忽略的路径，再继续自动检测 |
| 导出图形会摧毁多面板工作流 | 复合块后无条件 `graph drop _all`。具名图正是「后续要引用它」的显式表达，于是 `graph combine g1 g2` 导出一张后，换个布局导出第二张时源图已不存在。真机确认匿名图名为 `Graph`，`graph drop Graph` 只删它、具名图存活 | 改 drop 目标为 `Graph`；真机复验四步工作流全部成功且 `graph dir` 仍列出 g1 g2 |
| 一行 `ssc install` 让 do 文件的错误报告不可读 | 失败时走 `_result_text_inline`（换行变 `" | "`），而该函数是为并入单行**报告条目**设计的，套在可达 120K 的完整输出上会把错误上下文、表格、行号压成一条巨型单行；同一 do 文件不含 `ssc install` 时走原路径、格式完好 | 抽出保留换行的 `_result_text`，失败路径改用它；`_result_text_inline` 的 docstring 写明只可用于单行条目 |
| 回归表导出被第三方包与 CSV 锁死 | 唯一路径 `stata_export_excel(results=True)` 依赖第三方 `estout`，名叫 excel 却只能产出 CSV；而 Stata 17+ 自带的 `etable` 无需任何第三方包，直出 .docx/.xlsx/.pdf/.tex | 新增 `stata_etable`。导出格式经真机逐一实测（9 种可用，`.csv`/`.rtf` 报 r(198)），不支持的格式在入口拦下 —— `etable` 会先打印表格再报错，只看输出会把失败当成功；成败以文件 mtime 判定（与 `stata_graph` 同思路） |
| 晚到的 break 打断下一条无辜命令 | 看门狗的二次确认（`if exec_done.is_set(): return` 紧跟 `_set_break()`）留有窗口，而主线程要走完 `RedirectOutput.__exit__` 与临时文件清理（多行块含一次磁盘 unlink）才置位事件。break 不会被任何代码消费，而是被**下一次** `StataSO_Execute` 吃掉 → 无关命令报 rc=1 | 命令一返回就立即置位；置位与「确认+break」共用 `break_guard` 锁，合成原子步骤。另把 `did_break = True` 移到 `_set_break()` **之前** —— 此前主线程可能读到中间态，既不清 break 残渣也不追加超时说明 |
| `stata_read_file(action="read")` 可回传 ~21MB base64 | `_MAX_RESOURCE_READ_BYTES=16MB` 只约束读入内存的文件字节，base64 编码后再膨胀 4/3，直接作为工具结果返回，绕过代码库 120K 输出上限，可能撑爆 stdio 传输 | 新增 `_MAX_TOOL_READ_BYTES=80KB`（base64 后约 106K 贴近传输上限），超限报错并引导用资源协议 `resources/read`（流式二进制，上限仍 16MB） |
| 资源读取的 TOCTOU / 符号链接换靶 | size 复查与 `open().read()` 之间文件被替换/增长时，无长度参数会整段读入超限内容；登记与读取之间不校验 inode | `_read_registered_file` 改**有界读取**（`read(_MAX_RESOURCE_READ_BYTES + 1)`），超限即拒；即便换靶也不会一次性持有超限内容 |
| 文件名含字面 `%xx` 的资源读不回 | fastmcp 已对模板 `{path*}` 解码一次，`_resource_lookup` 再 unquote 一次 → `a%20b.csv` 二次解码成 `a b.csv` 查表 miss（功能性拒绝） | 移除 `_resource_lookup` 的二次 unquote；模板路径由 fastmcp 解码，工具路径本就是文件系统路径 |
| `stata_run(save_output=)` 早退时登记陈旧文件 | 空命令/超长命令在 `_run_stata_command` 内早退（不截断文件），`stata_run` 仍把上一次留下的旧文件登记并附「完整输出已保存」 | 记录调用前 mtime，`_file_written_since` 判定未写入则不登记、不附误导说明（与导出工具同一判定思路） |
| 取消的晚到 break 会打断后续无关命令 | `_bg_cancel` 直接跨线程 `_set_break()`，若命令恰在置位后完成，break 被下一次 `StataSO_Execute` 消费 → 无关命令 rc=1 | 取消改由看门狗处理：`_execute_single` 新增 `cancel_event`，看门狗以与超时相同的**锁内二次确认**发出 break —— break 只在 exec_done 未置位时发出，永远不晚到 |
| `replace`/`recode` 的自由文本可注释掉保护性 `if/in` | `expression`/`values` 只过 `_validate_no_injection`（放行 `//`），而它们拼在 `if`/`in` 之前 —— `//` 把尾部子句整段注释，破坏性命令静默作用于全部观测 | 改用 `_validate_filter_expr`（拒字符串外的 `//`/`/*`/`*/`/独立 using/未闭合引号），与 `stata_import` 的筛选子句同一层级 |
| 多变量 `recode` 用裸规则拼出非法命令 | 官方仅单变量（且不定义值标签）可省略括号；`recode price mpg nonmiss=1` 是非法语法 | 入口校验：varlist 含多变量（空格/范围 `-`）且 values 无括号 → 报错引导写 `(1=0) (2/4=1)` 括号组 |
| `destring` 同时给 `replace` 与 `generate()` | 二者互斥（Stata 报 r(198)），错误留给 Stata 报 | 入口拦下并说明二选一（与 `export excel` 的 sheet_mode/replace 同思路） |
| `rename` 批量形式不配对 | 一个带 `(a b)` 一个不带拼出 `rename (a b c) x` 非法命令 | 入口校验 oldname/newname 要么都带括号要么都不带 |
| `mixed` 的 `[if]` 拼在 `||` 之后 | 官方语法 `[if]`/`[in]` 属于固定效应方程、在随机效应 `||` **之前**；拼在之后虽实测可用但语义偏移 | 改为 `filter_clause` 先于 `random` 拼接 |
| `mlogit` 的 `baseoutcome` 拒绝 0 | 正则 `^[1-9]\d*$` 拒绝 0 —— 而 0/1/2 编码是最常见的分类写法 | 放宽为 `^(0|[1-9]\d*)$`（允许 0、仍拒前导零与非数字） |
| 危险命令缩写穿透护栏 | `_DANGEROUS_COMMAND_PREFIXES` 只有 `!`/`shell`/`winexec`/`python:`/`python(`，而 Stata 允许最小缩写 —— 真机确认 `sh whoami`（shell）、`era /tmp/x`（erase，**删文件**）、`unixcmd ls`、`rmdir` 都能原样穿过 | 扩展覆盖四族全写与缩写：shell-out（sh/shell/xsh/xshell/winex/winexec/unix/unixc/unixcmd）、销毁（era/erase/rmd/rmdir）、代码执行（java/plugin/python）；`capture sh`/`by g: sh` 等前缀藏匿形态同步拦截 |
| 自由文本命令绕过路径沙箱 | 配置 `STATA_ALLOWED_ROOTS` 后 `stata_run('use "越界路径"')` 照常执行 —— 审计只覆盖结构化工具的路径参数（CLAUDE.md 文档化缺口） | `_audit_block_paths`：锁内对数据命令的**引号路径**（use/save/import/merge/graph export/do/run/include/cd…）做权威沙箱校验，越界即拒；只审引号路径避免 varlist 误伤、宏路径 fail-open、未配置白名单时不启用（向后兼容） |
| `merge 1:1` 被前缀剥离器吞掉命令名 | `_strip_command_prefixes` 对未知冒号形态一律剥离（护栏「多剥更严格」的刻意设计），`merge 1:1 price using "f"` 的 `1:1` 匹配规格被当冒号前缀剥掉，命令名丢失 | 审计改用 `_light_strip_prefixes`：只剥已知前缀（capture/quietly/by/version/svy/xi），保留命令身份 |
| 宏间接调用绕过全部前缀护栏 | `local c "shell whoami"` 后 `` `c' whoami `` 展开成 `shell whoami` —— 真机确认旧护栏放行且主机 shell 真实执行（输出 MACRO_EXECUTED） | `_flag_macro_obfuscation`：扫描 local/global 字面量定义，值为危险命令的宏名 + 命令位（行首剥前缀后）引用即拒；只查命令位引用，`display "\`c'"` 字符串内引用不误伤 |
| do 文件的第三方包安装不可控 | `net install`（任意 from() URL）/`github install`/`adoupdate`/`update all` 在 do 文件里原样执行，不经过受控预装路径与来源白名单 | `_flag_unmanaged_package_commands`：do 文件含这些命令即整体拒绝并引导改用 `stata_install_package`（ssc 或完整 https from() URL，带 timeout）；`ssc install` 仍走受控预装 |
| do 文件/长命令输出含噪声计数行 | `(22 real changes made)` 等统计计数行每条命令打一行，挤占 token；连续空行同样浪费 | `compact=True`（stata_run / stata_run_do_file opt-in）：`_compact_output` 删计数行 + 折叠空行，结果表与错误文本绝不动（真机验证） |

### 第二轮对抗性审查（2026-08，执行核心/安全纵深/模块一致性/性能/测试质量 5 维）

35 agent 并行审查 + 对抗性证伪，29 条发现全部确认并修复：

**安全纵深（堵剩余绕过面）**：
- do 文件内容护栏从原始文本升级为**解析后块**检查（挡 `sh/*x*/ell` 注释混淆）；
  且 do 文件内容里的数据路径同样受沙箱审计（配置白名单时，锁内逐行）
- 宏混淆防御补 `=` 形式（`local c = "shell …"`）与复合引号（`` `"shell …"' ``）；
  跨调用 def/use 是护栏固有边界（无法跟踪会话状态），已文档化
- 后台任务的自由文本同样走路径审计（此前完全跳过）
- 路径审计命令集补官方缩写（`sav`/`imp`/`cop`/`exp`/`lo`…），精确 token 不误伤
- 包管理拦截补缩写（`net ins`/`github ins`）与 `version 15:` 前缀
- `#d`（#delimit 缩写）纳入 delimit 拦截

**执行核心正确性**：
- 截断去重修硬上限：旧 `full[:idx]+notice` 在截断块非首个时超 120K（实测 170K）；
  且把提示后的看门狗超时指引整条丢弃 —— 现收口到 120K 并保留尾部关键信息
- 被取消的后台任务不再收到伪造的「超时、调大 timeout」建议
- 后台任务错误分支补「已跳过后续 N 条命令」提示（与前台一致）
- 错误路径（had_error）前置截断提示
- 移除死字段 `_BackgroundTask.in_execute`

**模块一致性**：`export_delimited` 的筛选子句升到 `_validate_filter_expr`（此前弱校验可被 `//` 注释掉路径）；16 个长命令工具（merge/append/reshape/collapse/import/export/graph 等）补 `timeout` 参数（对齐 19 个估计工具）；全角冒号统一；verify/import 文档对齐；scheme 补 destructiveHint；tabulate 去冗余预检。

**测试质量**：conftest 补 `_ALLOWED_ROOTS_CACHE` 重置；新增 20+ 回归测试（timeout 钳制、分页截断标志、do 文件混淆/宏/路径审计、后台审计、graph 目录提示等）。

## 权限配置

`.claude/settings.json` 的实际内容：

```json
{
  "permissions": {
    "allow": [
      "WebFetch"  // 不限域名 Web 抓取
    ]
  },
  "enabledPlugins": {
    "skill-creator@claude-plugins-official": true
  }
}
```

**MCP 工具仍会逐次弹窗**：本文件此前记有 `"enableAllProjectMcpServers": true`，
但实际配置里没有这个键。要免弹窗需自行加上（或在 Claude Code 里对 `stata`
server 选择「始终允许」）。

## 已知局限

- **CI 实际不运行**：`.github/workflows/test.yml` 是 GitHub Actions 格式，而本仓库的 `origin` 是自建 Gitea（`gitea.aliveranme.space`）。除非该 Gitea 启用 Actions 并注册了 runner，推送**不会触发任何检查**。这份 workflow 目前只是「若迁到 GitHub 即可用」的配置，**不能当作质量门禁**。
  - 它描述的内容：ubuntu-latest × py3.10/3.11/3.12，跑 `ruff check .` 加对仓库根 `setup.py` 的单独 lint，再跑 `pytest --cov`（ruff 规则见 `pyproject.toml` 的 `[tool.ruff]`）。
  - 提交前请在本地手动执行：`cd mcp-stata-server && .venv/bin/python -m pytest tests/ -q && .venv/bin/python -m ruff check server.py tool_modules/ tests/ tests_e2e/ && .venv/bin/python -m ruff check --config pyproject.toml ../setup.py`
  - 即便迁到 GitHub，也只覆盖 Linux；本项目主要面向 Windows（`STATA_HOME` 默认即 Windows 路径），Windows 与 macOS 都无覆盖。
  - 测试用 `conftest.abs_path()` 构造平台原生绝对路径，不要硬编码 `C:/` —— POSIX 下 `os.path.isabs("C:/x")` 为假，路径会被当相对路径拼上 cwd。
- **`stata_snapshot` 只覆盖内存数据集，不是全会话快照**：Stata 原生 `snapshot` 保存的是数据 + 值标签，估计结果、宏、program、frame 结构都不在其中；`restore` 后这些需要重跑。真「多会话隔离」需多个 Stata 实例（受许可证并发实例数约束），snapshot 是单会话内的轻量回退。
- **后台任务不提供真正的并行**：Stata DLL 单线程，`stata_background` 的任务与前台命令共享同一把 `_stata_lock` —— 任务运行期间其他工具调用会**阻塞等待**，`stata_task_status` 不受此限（不持锁）。它的价值是交互模型：提交不阻塞、进度可查、可显式取消、单块超时上限放宽到 3600s，而不是并发提速。
- **资源注册表是会话级的**：MCP Server 重启后 `_resource_registry` 清空，磁盘上的导出文件仍在但需重新登记（`stata_register_file`）才能经资源协议读取。
- **`stata_run(save_output=)` 是覆盖式写入**：已存在的目标文件会被截断（与导出工具的 `replace=False` 默认不同），这是刻意设计 —— 它服务「重跑同一分析、重新抓完整输出」的场景。
- **`tests/` 与 `tests_e2e/` 必须分开跑**：`tests/conftest.py` 在导入时就把 `pystata` / `sfi` 换成 `MagicMock`（且只在 `sys.modules` 里没有时才装桩），同一个 pytest 进程里再也换不回真 Stata。
  - 单元测试（无需 Stata，默认 `testpaths`）：`.venv/bin/python -m pytest tests/ -q`
  - 端到端（需真 Stata）：`STATA_HOME=/path/to/StataNow .venv/bin/python -m pytest tests_e2e/ -q`；未检测到安装时整目录自动跳过。混跑 `tests/ tests_e2e/` 时 E2E 会带明确原因跳过，不会静默对着 mock 断言。
  - `tests_e2e/` 有 `__init__.py` 是**必需的**：否则 pytest 把该目录插进 `sys.path` 并以 `conftest` 为模块名导入，与 `tests/conftest.py` 撞名，`tests/` 里的 `from conftest import abs_path` 会解析错。
  - E2E 只放**单元测试无法证伪**的断言（即代码对 Stata 实际行为的假设）；命令拼接与参数校验留在 `tests/`。
- **无类型检查**：无 `mypy`、`pre-commit`。server.py 混合中英文标识符。
- **`setup.py` 自动检测只覆盖标准安装位置**：macOS 扫 `/Applications` 与 `~/Applications`，Windows 扫 `ProgramFiles`/`ProgramFiles(x86)`/`D:`/`E:`，Linux 扫 `/usr/local`、`/opt`。装在其他位置（实测外置卷 `/Volumes/ccc/Applications/StataNow` 即检测不到）时 `find_stata_installation()` 返回 `(None, None)` —— 需先设 `STATA_HOME` 环境变量再跑。这不是缺陷，但文档要说清，否则用户会以为「不支持我的系统」。
- **`setup.py` 的测试覆盖仅限纯函数部分**：`tests/test_setup_script.py` 按路径加载仓库根的 `setup.py`（顶层只有定义，入口在 `if __name__ == "__main__"` 之下，可安全导入），覆盖 `generate_mcp_json` 的合并语义与失败路径、`install_deps` 的版本下界与超时、`find_stata_installation` 的 `STATA_HOME` 分支。**未覆盖**：`create_venv`（真建 venv）、`test_server`（真起进程）、`main()` 的交互式输入分支 —— 这些需要真实子进程或 stdin，留给手工验证。
  - `FASTMCP_SPEC` 在 `setup.py`、`requirements.txt`、`pyproject.toml` 三处各有声明，但只有 `setup.py` 那处会被新用户实际执行；`test_fastmcp_spec_matches_requirements_and_pyproject` 守住三者一致，防止再次出现「元数据写 `>=3.2.0`、安装的却是裸 `fastmcp`」。
- **日志写入文件**：server.py 已将日志同时输出到 stderr 和 `mcp-stata-server/logs/stata-mcp.log`，MCP 传输中断后仍可排查。
- **`stata_export_excel` 的 results=True 需要先运行过回归模型**：会用 `esttab` 导出估计结果；执行前先用裸 `which estout` 探测（**不能加 `capture`** —— 那会吞掉错误使 rc 恒为 0，探测形同虚设），**estout 缺失则直接报错**，提示用 `stata_install_package("estout", source="ssc")` 手动安装。**不要内嵌 `ssc install`** —— 但原因不是「损坏 DLL」。实测（Stata 19.5 MP，macOS）：`ssc install` 是网络阻塞调用，同一个包耗时在 **3–13s 间波动**，慢/不可达网络下更久；它整段独占 `_stata_lock` 阻塞整个 server。**看门狗超时对它是生效的**：装超过 timeout 时 `SetBreak` 会**干净中断**（实测 rdrobust 在 10s 下限被 break，返回超时提示，会话健康、包不残留半装状态；`timeout=1/2` 的 fre/mdesc 没被 break 只是它们在 10s 下限前就装完了）。**全程无 DLL 损坏**——多场景复现，break 后 `display`/`summarize`/`regress` 全正常。真正的问题只是：内嵌进分析步骤时，一个几秒到十几秒的网络阻塞会意外冻结整个流程。故包安装走专用的 `stata_install_package`（用户可控时机、`timeout` 参数真实兜底）。
- **超时看门狗线程安全**：Stata DLL 不提供官方线程安全的中断机制。看门狗在命令超时时调用 `StataSO_SetBreak`，与执行线程的 `StataSO_Execute` 存在极小并发风险。当前的缓解是串行锁、较低的默认超时（60s）与锁内二次确认；本条此前还写有「连续 break 熔断」，代码中并不存在，已删除。**二次确认此前留有窗口**：主线程在 `StataSO_Execute` 返回后还要走完 `RedirectOutput.__exit__` 与临时文件清理才置位事件，看门狗恰在这段间隙确认时仍读到未完成，于是 break 落在命令结束之后并被下一条命令消费（表现为无关的 rc=1）。现已闭合 —— 命令一返回就立即置位，且置位与看门狗的「确认+break」共用 `break_guard`。剩余风险是 `SetBreak` 与 `StataSO_Execute` 本身的 DLL 层并发，无法在 Python 侧消除。建议长命令显式拆分或使用更大的 timeout 参数。
- **工具错误语义**：错误结果（Stata 返回码非 0、输入验证失败、DLL 崩溃）通过 `ToolResult(is_error=True)` 告知 MCP 客户端。成功工具结果仍以普通字符串返回。若使用 `mcp.list_tools` 或类似客户端，需注意区分返回类型。
