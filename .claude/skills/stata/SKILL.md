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
- 名称：`stata`，19 个工具覆盖数据管理、探索、分析、包管理和会话控制
- Stata 版本：StataNow 19.5 MP
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

### 命令执行策略

- **单次查询** → 使用专用工具（`stata_summarize`、`stata_tabulate` 等）
- **多步骤流程** → 使用 `stata_run`，用 `\n` 连接多条命令
- **已有 .do 文件** → 使用 `stata_run_do_file`
- **每次 `stata_run` 应包含逻辑完整的一组命令**，避免零碎调用

---

## MCP 工具参考

### 数据管理
| 工具 | 用途 | destructiveHint |
|------|------|:---:|
| `stata_use_dataset` | 加载 .dta 文件 | ✓ |
| `stata_save_dataset` | 保存当前数据 | ✓ |
| `stata_set_cwd` | 更改工作目录 | ✓ |

### 数据探索（只读）
| 工具 | 用途 |
|------|------|
| `stata_describe` | 变量基本信息 |
| `stata_codebook` | 详细变量字典 |
| `stata_summarize` | 描述统计量 |
| `stata_list` | 查看数据值 |
| `stata_tabulate` | 频数/交叉表 |
| `stata_display` | 表达式计算/返回值 |

### 统计分析（只读）
| 工具 | 用途 | 模型类型 |
|------|------|----------|
| `stata_regress` | 线性回归 (OLS) | 横截面 |
| `stata_logistic` | Logistic 回归 | 二元选择 |
| `stata_ttest` | t 检验 | 均值比较 |

### 通用执行
| 工具 | 用途 |
|------|------|
| `stata_run` | **执行任意 Stata 命令**（用于以上工具未覆盖的操作） |
| `stata_run_do_file` | 执行 .do 文件 |
| `stata_graph` | 生成图形 |

### 包管理
| 工具 | 用途 |
|------|------|
| `stata_install_package` | 安装扩展包 |
| `stata_find_package` | 搜索扩展包 |
| `stata_list_packages` | 列出已安装包 |
| `stata_status` | 查看会话状态 |

### 工具选择指南

- 需要 `correlate`、`anova`、`pwcorr`、`ttest`（配对）、`graph bar` 等未封装命令 → 使用 `stata_run`
- 多命令组合（加载 + 清洗 + 回归）→ 单次 `stata_run` 用 `\n` 连接
- 需要安装第三方包 → 先 `stata_find_package` 搜索，再 `stata_install_package` 安装

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
xtreg y x1 x2, re                              // 随机效应
hausman fe re                                  // Hausman 检验
```

### 7：工具变量（ivreg2）
```stata
ssc install ivreg2                             // 先安装
use "data.dta", clear
ivreg2 y (x = z1 z2), robust first            // 2SLS + 第一阶段
estat firststage                               // 第一阶段 F 统计量
estat overid                                   // 过度识别检验
```

### 8：DID（双重差分）
```stata
use "did_data.dta", clear
generate treat_post = treat * post
regress y treat post treat_post, robust       // 经典 2x2 DID
// 事件研究（需安装 eventdd）
ssc install eventdd
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

// 导出（需安装 estout）
ssc install estout
esttab m1 m2 using "results.csv", replace
```

---

## 常用第三方包

| 包名 | 用途 |
|------|------|
| `outreg2` | 回归结果导出 Word/Excel/LaTeX |
| `estout` / `esttab` | 估计结果格式化输出 |
| `ivreg2` | 工具变量回归 |
| `reghdfe` | 高维固定效应 |
| `coefplot` | 系数可视化 |
| `winsor2` | 缩尾处理 |
| `binscatter` | 分箱散点图 |
| `eventdd` | 事件研究 DID |
| `ppmlhdfe` | PPML 高维固定效应 |

---

## 与 Agent 协作规范

1. **分析前先 `stata_status`**，了解当前会话状态
2. **完整流程**：加载 → 探索 → 清洗 → 分析 → 输出结果
3. **向用户展示关键结果**，而非原始 Stata 日志的全部内容
4. **错误排查顺序**：变量名拼写 → 数据是否加载 → 路径 → 包是否安装
5. **图形需导出**：`graph export "output/fig1.png", replace width(1200)`
6. **分析完成后向用户汇报**：用了什么方法、关键发现是什么
