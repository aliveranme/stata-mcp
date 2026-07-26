---
name: stata
description: >
  Stata 数据分析与计量经济学编程指南。当用户需要撰写 Stata 命令、构建 do 文件、
  进行统计分析（回归、描述统计、假设检验）、数据清洗与处理、或使用 Stata 进行
  计量经济学建模时，必须使用此 Skill。包括但不限于：用户提到 Stata、.do 文件、
  .dta 数据、regress/logit/t test/summarize/tabulate 等 Stata 命令、面板数据、
  工具变量、DID、时间序列分析等。即使用户没有明确说"Stata"，只要涉及统计分析
  和计量建模需求，也应优先考虑调用此 Skill 来生成 Stata 代码并执行。
---

# Stata 数据分析与计量经济学编程

## 概述

此 Skill 指导你撰写正确的 Stata 命令和 do 文件，并通过 MCP Server (`stata`)
在本地 StataNow 19 MP 环境中实时执行。你可以独立完成数据加载、清洗、分析、
结果输出全流程。

**MCP Server 信息：**
- 名称：`stata`，50 个工具覆盖数据管理/生成、探索、估计、后估计、图形导出、包管理与帮助、会话控制；`stata_run` + `stata_help` 覆盖全部内置命令
- Stata 版本：StataNow 19 / Stata 18+（取决于安装的版本）
- 连接方式：本地 stdio，通过 pystata 直接调用 DLL
- **会话持久**：Stata 在服务器启动时初始化一次，所有命令共享同一会话。
  数据加载后会一直保留在内存中，直到被 `clear` 或替换。

---

## 核心原则

### 每次分析前

1. **先了解数据** — 使用 `stata_describe` 和 `stata_codebook` 查看变量信息
2. **变量名大小写敏感** — `mpg` 和 `MPG` 是不同的变量
3. **路径用正斜杠** — `D:/data/file.dta`，不要用反斜杠
4. **带空格的路径用双引号包裹** — `use "D:/my data/file.dta", clear`
5. **检查返回值** — 用 `stata_display` 查看 `r(mean)`、`e(N)`、`e(r2)` 等
6. **限制输出大小** — 永远不要执行 `list` 而不限制观测数。数据量大时必须用
   `in 1/N` 或 `n` 参数限制。输出超过 4000 字符会自动分页

### 命令执行策略

- **单次查询** → 使用专用工具（`stata_summarize`、`stata_tabulate` 等，输出紧凑）
- **多步骤流程** → 使用 `stata_run`，用 `\n` 连接多条命令
- **已有 .do 文件** → 使用 `stata_run_do_file`
- **每次 `stata_run` 应包含逻辑完整的一组命令**，避免零碎调用

### 输出大小控制（重要！）

**始终在产生输出的命令中限制范围**，避免生成几万字符的原始输出：

| 命令 | 不推荐（输出过大） | 推荐（限制输出） |
|------|-------------------|-----------------|
| `list` | `list` — 输出全部 74 条，40K 字符 | `list in 1/10` — 只显示前 10 条 |
| `summarize` | 无限制 — 通常安全 | `summarize varlist if condition` |
| `tabulate` | 无限制 — 通常安全 | `tabulate var in 1/100` |
| `browse` | 不可用（GUI 命令） | 改用 `list in 1/N` |
| `describe` | 永远安全 | — |

**分页工作流**：
```
1. stata_list(n=10)         → 先看前 10 条了解数据结构
2. stata_summarize()         → 用描述统计看整体
3. stata_list(in_range="1/l") → 确实需要全部数据时，会自动分页
4. stata_more(page=2)        → 翻页浏览
```

**经验法则**：
- 观测数 < 50：`list` 全部输出通常 OK
- 观测数 50-500：`list in 1/20` 预览 + `summarize` + `codebook` 了解全貌
- 观测数 > 500：只用 `summarize`、`tabulate`、`codebook`，避免用 `list`
- 回归/检验命令的输出通常不会过大，无需限制

---

## MCP 工具参考

