# 全流程审查报告：开发 → 部署 → 发布 → 用户安装

审查日期：2026-08-02
审查范围：只读，未修改任何源码文件。本报告即唯一产物。
核对对象：`mcp-stata-server/`（pyproject.toml / requirements.txt）、仓库根 `setup.py`、`README.md`、`npm-package/`（package.json / index.js / README.md / python/）、`server.json`、`.mcp.json.example`、`.github/workflows/lint.yml`、`publish.yml`、`.gitignore`、`CLAUDE.md`。

---

## 一、现状总览

双分发、双 remote、双 CI 工作流，核心发布链路**实测可用**：

- npmjs 上 `@aliveranme/stata-mcp` 当前为 **v1.0.8**（`npm view` 可查，`time.modified=2026-08-02T13:05Z`），说明「tag 即版本源 → npm publish」闭环在 v1.0.1–v1.0.8 之间至少跑通了一次。
- git 有 v1.0.0–v1.0.8 九个 tag；remote 双份：`github`（跑 Actions）、`origin`=Gitea（不跑 Actions，CLAUDE.md 所述属实）。
- 依赖下界一致：`fastmcp>=3.2.0` 在 `setup.py(FASTMCP_SPEC)`、`pyproject.toml`、`requirements.txt`、`npm-package/index.js` 四处零漂移。
- 本机 venv（uv 建）版本：fastmcp 3.4.4、mcp 1.28.1、ruff 0.16.0、pytest 9.1.1 —— 与 CI 钉的 ruff 0.16.0 一致。
- `server.json` 字段经 `https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json` 逐项核对合规（顶层 `name/description/version` 必填齐备；`packages[].registryType=“npm”`、`transport.type=“stdio”`、`environmentVariables[].format∈{string,filepath}` 均在 schema 枚举内）。
- 工具数 75 与 README 声称一致（`@mcp.tool` 全仓库计数 = 75）。

**结论：无 P0 阻断项**，但存在 7 项 P1 与一批 P2 —— 最值得立刻处理的是：CI 门禁当前实际是红的（format 检查）、发布物夹带 `.pyc`/日志、官方 MCP registry 条目疑似未上架、9 个 tag 只有 1 个 GitHub Release、LICENSE 缺失。

---

## 二、逐环节核对表

### A. 开发者侧（怎么跑测试 / lint / CI）

| 环节 | 文件/命令 | 现状 | 问题/风险 | 建议 |
|---|---|---|---|---|
| 单元测试 | `cd mcp-stata-server && .venv/bin/python -m pytest tests/ -q` | 本机可跑（venv 有 pytest 9.1.1） | `setup.py` 只装 fastmcp，`requirements.txt`（含 pytest/ruff）**没有任何文档要求安装**；新开发者 clone→setup.py 后直接跑测试 → `ModuleNotFoundError: pytest` | README「开发/调试」与 CLAUDE.md 补一行「先 `uv pip install -r requirements.txt`」 |
| E2E 测试 | `STATA_HOME=… pytest tests_e2e/ -q` | 需真 Stata，未装时整目录跳过（conftest 逻辑在） | CI 不含 E2E（合理，需真 Stata）；但**连无需 Stata 的单元测试也不在 CI**（见 P1-1） | 见 P1-1 |
| ruff lint | `ruff check server.py tool_modules/ tests/ tests_e2e/` | 本地通过 | CLAUDE.md 手动命令漏了 `stata_helpers.py`（CI 里有），README 里同样漏 | 统一三处命令文本 |
| **ruff format** | `ruff format --check server.py stata_helpers.py tool_modules/ tests/ tests_e2e/` | **当前是红的**：`tests_e2e/test_protocol_compliance.py` 会 reformat（实测） | 这是 CI 的独立 gate；本地手动命令（CLAUDE.md/README）**不含 format --check** → 开发者本地全绿、push 后 CI 挂 | 修该文件格式；把 format --check 补进文档化手动命令 |
| ruff 版本 | lint.yml 钉 `ruff==0.16.0` vs `requirements.txt`/`pyproject` 写 `>=0.5.0` | 本机恰为 0.16.0 | 新开发者装 requirements 会得到最新 ruff，格式规则可能漂移 → 本地过 CI 挂 | requirements 同步钉 `ruff==0.16.0`，或 CLAUDE.md 注明「先钉到与 CI 一致」 |
| CI 现状 | `.github/workflows/lint.yml`（ubuntu-latest × Python 3.12，步骤：ruff check + ruff format --check + compileall） | **无 pytest 步骤** | CLAUDE.md「已知局限」声称 CI「ubuntu-latest × py3.10/3.11/3.12 … 再跑 `pytest --cov`」——与实际**两处不符**：无版本矩阵、无 pytest | 修正 CLAUDE.md 描述（或按描述补矩阵与 pytest） |

