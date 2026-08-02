# 常用第三方包（SSC）

> 用法：`stata_find_package("关键词")` 搜 → `stata_describe_package("包名", source="ssc")`
> 看详情（可选）→ `stata_install_package("包名", source="ssc")` 装（可传
> `timeout=120`）→ `stata_help("包名")` 查语法。不再需要时 `stata_uninstall_package("包名")`
> 卸载。**不要把 `ssc install` / `ssc describe` 写进 `stata_run`** —— 网络阻塞（实测
> 3–13s 波动）会独占串行锁冻结整个流程；装包走专用工具（可控时机）。这是「阻塞太久」
> 而非「损坏 DLL」，超时会被看门狗干净中断。细节见主文档「与 Agent 协作规范」。

## 结果输出 / 表格

| 包 | 用途 | 补足哪个内置 |
|----|------|------|
| `estout` / `esttab` / `eststo` | 估计结果格式化成表（CSV/LaTeX/RTF） | `estimates table` / `stata_etable` |
| `outreg2` | 回归结果导出 Word/Excel/LaTeX | 同上 |
| `coefplot` | 系数图（点估计 + 置信区间） | `marginsplot` |

> 回归表导出**优先用 `stata_etable`**（官方 `etable`，Stata 17+，无第三方依赖，
> 直出 .docx/.xlsx/.xls/.pdf/.tex/.html/.md/.txt/.smcl）。`estout` 只在需要 CSV/LaTeX
> 兼容旧脚本时才值得装。

## 高维固定效应 / 面板

| 包 | 用途 | 补足哪个内置 |
|----|------|------|
| `reghdfe` | 多维固定效应线性回归（吸收大量虚拟变量） | `areg` / `xtreg` |
| `ivreghdfe` | 高维固定效应 + IV | `ivregress` |
| `ppmlhdfe` | 泊松高维固定效应（引力模型常用） | `poisson` |
| `ftools` | reghdfe/gtools 的底层依赖 | — |
| `xtabond2` | 动态面板 GMM（差分/系统 GMM） | `xtabond` |
| `xtscc` | Driscoll-Kraay 标准误（面板异方差/自相关） | `xtreg, vce()` |

## 工具变量

| 包 | 用途 | 补足哪个内置 |
|----|------|------|
| `ivreg2` | IV/2SLS，弱工具/过度识别诊断直接打印在主输出 | `ivregress`（诊断走 `estat`） |

## 因果推断 / 政策评估

| 包 | 用途 | 补足哪个内置 |
|----|------|------|
| `csdid` | Callaway-Sant'Anna 交错 DID（异质处理效应） | 手写 DID 交互项 |
| `did_multiplegt` | de Chaisemartin-D'Haultfœuille DID | 同上 |
| `eventdd` | 事件研究法 DID 图 | 同上 |
| `drdid` | 双重稳健 DID | 同上 |
| `rdrobust` / `rddensity` | 断点回归（RD）估计与操纵检验 | — |
| `psmatch2` / `teffects`(内置) | 倾向得分匹配 | `teffects psmatch` |
| `synth` / `synth_runner` | 合成控制法 | — |

## 微观计量 / 分解

| 包 | 用途 | 补足哪个内置 |
|----|------|------|
| `cmp` | 递归多方程混合模型（联立 probit/tobit 等） | 分别估计 |
| `oaxaca` | Blinder-Oaxaca 分解（工资差异等） | — |
| `gllamm` | 广义线性潜变量混合模型 | `me` 系列 |

## 数据处理 / 工具

| 包 | 用途 | 补足哪个内置 |
|----|------|------|
| `winsor2` | 缩尾/截尾处理（注：`suffix()` 与 `replace` 互斥） | 手写 `egen pctile` |
| `gtools`（`gcollapse` 等） | 大数据集加速版 collapse/egen | `collapse` / `egen` |
| `binscatter` | 分箱散点图（**headless 可能挂起**，优先原生 `twoway scatter`） | — |
| `estout` 见上 | — | — |

> 未列出的包用 `stata_find_package("关键词")` 联网搜索。装好后 `stata_help("包名")`
> 拿权威语法，不要凭记忆拼选项。