> **命令支持模型**：`stata_run` 能执行**任意** Stata 命令，`stata_help` 能查
> **任意**命令的官方语法 —— 二者合起来即「全量内置命令支持」。下面的专用工具
> 只是给最高频的命令加了结构化参数与校验，是便利层，不是能力边界。拿不准某条
> 命令的语法时先 `stata_help("命令名")`，再用 `stata_run` 执行。

### 数据管理
| 工具 | 用途 | destructiveHint |
|------|------|:---:|
| `stata_use_dataset` | 加载 .dta 文件；可用 `varlist`/`condition`/`in_range` 只载入子集 | ✓ |
| `stata_import` | 导入 excel/csv/sas/spss/dbase/parquet（按扩展名推断；不适用的选项会被丢弃并说明） | ✓ |
| `stata_xtset` | 声明面板(`xtset`)/时序(`tsset`)结构 —— **`stata_xtreg` 的前提**；`action="show"` 查当前设定 | 改会话 |
| `stata_use_example` | 加载官方示例数据：`sysuse`(本地) / `source="webuse"`(联网)；`action="list"` 列出可用 | ✓ |
| `stata_merge` | 横向合并：`kind` = 1:1/m:1/1:m/m:m，`keyvars` 键变量，`keepusing` 限定带入变量。合并后查 `_merge` | ✓ |
| `stata_append` | 纵向追加，`using` 可空格分隔多个文件；`options="generate(src)"` 标记来源 | ✓ |
| `stata_reshape` | 长宽转换：`direction`=long/wide，`stub` 前缀，`i` 个体，`j` 区分列。面板分析要长表 | ✓ |
| `stata_collapse` | 按组聚合，**就地替换数据集**；`clist="(mean) price (sd) mpg"`，`by` 分组 | ✓ |
| `stata_frame` | 多数据集 frame：`dir`/`current`/`create`/`change`/`drop`/`copy`/`rename` | ✓ |
| `stata_verify` | 校验：`count`/`assert`/`duplicates`/`isid`/`missing`。分析前跑一遍能挡掉多数「结果诡异」的根因 | — |
| `stata_save_dataset` | 保存当前数据 | ✓ |
| `stata_set_cwd` | 更改工作目录 | ✓ |
| `stata_generate` | 创建新变量（`generate`）；支持 `condition` | 改数据集 |
| `stata_egen` | 扩展生成（`egen`）；支持 `by` 组内聚合 | 改数据集 |

### 数据探索（只读）
| 工具 | 用途 |
|------|------|
| `stata_describe` | 变量基本信息 |
| `stata_codebook` | 详细变量字典；支持 `condition` |
| `stata_summarize` | 描述统计量；支持 `condition` / `detail` |
| `stata_list` | 查看数据值；支持 `condition` / `in_range` |
| `stata_tabulate` | 频数/交叉表；支持 `condition`、卡方检验 |
| `stata_correlate` | 相关矩阵（`correlate`/`pwcorr`）；支持 `condition` |
| `stata_display` | 表达式计算/返回值 |

### 统计分析（只读）
| 工具 | 用途 | 模型类型 |
|------|------|----------|
| `stata_regress` | 线性回归 (OLS)；支持 `condition` | 横截面 |
| `stata_logistic` | Logistic 回归；支持 `condition` | 二元选择 |
| `stata_probit` | Probit 回归；可选 `marginal_effects` 附平均边际效应 | 二元选择 |
| `stata_poisson` | Poisson 回归；可选 `irr` 报发生率比 | 计数 |
| `stata_ttest` | t 检验四形式：`compare_to="5000"`（单样本）/ `byvar=`（按组）/ `compare_to="v2"`（配对，加 `options="unpaired"` 为非配对）。**二者必给其一** —— 裸 `ttest var` 是非法命令 | 均值比较 |
| `stata_xtreg` | 面板回归；`effects` = fe/re/be/mle/pa（需先 `xtset`） | 面板 |
| `stata_ivregress` | 工具变量 2SLS/LIML/GMM | 内生性 |