### B. 部署/发布侧（publish.yml 全链）

| 环节 | 文件/命令 | 现状 | 问题/风险 | 建议 |
|---|---|---|---|---|
| 触发 | `on: push: tags: ['v*']` | 正确 | 无 lint/测试前置门禁：master 上哪怕 lint 红也能打 tag 发布 | 加 `needs: lint` 或 status-check 前置 |
| python→npm 同步 | `rm -rf python && cp server.py/stata_helpers.py + cp -r tool_modules` | 覆盖全部运行必需文件；`files` 字段（index.js/python/README）闭环 | 仓库里**双份 server.py 被 git 追踪**（npm-package/python/server.py）。当前 HEAD 两份一致（实测 diff 相同），但开发者改主份忘同步即静默漂移；`cp -r tool_modules` 会把源里 `__pycache__/` 一并复制 | 建议 `npm-package/python/` 不进 git，仅在发布时生成；至少 sync 用 `cp -r` 排除 `__pycache__` |
| 版本一致性 | `V="${GITHUB_REF_NAME#v}"` + node 写 package.json/server.json；下一 step 校验 mcpName 与版本 | **闭环可用**（npm 1.0.8 已发布验证）；`mcpName`、顶层版本、包内版本、校验 step 均以 tag 为源 | 版本**不回写仓库** → 仓库里的 package.json/server.json 永久停在 1.0.0（见 P1-5）；`env: V: ${{ github.ref_name }}` 与 shell 内 `V=…` 同名的写法依赖 bash「export 属性在赋值后保留」才不把 `v` 前缀写进版本，脆弱易误改 | 发布成功后把同步过的版本提交回 master（或至少在工作流头部注释说明该依赖） |
| 校验 | `npm pack --dry-run` + node 校验 | 步骤在 | **dry-run 只打印不含告警**：打包进了 `.pyc` 与日志文件也不会报错（见 P1-3） | 配合 .npmignore 后，在 dry-run 输出里 grep 断言无 `__pycache__/logs/` |
| npmjs.org | `npm publish --access public`（NPM_TOKEN） | v1.0.8 在线可查 ✓ | token 需 granular + bypass 2fa（注释已写明） | — |
| GitHub Packages | GITHUB_TOKEN + 顶层 `packages: write`，continue-on-error | 已按根因修复（403→packages:write；`.npmrc` scope 路由） | 首次发布需网页手动改 public；`.npmrc` 写进工作树是临时的 | 保留现状，注释已充分 |
| GitHub Release | `gh release create`，notes 取 `git log` 上一个 tag..本 tag | **9 个 tag 只有 v1.0.0 一个 Release**（`gh release list` 实测）；Release 步骤是**未提交**的新增（git diff 可见） | v1.0.1–v1.0.8 无 Release Notes；无 CHANGELOG 机制 | 补发历史 Release；提交该 workflow；可选：补 CHANGELOG.md 或靠 Release notes 兜底 |
| 官方 registry | `mcp-publisher` v1.8.0（钉死下载 URL）→ `login github-oidc` → `publish` | server.json 合规（见总览） | **`registry.modelcontextprotocol.io/servers/io.github.aliveranme/stata-mcp` 返回 404，无法确认已上架**（沙箱内 registry API 端点也不可达）；该步在 npm 发布之后，失败不会回滚已发布的 npm 版本 → 可能「npm 已发、registry 没发」而无人注意 | 人工在 registry 网站确认；在 publish.yml 该步加失败即告警（或本地复查 `mcp-publisher list`） |

### C. 用户安装侧（两条路径）

