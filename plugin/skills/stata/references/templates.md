# 数据分析模板与诊断配方

> 模板用 `stata_run` 一条命令链执行（`\n` 连接）。**多命令链首错即停**：某条命令
> 报错（如变量名拼错、数据未加载）会中止后续命令并指出第一条错误——需要继续执行时
> 用 `capture` 包裹期望失败的命令（Stata 原生语义）。
> 需第三方包时（`ivreg2`、`eventdd`、`estout` 等）**不要**把 `ssc install` 写进命令链
> （网络阻塞会冻结整个流程）；先单独调 `stata_install_package("包名", source="ssc")`
> 装好，再跑下面的模板。装包细节见主文档「与 Agent 协作规范」。

## 1：快速数据探索

```stata
use "data.dta", clear
describe
codebook, compact
summarize
tabulate categorical_var
```

## 2：OLS 回归

```stata
use "data.dta", clear
summarize depvar indepvars
pwcorr depvar indepvars, sig
regress depvar indepvars
regress depvar indepvars, robust
estimates store model1
```

## 3：Logistic 回归

```stata
use "data.dta", clear
tabulate depvar
logit depvar indepvars
logit depvar indepvars, or
estimates store logit_model
```

## 4：分组比较

```stata
use "data.dta", clear
bysort groupvar: summarize depvar
ttest depvar, by(groupvar)
```

## 5：数据清洗

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

## 6：面板数据（xt 系列）

```stata
use "panel_data.dta", clear
xtset id year                                  // 声明面板结构（stata_xtset 工具也可）
xtdescribe                                     // 面板描述
xtsum y x1 x2                                  // 面板摘要统计
xtreg y x1 x2, fe                              // 固定效应
estimates store fe                             // 必须存储，hausman 靠名字引用
xtreg y x1 x2, re                              // 随机效应
estimates store re
hausman fe re                                  // Hausman 检验
```

## 7：工具变量（IV / 2SLS）

**官方 `ivregress`（无需安装，诊断走 estat）**
```stata
use "data.dta", clear
ivregress 2sls y (x = z1 z2), robust
estat firststage                               // 第一阶段 F（弱工具变量检验）
estat overid                                   // 过度识别检验（Sargan/Hansen）
```

**SSC 的 `ivreg2`（诊断直接打印在主输出里，不要用 estat）**
```stata
// 需要 ivreg2：先 stata_install_package("ivreg2", source="ssc") 安装
use "data.dta", clear
ivreg2 y (x = z1 z2), robust first            // first 选项输出第一阶段
// Hansen J / Kleibergen-Paap 统计量已在上面的输出里，无需再调 estat
// ivreg2 不注册 estat handler，`estat firststage` 会报 r(321)
```

两套不要混用：`estat firststage` / `estat overid` 只对 `ivregress` 有效。

## 8：DID（双重差分）

```stata
use "did_data.dta", clear
generate treat_post = treat * post
regress y treat post treat_post, robust       // 经典 2x2 DID
// 事件研究：需 eventdd，先 stata_install_package("eventdd", source="ssc")
eventdd y, hdfe absorb(id year) timevar(year) method(fe)
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

// 回归表导出优先用 stata_etable（官方 etable，无第三方依赖，可直出
// .docx/.xlsx/.xls/.pdf/.tex/.html/.md/.txt/.smcl）；estout 只在需要
// CSV/LaTeX 兼容旧脚本时才用（缺失时先 stata_install_package("estout", source="ssc")）
esttab m1 m2 using "results.csv", replace
```