### 后估计（只读，须先跑估计命令）
| 工具 | 用途 |
|------|------|
| `stata_margins` | 边际效应 / 预测边际；`dydx` / `at` |
| `stata_test` | 系数的 Wald 检验（联合显著、系数相等） |
| `stata_predict` | 生成预测值/残差（会创建新变量，改数据集） |
| `stata_estat` | 诊断：`vif` 多重共线 / `hettest` 异方差 / `ovtest` 遗漏变量 / `ic` AIC-BIC / `firststage` IV 第一阶段 |
| `stata_estimates` | 存取模型：`store`/`restore`/`drop`（需 name）、`table`/`stats`（可多个 name 并排比较）、`dir`/`clear` |

### 通用执行
| 工具 | 用途 |
|------|------|
| `stata_run` | **执行任意 Stata 命令**（专用工具未覆盖的操作全走这里） |
| `stata_run_do_file` | 执行 .do 文件；执行前自动拆出 `ssc install` 单独安装（已装跳过），避免脚本卡在网络请求 |
| `stata_graph` | 生成图形（推荐用 `export` 参数直接导出）；选项按格式自动适配官方边界 |
| `stata_scheme` | 主题：`action="list"` 列出全部方案 / `"get"` 查当前 / `"set"` 切换（可 `permanently`） |

### 结果导出
| 工具 | 用途 |
|------|------|
| `stata_etable` | **回归表导出首选**（官方 `etable`，Stata 17+，无第三方依赖）。`estimates="m1 m2"` 并排多模型，`stars=True` 加星号，`stats="N r2"` 附统计量，`export=` 直出 .docx/.xlsx/.pdf/.tex/.html/.md |
| `stata_export_excel` | 导出数据集为 .xlsx（`sheet_mode`/`cell`/`firstrow`/`if`-`in`）；`results=True` 是回归表的**旧路径**：依赖第三方 estout 且只能产出 CSV，新代码用 `stata_etable` |
| `stata_export_delimited` | 导出为 CSV / TSV / 自定义分隔符（`delimiter`、`novarnames`、`quote` 等） |

### 包管理与帮助
| 工具 | 用途 |
|------|------|
| `stata_help` | **查任意命令的官方帮助**（内置 + 已装外置，覆盖全部命令） |
| `stata_install_package` | 安装扩展包（ssc 或完整 from() URL）；`replace=True` 即重装最新 |
| `stata_uninstall_package` | 卸载已装包（`ado uninstall`，纯本地，与 install 对称） |
| `stata_describe_package` | 查包详情：默认本地 `ado describe`；`source="ssc"` 联网查（装前了解） |
| `stata_find_package` | 联网搜索扩展包（`net search`，0.6–2s）。宽泛多词查询输出极大（实测 94K 字符/24 页），用 `scope="toc"` 收窄约 8 倍；`match_any=True` 慢 13 倍慎用 |
| `stata_list_packages` | 列出已安装包 |

### 会话控制
| 工具 | 用途 |
|------|------|
| `stata_status` | 会话状态：数据集、工作目录、**frame**、**面板/时序设定**、**已存与活跃估计**、内存。调 `xtreg`/`margins`/`predict` 前先看这个 |
| `stata_return_list` | 一次列出 `r()`（kind="r"）/ `e()`（"e"）/ `c()`（"c"）全部返回值 |
| `stata_ping` | 快速检测 Stata DLL 存活 |

### 参数与官方语法位置的对应

所有包装具体命令的工具都对齐了官方语法 `command [varlist] [if] [in] [, options]`：

| 官方位置 | 工具参数 | 说明 |
|----------|----------|------|
| `[if]` | `condition` | 如 `condition="foreign == 1"` |
| `[in]` | `in_range` | 如 `in_range="1/100"`；`if` 与 `in` 叠加是「**前 n 条里**满足条件的」 |
| `[, options]` | `options` | 长尾官方选项的自由文本出口，如 `options="noobs clean"` |
| `[type]` | `vartype` | 仅 `generate`/`egen`：`byte`/`int`/`long`/`float`/`double`/`str#`/`strL` |

不确定某选项属于哪个位置时先 `stata_help("命令名")`。少数命令没有某些位置
（`test` 不接受 `if`/`in`，`display` 没有 `, options`），工具也就不提供对应参数。