| 环节 | 文件/命令 | 现状 | 问题/风险 | 建议 |
|---|---|---|---|---|
| npm 路径 | `npx @aliveranme/stata-mcp` / `npx stata-mcp-server`（bin 名） | bin 正确；npm README 指引与 index.js 行为一致（有 uv → `uv run --with fastmcp>=3.2.0 --no-project python3 server.py` 自动装依赖；无 uv 需预装 fastmcp） | **主 README.md 对 npm 安装路径零提及**（grep `npx|@aliveranme|npm` 无结果）→ GitHub 上的用户不知道还有 npm 装法；两路径未互链 | 主 README 补「npm 安装」小节并链接 npm-package/README |
| index.js 健壮性 | `child.on('error')` 只处理 spawn 失败 | 子进程**非零退出**（如 fastmcp 缺失）时只透传裸 Python traceback，无友好提示 | npm README 已写前置条件，可接受；但体验粗糙 | 监听 `exit` 时若 code≠0 且 stderr 含 ModuleNotFoundError 给出中文指引 |
| setup.py | 跨平台检测（env > 常见路径 > 目录扫描）+ venv + 原子写 .mcp.json + 验证 | 健壮性好：STATA_HOME 无效有黄色警告、uv/pip 超时均捕获、`.mcp.json` 原子替换保留他 server 与自定义 env、edition 按平台特征文件检测 | `.mcp.json.example` 只提供 Windows 形态（`.venv/Scripts/python.exe`），macOS/Linux 用户照抄（README 手动段只说「编辑 <repo-path>」）会在 command 路径上挂 | 示例加平台注释或提供 bin 变体；README 手动段点明 |
| 两路径文档一致性 | README 一键安装 vs .mcp.json.example vs setup.py 生成 | 三处结构一致（command/args/env） | 同上，平台路径是唯一偏差 | 同上 |

### D. 版本与一致性

| 环节 | 现状 | 问题/风险 | 建议 |
|---|---|---|---|
| 本地 package.json / server.json = 1.0.0 vs registry 1.0.8 vs tag v1.0.8 | 功能上闭环（发布时从 tag 覆写），**仓库文件永久漂移** | 阅读源码无法得知当前发布版；本地直接 `npm publish` 会因版本已存在而失败（软护栏，不算坏） | 发布后自动回写 master（见 P1-5） |
| CHANGELOG / Release Notes | 无 CHANGELOG 文件；Release notes 依赖 publish.yml 的 gh 步骤（见上） | v1.0.1–v1.0.8 无任何发布说明 | 补发 Release + 可选 CHANGELOG |

### E. 缺失环节盘点

| 项 | 现状 | 判定 |
|---|---|---|
| LICENSE 文件 | 仓库**无 LICENSE 文件**；README/package.json/pyproject 三处声明 MIT | P1（见下） |
| .gitignore 覆盖 | 根 .gitignore 已盖 `.mcp.json`、`.venv`、`dist`、`mcp-stata-server/logs/`、`*.log` ✓ | 见下两条缺口 |
| `.agent_tmp` | 提交 e9d8411 标题「忽略 .agent_tmp」但**实际没加 ignore 规则**（`git check-ignore` 实测不忽略，git status 仍显示 untracked） | P2 |
| `npm-package/python/logs/` | 只被根 `.gitignore` 的 `*.log` 兜底；npm 打包时该规则不生效（见 P1-3） | P1 |
| Windows/macOS CI | 只有 ubuntu lint；无任何平台的真实测试 | P1-1 的一环 |
| E2E 进 CI | 不在（需真 Stata，合理）；但单元测试也不在（不合理） | P1-1 |
| npm postinstall 校验 | 无（薄启动器零依赖，可接受；但如上 index.js 退出提示弱） | P2 |
| 锁文件 | npm 包零依赖无需 lock ✓；Python 用 `>=` 下界（有意为之，与 ruff 漂移风险并存） | P2 |

---

## 三、问题清单（按严重度）

### P0 — 阻断
**无。** 核心发布链路（tag → npm publish → 用户 npx/setup.py）已实测走通（npm 1.0.8 在线）。

### P1 — 重要（推荐本轮修复）

1. **CI 不跑任何测试，且 CLAUDE.md 对 CI 的描述与实际不符。**
   lint.yml 只有 ruff（check/format）+ compileall，**没有 pytest 步骤**，连 mock 的单元测试都不跑；CLAUDE.md「已知局限」声称「py3.10/3.11/3.12 矩阵 + 跑 pytest --cov」。结果：质量门禁 = 仅语法与格式，回归测试全靠人肉。建议至少把 `pytest tests/ -q`（无需 Stata）加进 lint.yml，并修正 CLAUDE.md 文案。

