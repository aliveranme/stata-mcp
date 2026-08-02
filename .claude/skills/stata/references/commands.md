# 内置命令地图（Stata 内置命令速查）

> Stata 有 3500+ 内置命令，**全部**可经 `stata_run` 执行、`stata_help` 查语法。
> 下表按族列出高频命令，帮你快速定位；具体语法与选项一律 `stata_help("命令名")`。
> 需要**回归/后估计等专用工具**的命令（regress、xtreg、mlogit、margins 等）优先用
> 对应专用工具而非裸命令——见主文档「MCP 工具参考」。

## 数据管理

| 类别 | 命令 |
|------|------|
| 生成/修改 | `generate` `replace` `egen` `recode` `rename` `drop` `keep` `order` |
| 类型转换 | `destring` `tostring` `encode` `decode` `format` `label` |
| 重构 | `reshape`（长宽转换）`collapse`（聚合）`expand` `contract` `separate` |
| 合并 | `merge`（横向）`append`（纵向）`joinby` `cross` |
| 排序/去重 | `sort` `gsort` `by`/`bysort` `duplicates` |
| 抽样/保存 | `sample` `preserve`/`restore` `save` `use` `import`/`export` `frame` |

## 数据探索

| 类别 | 命令 |
|------|------|
| 结构 | `describe` `codebook` `inspect` `ds` `lookfor` `compare` |
| 统计 | `summarize` `tabstat` `tabulate` `table` `pwcorr`/`correlate` |
| 缺失/分布 | `misstable` `histogram` `kdensity` `pnorm`/`qnorm` |

## 估计（estimation）

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

## 后估计（postestimation，须先跑估计命令）

| 类别 | 命令 |
|------|------|
| 预测/边际 | `predict` `margins` `marginsplot` `estat` |
| 假设检验 | `test` `testnl` `lincom` `nlcom` `contrast` |
| 模型比较 | `estimates store`/`table` `lrtest` `hausman` `estat ic` |
| 诊断 | `estat vif` `estat hettest` `estat ovtest` `estat firststage` `rvfplot` |

## 图形

`twoway`（`scatter` `line` `lfit` `connected`）`histogram` `graph bar`/`box`/`pie`
`kdensity` `marginsplot` `coefplot`(外置) —— 一律经 `stata_graph` 导出。

## 编程/其他

`forvalues` `foreach` `while` `if`/`else` `program` `local`/`global` `scalar`
`matrix` `return`/`ereturn` `capture` `assert` `postfile`