### 工具选择指南

- 有专用工具的命令（回归/面板/IV/边际效应/生成变量等）→ 优先用专用工具，参数更规整、有校验
- 专用工具未覆盖的命令（`anova`、`reshape`、`merge`、`graph bar`、`heckman` 等）→ 用 `stata_run`
- 不确定某命令的语法/选项 → 先 `stata_help("命令名")` 查官方文档，再执行
- 多命令组合（加载 + 清洗 + 回归）→ 单次 `stata_run` 用 `\n` 连接
- 需要第三方包 → 先 `stata_find_package` 搜索，再 `stata_install_package` 安装

---

## 内置命令地图

Stata 有 3500+ 内置命令，**全部**可经 `stata_run` 执行、`stata_help` 查语法。
下表按族列出高频命令，帮你快速定位；具体语法与选项一律 `stata_help("命令名")`。

### 数据管理
| 类别 | 命令 |
|------|------|
| 生成/修改 | `generate` `replace` `egen` `recode` `rename` `drop` `keep` `order` |
| 类型转换 | `destring` `tostring` `encode` `decode` `format` `label` |
| 重构 | `reshape`（长宽转换）`collapse`（聚合）`expand` `contract` `separate` |
| 合并 | `merge`（横向）`append`（纵向）`joinby` `cross` |
| 排序/去重 | `sort` `gsort` `by`/`bysort` `duplicates` |
| 抽样/保存 | `sample` `preserve`/`restore` `save` `use` `import`/`export` `frame` |

### 数据探索
| 类别 | 命令 |
|------|------|
| 结构 | `describe` `codebook` `inspect` `ds` `lookfor` `compare` |
| 统计 | `summarize` `tabstat` `tabulate` `table` `pwcorr`/`correlate` |
| 缺失/分布 | `misstable` `histogram` `kdensity` `pnorm`/`qnorm` |

### 估计（estimation）
| 类别 | 命令 |
|------|------|
| 线性 | `regress` `areg` `anova` `cnsreg` `nl` |
| 二元/多元选择 | `logit`/`logistic` `probit` `mlogit` `ologit`/`oprobit` `clogit` |
| 计数 | `poisson` `nbreg` `zip`/`zinb` `tpoisson` |
| 面板（xt） | `xtreg` `xtlogit` `xtpoisson` `xtgls` `xtabond` `xttobit` |
| 时间序列（ts） | `tsset` `arima` `var` `vec` `dfuller` `newey` |
| 内生性/选择 | `ivregress` `heckman` `treatreg` `etregress` |
| 生存/删失 | `stset` `stcox` `streg` `tobit` `intreg` |
| 分位数/稳健 | `qreg` `rreg` `bootstrap` `jackknife` |

### 后估计（postestimation，须先跑估计命令）
| 类别 | 命令 |
|------|------|
| 预测/边际 | `predict` `margins` `marginsplot` `estat` |
| 假设检验 | `test` `testnl` `lincom` `nlcom` `contrast` |
| 模型比较 | `estimates store`/`table` `lrtest` `hausman` `estat ic` |
| 诊断 | `estat vif` `estat hettest` `estat ovtest` `estat firststage` `rvfplot` |

### 图形
`twoway`（`scatter` `line` `lfit` `connected`）`histogram` `graph bar`/`box`/`pie`
`kdensity` `marginsplot` `coefplot`(外置) —— 一律经 `stata_graph` 导出。

### 编程/其他
`forvalues` `foreach` `while` `if`/`else` `program` `local`/`global` `scalar`
`matrix` `return`/`ereturn` `capture` `assert` `postfile`

---

## Stata 语法要点

### 基本规则

1. **命令不区分大小写** — `SUMMARIZE` = `summarize`
2. **变量名区分大小写** — `price` ≠ `Price`
3. **注释** — `//` 行尾、`/* */` 块注释、`*` 行首
4. **缺失值** — `.` 被视为正无穷大。`if x > 100` 会包含缺失值，
   应使用 `if x > 100 & x < .`