2. **CI 的 format 门禁当前是红的。**
   实测 `ruff format --check tests_e2e/` 报 `test_protocol_compliance.py` 需 reformat（已提交文件，非工作树改动）→ 现在 push 到 GitHub 会让 lint.yml 失败。同时本地手动命令（CLAUDE.md/README）不含 `ruff format --check`，开发者本地全绿也拦不住。建议：修该文件格式 + 把 format --check 写进文档化手动命令。

3. **npm 发布物夹带 `__pycache__/*.pyc`（14 个）与 `python/logs/stata-mcp.log`。**
   `npm pack --dry-run` 实测：`npm-package/.gitignore` 里的 `__pycache__/` 对 npm-packlist **不生效**（目录型 gitignore 模式不被尊重），sync 的 `cp -r tool_modules` 又把源里的 `__pycache__` 带进来。发布物 = 源代码副本 + 平台/解释器特定字节码（cpython-313）+ 运行时日志，每次发布都会带。建议加 `.npmignore` 显式排除（`**/__pycache__/`、`*.pyc`、`logs/`），并让校验 step 在 dry-run 输出里断言不含这些路径。

4. **无 LICENSE 文件。** 三处声明 MIT，仓库内没有 LICENSE。对已发布 npm 包 + MCP registry 条目而言是合规/消费方确认的实缺口（npm publish 也会告警）。补一个标准 MIT LICENSE（作者 aliveranme）即可。

5. **版本永久漂移：仓库 package.json / server.json 停在 1.0.0，registry 已是 1.0.8。**
   发布流程从 tag 覆写版本但不回写仓库，仓库阅读者无从得知当前发布版。功能不阻断（tag 即版本源已闭环），但建议发布成功后把同步结果提交回 master，或在工作流头部写明「版本源是 git tag，仓库文件不维护实际版本」。

6. **9 个 tag 只有 1 个 GitHub Release（v1.0.0）。**
   `gh release list` 实测仅 v1.0.0；Release 步骤是 publish.yml 里**尚未提交**的新增（git diff 可见），v1.0.1–v1.0.8 发布时该步骤不存在 → 历史版本无 Release Notes、无 CHANGELOG。建议：提交该 workflow，并补发历史 tag 的 Release（或至少 v1.0.8）。

7. **官方 MCP registry 条目疑似未上架。**
   `registry.modelcontextprotocol.io/servers/io.github.aliveranme/stata-mcp` 返回 404（沙箱内 registry API 也不可达，无法百分百定论，但规范 URL 404 是强信号）。mcp-publisher publish 在 npm 发布之后，失败不回滚 npm 版本 → 可能出现「npm 已发、registry 没发」而流程显示绿。建议：人工到 registry 网站确认；确认缺失则重发，并给该步加失败告警。

### P2 — 建议（后续批次）

- **新开发者缺 dev 依赖安装入口**：setup.py 只装 fastmcp；README/CLAUDE.md 无「安装 requirements.txt」步骤 → clone 后跑 pytest 直接失败。补一行文档。
- **ruff 版本漂移风险**：requirements/pyproject 写 `>=0.5.0`，CI 钉 `==0.16.0`；新开发者装出最新 ruff 可能格式不一致。requirements 钉版本。
- **CLAUDE.md 手动 lint 命令漏 `stata_helpers.py`**（CI 有），且漏 format --check（见 P1-2）。
- **`.agent_tmp` 声称忽略实际未忽略**：e9d8411 只删了文件没加 ignore 规则。
- **主 README 无 npm 安装路径**（见 C 环节）。
- **`.mcp.json.example` 仅 Windows 形态**（Scripts/python.exe）。
- **publish.yml 无 lint/测试前置门禁**：打 tag 即发布，master 红也能发。
- **index.js 子进程非零退出无友好提示**（fastmcp 缺失时是裸 traceback）。
- **版本同步的 `V` 变量写法脆弱**：`env: V: ${{ github.ref_name }}` + shell 内 `V="${GITHUB_REF_NAME#v}"` 同名覆盖，靠 bash「已 export 变量在赋值后仍 export」才不把 `v` 写进版本——换 sh 或重构即踩坑。
- **npm-package/python/server.py 双份被 git 追踪**：主份改动后忘同步即静默漂移（当前 HEAD 两份一致，风险在过程）。
- **mcp-publisher 下载 URL 钉死 v1.8.0**：建议定期核对，或用 release tag 变量。