### 命令结构

```
command varlist [if exp] [in range] [weight] [, options]
```

### if 条件 — 关键陷阱

`if` 在 Stata 中有两种截然不同的用法：

```stata
// ✓ 命令限定符 — 对每条观测独立求值（数据筛选用这个）
summarize price if foreign == 1
regress mpg weight if price < 10000

// ✗ 编程控制流 — 只对第一条观测求值（do-file 编程用这个）
if `r(mean)' > 100 {
    display "mean > 100"
}
```

**数据筛选时始终使用命令限定符（不带花括号）。**

### 变量生成

```stata
generate newvar = expression
replace oldvar = expression if condition
egen newvar = function(varlist)
```

### 常用 egen 函数

- `egen mean_x = mean(x)` — 均值
- `egen sd_x = sd(x)` — 标准差
- `egen total_x = total(x)` — 总和
- `egen tag = tag(id)` — 标记每组首条观测
- `egen group_id = group(var1 var2)` — 创建分组 ID
- `egen pctile_x = pctile(x), p(25)` — 百分位数

### 因子变量（Stata 11+）

```stata
regress wage age i.industry i.year           // 自动创建虚拟变量
regress wage age i.industry##i.year          // 含交互项
regress wage c.age##c.age                    // 二次项
```

`i.` 前缀自动创建虚拟变量并处理共线性，优于手动 `tabulate, gen()`。

### 循环与条件块

`forvalues` / `foreach` / `if` 块、`program define ... end` 都可以直接写进
`stata_run`，整块会被原子执行：

```stata
foreach v in price weight mpg {
    summarize `v'
}

forvalues i = 1/5 {
    display "第 `i' 次"
}

if _N > 100 {
    regress price weight
}
```

**`{` 之后必须换行**，这是 Stata 本身的语法规则：

```stata
forvalues i = 1/3 { display `i' }     // ❌ r(198)
forvalues i = 1/3 {                   // ✅
    display `i'
}
```

**每个块必须在同一次调用里闭合**。不要把开头和结尾拆到两次 `stata_run`：

```stata
// ❌ 第一次调用只发开头 —— 会被拒绝
stata_run("forvalues i = 1/3 {")
stata_run("    display `i'\n}")

// ✅ 一次发完整块
stata_run("forvalues i = 1/3 {\n    display `i'\n}")
```

未闭合的块会返回「命令块未闭合（缺少 `}`）」错误。这不是过度严格：Stata 收到
孤立的 `{` 会进入等待输入状态，在 MCP 会话中直接挂死整个连接且无法恢复。
`program define ... end`、`input ... end` 同理。

批量处理变量时，循环比逐条调用工具高效得多 —— 一次往返完成全部迭代。

---

## 数据分析模板

### 1：快速数据探索
```stata
use "data.dta", clear
describe
codebook, compact
summarize
tabulate categorical_var
```

### 2：OLS 回归
```stata
use "data.dta", clear
summarize depvar indepvars
pwcorr depvar indepvars, sig
regress depvar indepvars
regress depvar indepvars, robust
estimates store model1
```

### 3：Logistic 回归
```stata
use "data.dta", clear
tabulate depvar
logit depvar indepvars
logit depvar indepvars, or
estimates store logit_model
```

### 4：分组比较
```stata
use "data.dta", clear
bysort groupvar: summarize depvar
ttest depvar, by(groupvar)
```

### 5：数据清洗
```stata
use "raw_data.dta", clear
misstable summarize                           // 缺失值报告
drop if missing(keyvar)                        // 删除关键变量缺失
rename oldname newname                         // 重命名
label variable varname "变量说明"              // 变量标签
label define lbl 1 "类别1" 2 "类别2"          // 值标签
label values varname lbl
generate log_price = ln(price)
generate price_sq = price^2
save "clean_data.dta", replace
```

### 6：面板数据（xt 系列）
```stata
use "panel_data.dta", clear
xtset id year                                  // 声明面板结构
xtdescribe                                     // 面板描述
xtsum y x1 x2                                  // 面板摘要统计
xtreg y x1 x2, fe                              // 固定效应
estimates store fe                             // 必须存储，hausman 靠名字引用
xtreg y x1 x2, re                              // 随机效应
estimates store re
hausman fe re                                  // Hausman 检验
```

### 7：工具变量（IV / 2SLS）

**官方 `ivregress`（无需安装，诊断走 estat）**
```stata
use "data.dta", clear
ivregress 2sls y (x = z1 z2), robust
estat firststage                               // 第一阶段 F（弱工具变量检验）
estat overid                                   // 过度识别检验（Sargan/Hansen）
```

**SSC 的 `ivreg2`（诊断直接打印在主输出里，不要用 estat）**
```stata
// 需要 ivreg2：先用 stata_install_package("ivreg2", source="ssc") 安装，
// 不要把 ssc install 写进 stata_run —— 网络阻塞会长时间冻结流程（非损坏 DLL）
use "data.dta", clear
ivreg2 y (x = z1 z2), robust first            // first 选项输出第一阶段
// Hansen J / Kleibergen-Paap 统计量已在上面的输出里，无需再调 estat
// ivreg2 不注册 estat handler，`estat firststage` 会报 r(321)
```

两套不要混用：`estat firststage` / `estat overid` 只对 `ivregress` 有效。

### 8：DID（双重差分）
```stata
use "did_data.dta", clear
generate treat_post = treat * post
regress y treat post treat_post, robust       // 经典 2x2 DID
// 事件研究：需 eventdd，先用 stata_install_package("eventdd", source="ssc") 安装
eventdd y, hdfe absorb(id year) timevar(year) method(fe)
```

---

## 常见陷阱

### 1. 变量名冲突
```stata
// ✗ 错误
generate price = price / 100
// ✓ 正确
replace price = price / 100
// 或
generate price_new = price / 100
```

### 2. 字符串与数值
```stata
destring varname, replace        // 字符串 → 数值
tostring varname, replace        // 数值 → 字符串
encode stringvar, gen(numvar)    // 字符串 → 带标签数值
decode numvar, gen(stringvar)    // 带标签数值 → 字符串
```

### 3. 缺失值陷阱
```stata
// ✗ 错误：会包含 price 缺失值
summarize if price > 10000
// ✓ 正确
summarize if price > 10000 & price < .
summarize if !missing(price) & price > 10000
```

### 4. do 文件模板
```stata
clear all
set more off
capture log close
log using "analysis.log", replace text

// 分析代码 ...

log close
```

### 5. 路径
```stata
// ✗ 错误
use "C:\data\file.dta"
// ✓ 正确
use "C:/data/file.dta"
cd "C:/data"
use "file.dta", clear
```

---

## 回归后诊断

```stata
regress y x1 x2 x3
estat hettest                              // 异方差检验
estat vif                                  // 多重共线性 (VIF)
predict resid, resid                       // 残差
predict fitted, xb                         // 拟合值
predict std_resid, rstandard               // 标准化残差
swilk std_resid                            // 正态性检验
rvfplot                                    // 残差 vs 拟合散点图
```

## 结果存储与输出

```stata
regress y x1 x2, robust
estimates store m1
regress y x1 x2 x3, robust
estimates store m2
estimates table m1 m2, star stats(N r2 r2_a)

// 导出（需 estout；缺失时先用 stata_install_package("estout", source="ssc") 装，
//       不要把 ssc install 混进 stata_run —— 网络阻塞会长时间冻结流程（非损坏 DLL）
esttab m1 m2 using "results.csv", replace
```

---

## 常用第三方包

用法：`stata_find_package("包名")` 搜 → `stata_describe_package("包名", source="ssc")`
看详情（可选）→ `stata_install_package("包名", source="ssc")` 装 → `stata_help("包名")`
查语法。不再需要时 `stata_uninstall_package("包名")` 卸载。**不要**把 `ssc install`
或 `ssc describe` 写进 `stata_run` —— 它们是网络阻塞调用（实测 3–13s 波动，慢网络更久），
会独占串行锁冻结整个流程且看门狗超时对其无效；改走专用工具（可控时机、显式 timeout）。
注意：这是「阻塞太久」而非「损坏 DLL」，网络正常时安装本身会干净完成。

### 结果输出 / 表格
| 包 | 用途 | 补足哪个内置 |
|----|------|------|
| `estout` / `esttab` / `eststo` | 估计结果格式化成表（CSV/LaTeX/RTF） | `estimates table` |
| `outreg2` | 回归结果导出 Word/Excel/LaTeX | 同上 |
| `coefplot` | 系数图（点估计 + 置信区间） | `marginsplot` |

### 高维固定效应 / 面板
| 包 | 用途 | 补足哪个内置 |
|----|------|------|
| `reghdfe` | 多维固定效应线性回归（吸收大量虚拟变量） | `areg` / `xtreg` |
| `ivreghdfe` | 高维固定效应 + IV | `ivregress` |
| `ppmlhdfe` | 泊松高维固定效应（引力模型常用） | `poisson` |
| `ftools` | reghdfe/gtools 的底层依赖 | — |
| `xtabond2` | 动态面板 GMM（差分/系统 GMM） | `xtabond` |
| `xtscc` | Driscoll-Kraay 标准误（面板异方差/自相关） | `xtreg, vce()` |

### 工具变量
| 包 | 用途 | 补足哪个内置 |
|----|------|------|
| `ivreg2` | IV/2SLS，弱工具/过度识别诊断直接打印在主输出 | `ivregress`（诊断走 `estat`） |

### 因果推断 / 政策评估
| 包 | 用途 | 补足哪个内置 |
|----|------|------|
| `csdid` | Callaway-Sant'Anna 交错 DID（异质处理效应） | 手写 DID 交互项 |
| `did_multiplegt` | de Chaisemartin-D'Haultfœuille DID | 同上 |
| `eventdd` | 事件研究法 DID 图 | 同上 |
| `drdid` | 双重稳健 DID | 同上 |
| `rdrobust` / `rddensity` | 断点回归（RD）估计与操纵检验 | — |
| `psmatch2` / `teffects`(内置) | 倾向得分匹配 | `teffects psmatch` |
| `synth` / `synth_runner` | 合成控制法 | — |

### 微观计量 / 分解
| 包 | 用途 | 补足哪个内置 |
|----|------|------|
| `cmp` | 递归多方程混合模型（联立 probit/tobit 等） | 分别估计 |
| `oaxaca` | Blinder-Oaxaca 分解（工资差异等） | — |
| `gllamm` | 广义线性潜变量混合模型 | `me` 系列 |

### 数据处理 / 工具
| 包 | 用途 | 补足哪个内置 |
|----|------|------|
| `winsor2` | 缩尾/截尾处理（注：`suffix()` 与 `replace` 互斥） | 手写 `egen pctile` |
| `gtools`（`gcollapse` 等） | 大数据集加速版 collapse/egen | `collapse` / `egen` |
| `binscatter` | 分箱散点图（**headless 可能挂起**，优先原生 `twoway scatter`） | — |
| `estout` 见上 | — | — |

> 未列出的包用 `stata_find_package("关键词")` 联网搜索。装好后 `stata_help("包名")`
> 拿权威语法，不要凭记忆拼选项。

---

## 与 Agent 协作规范

1. **分析前先 `stata_status`**，了解当前会话状态
2. **完整流程**：加载 → 探索 → 清洗 → 分析 → 输出结果
3. **向用户展示关键结果**，而非原始 Stata 日志的全部内容
4. **输出大小第一原则**：永远不要产生无限制的原始输出。
   - `list` 必须限定 `in 1/N`（已知 74 条数据 `in 1/20`，未知数据 `in 1/10`）
   - 优先用 `summarize`、`tabulate`、`codebook` 而非 `list`
   - 确实需要全量数据时利用自动分页：先看首页，需要时再 `stata_more`
5. **错误排查顺序**：变量名拼写 → 数据是否加载 → 路径 → 包是否安装
5a. **缺失包的处理**：某工具报「未安装 X 包」时，**不要**自己往 `stata_run` 里塞 `ssc install`（会阻塞冻结流程）。照提示单独调用一次 `stata_install_package("X", source="ssc", timeout=120)`（联网，阻塞几秒到十几秒；超时会被干净中断不卡死），装好后**重试刚才失败的那一步**、继续原任务。不要因为装个包就放弃或重排整个分析。
6. **图形需导出**：`stata_graph(command=..., export="output/fig1.png", width=1200)`。导出选项按格式而变（实测 Stata 19.5，已由 `stata_graph` 自动适配，不适用的会被丢弃并说明）：

   | 格式 | width/height | quality | mag | fontface |
   |------|------|:---:|:---:|:---:|
   | png / jpg / tif / gif | 像素（8–16000） | 仅 jpg | ✗ | ✗ |
   | svg | 像素 | ✗ | ✗ | ✓ |
   | pdf | **英寸 0.5–20** | ✗ | ✓ | ✓ |
   | eps / ps | **不支持** | ✗ | ✓ | ✓ |
   | emf / wmf | **不支持** | ✗ | ✗ | ✗ |

   可用性还依环境：emf/wmf 仅 Windows、gif 仅 Mac GUI、tif 不支持 console；本 MCP 是 headless console，实测只有 png/jpg/pdf/svg/eps/ps 能用。`.jpeg` 不是官方后缀。

6a. **主题（scheme）**：`stata_graph` 不传 `scheme` 时**不会改动**当前主题（Stata 19 默认 `stcolor`）。要看/换主题用 `stata_scheme(action="list"/"get"/"set")`；跨会话保留传 `permanently=True`。
7. **图形导出优先使用 `stata_graph(..., export=...)`**：如 `stata_graph(command="twoway scatter mpg weight", export="output/scatter.png", scheme="s2color")`。它把 graph 与 export 放进同一复合块，少一次往返；导出成败以文件是否真被写入为准，失败会明确报错。分两步调用（先 `scatter` 再 `graph export`）实测也能成功，但错误定位更分散。
8. **大输出自动分页**：单命令输出 > 4000 字符时自动分页，`stata_more(page=N)` 翻页
9. **分析完成后向用户汇报**：用了什么方法、关键发现是什么
10. **危险命令避免**：`stata_run` 与 `stata_graph(command=)` 都会拦截行首 `!`、`shell`、`winexec`、`python:`、`python (`、裸 `python`，以及一切 `mata` 开头的命令（Mata 可经 `_stata()` 执行任意命令并直接读写文件，与内嵌 Python 同等对待）。不要尝试构造这些命令绕过过滤，也不要在未明确告知用户风险前构造删除、修改系统文件的操作。如确有系统级操作需求，请在操作系统命令行直接执行，不要通过 Stata 中转；确需 Mata 编程请在 Stata 界面里做。
11. **默认值注意**：
    - `stata_graph(..., replace=False)` — 导出文件时默认不覆盖已有文件，需显式传入 `replace=True`
    - `stata_export_excel(..., replace=False)` — 导出文件时默认不覆盖已有文件
    - `stata_use_dataset(filepath, clear=True)` — 默认清除内存中已有数据
    - `stata_run(command, timeout=60)` — 命令默认超时 60s，安装包/复杂回归可传 `timeout=120`
12. **`stata_graph` 非只读**：它注册为 `readOnlyHint=False, destructiveHint=True`（导出时写磁盘），Agent 应在覆盖文件前向用户确认。此前本条写作「虽然标记为只读探索」，与实际注解相反。
13. **`stata_export_excel(results=True)`** 会强制输出为 CSV，并**不会**自动安装 `estout`：执行前先探测，缺失则报错并给出可执行的单条安装命令。照提示执行 `stata_install_package("estout", source="ssc", timeout=120)`，装好后**重试本次导出**即可。不要在 `stata_run` 里内嵌 `ssc install` —— SSC 网络请求会独占串行锁阻塞整个流程（实测 3–13s 波动，慢网络更久）；这是「阻塞太久」而非「损坏 DLL」，且 `install` 工具的 `timeout` 超时会被看门狗干净中断（不卡死），改走专用工具即可。