---

## 四、优先级建议（按投入/收益排序）

1. **修 format 红（P1-2，10 分钟）**：`ruff format tests_e2e/test_protocol_compliance.py`，并提交 workflow 未提交部分。否则 CI 持续红。
2. **修 npm 发布物（P1-3，30 分钟）**：npm-package 加 `.npmignore`（`**/__pycache__/`、`*.pyc`、`logs/`），publish.yml 的 sync 改 `cp -r` 排除 `__pycache__`，校验 step 断言 dry-run 无这些路径。这次 1.0.9 就能生效。
3. **补 LICENSE（P1-4）与补发 GitHub Release / 提交 workflow（P1-6）**：纯增量、无风险。
4. **确认/重发官方 registry 条目（P1-7）**：先人工核实，缺失则重发 v1.0.8。
5. **CI 加单元测试 + 修正 CLAUDE.md CI 描述（P1-1）**：`pytest tests/ -q` 无需 Stata，直接进 lint.yml。
6. **版本回写 master（P1-5）**：发布成功后把 package.json/server.json 的版本提交回 master，消除永久漂移。
7. **P2 批**：文档补齐（dev 依赖安装、npm 路径、format 命令、platform 示例）、.agent_tmp 规则、ruff 钉版本、publish 前置门禁。

---

## 附：核对中确认的正面项（无需动作）

- `fastmcp>=3.2.0` 四处声明一致；本机 venv 的 fastmcp/mcp/ruff 与代码要求吻合。
- `server.json` 符合 2025-12-11 MCP registry schema（字段与枚举逐项核对）。
- `setup.py` 的健壮性改造（STATA_HOME 警告、超时捕获、`.mcp.json` 原子替换、跨平台 edition 检测、验证子进程）到位且自洽。
- 工具数 75 与文档一致；README 与 CLAUDE.md 的架构/工具表/安全设计基本一致。
- `.gitignore` 已覆盖 `.mcp.json`、`.venv`、`dist`、`mcp-stata-server/logs/`、`*.log`、`*.dta` 等主要生成物。
- GitHub Packages 步骤的根因注释（packages:write + 手动改 public）准确，非阻断设计合理。

---

## 修复状态（2026-08-02 审查当日跟进）

审查发现的问题已跟进，对照「四、优先级建议」编号：

| 问题 | 状态 |
|------|------|
| P1-2 format 红 | ✅ 已修：`ruff format tests_e2e/test_protocol_compliance.py`，全量 `ruff format --check` 44 文件绿 |
| P1-3 npm 发布物夹带脏文件 | ✅ 已修：publish.yml 同步步骤源头排除 `__pycache__`/`logs`，`npm pack --dry-run` 实测噪音 0。注：`.npmignore` 在 `files` 白名单下实测无效（npm 11），故弃用、改源头排除 |
| P1-4 缺 LICENSE | ✅ 已补 MIT LICENSE |
| P1-5 版本永久漂移 | ✅ 部分修复：本地 package.json/server.json/pyproject/server.py 对齐 1.0.8；publish.yml 版本同步新增 server.py `version=` 自动改写 + 校验步骤（tag 即版本源，发布产物 serverInfo.version 恒正确）。**未做**「发布后回写 master」——推送失败会污染已发布半成品，留作可选改进 |
| P1-6 历史 Release 缺失 | ✅ publish.yml 新增 `gh release create` 步骤并已提交；历史 tag（v1.0.1–v1.0.8）需人工补发 |
| P1-1 CI 零测试 | ⏸ 未改：用户明确将 test 流水线重命名为语法+格式化检查；`pytest tests/ -q` 进 CI 留作可选建议（tests_e2e 需真 Stata，无法进 CI） |
| P1-7 registry 上架确认 | ⏸ 需人工在 MCP registry 网站核实后重发 v1.0.8 |
| P2 .agent_tmp 规则 | ✅ 已修：mcp-stata-server/.gitignore 改为 `.agent_tmp/`（原写 `mcp-stata-server/.agent_tmp/` 在子目录内不命中） |
| P2 ruff 钉版本 | ✅ 已修：pyproject/requirements 统一 `ruff==0.16.0` 与 CI 一致 |
| P2 文档补齐 | ✅ 主 README 补 npm 公开安装路径；CLAUDE.md 本地命令对齐 lint.yml（含 format --check） |
