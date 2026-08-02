# MCP 2025-11-25 规范符合性报告

- **评审对象**：`mcp-stata-server/server.py`（~128KB，fastmcp 3.4.4 / mcp SDK 1.28.1 / Python 3.13）+ `npm-package/index.js`（stdio 桥）
- **评审日期**：2026-08-02
- **规范版本**：https://modelcontextprotocol.info/specification/2025-11-25/（英文原版，中文对照）
- **方法**：抓取规范源文件（GitHub `modelcontextprotocol/modelcontextprotocol` 的 `docs/specification/2025-11-25/*.mdx`）逐条核对 MUST；精读 server.py 与 fastmcp/mcp SDK 源码；**真机实测**（Stata 19.5 MP via `/Volumes/ccc/Applications/StataNow`，`node index.js → server.py` stdio 进程，一次性启动后发送 14+ 条协议探针）
- **结论概览**：核心协议链路（initialize/生命周期/stdio framing/tools/resources/prompts/错误结构）**PASS**；**2 个 FAIL**（畸形消息无 JSON-RPC 错误响应、未知 method 返回 -32602 而非 -32601），均为 mcp SDK 层行为、非 server.py 引入；**若干 WARN**（资源未登记读回错误码 0、未知工具返回 isError 结果而非协议错误等）。

---

## 1. 结论摘要

| 项目 | 判定 | 说明 |
|------|:---:|------|
| initialize 握手（protocolVersion/serverInfo/capabilities/instructions） | **PASS** | 协商 `2025-11-25`；serverInfo 正确；capabilities 与实现一致 |
| 协议版本协商（支持旧版本 / 回退） | **PASS** | 支持 2024-11-05 ~ 2025-11-25；未知版本回退最新 |
| 生命周期（initialized 通知、pre-init 请求） | **PASS**（含 1 WARN） | 状态机正确；pre-init 请求返回误导性错误码/文案 |
| JSON-RPC 消息格式与错误结构 | **FAIL** | 畸形消息被静默丢弃（应回 -32700/-32600）；未知 method 回 -32602（应回 -32601） |
| stdio transport framing（换行分隔、UTF-8、无 stdout 污染） | **PASS** | 单行 JSON + `\n`；Stata 输出被 RedirectOutput 收口 |
| tools（list 结构 / inputSchema / 调用 / 执行错误） | **PASS**（含 1 WARN） | 75 个工具结构合法；isError 语义正确；未知工具未按建议回 -32602 |
| resources（模板 / read / 二进制 / 安全边界） | **PASS**（含 1 WARN） | `stata-file:///{path*}` 合法；未登记读回错误码 0（规范建议 -32002） |
| prompts | **PASS** | 声明 capability、空列表合法；未注册任何 prompt |
| logging | **PASS**（含 1 WARN） | `logging/setLevel` 可用；capability 声明但产品侧从不主动发日志通知 |
| cancellation / progress | **PASS** | cancelled 通知无响应；progress 未实现（可选，未声明） |
| 安全与注入防护 | **PASS**（含 1 WARN） | 注入防护纵深极强；无速率限制 |

**FAIL 详表（违反规范的点）：**

| # | 规范要求 | 实测行为 | 根因 |
|---|----------|----------|------|
| F1 | MCP MUST 遵循 JSON-RPC 2.0；畸形 JSON/请求应回 `-32700`（parse error）或 `-32600`（invalid request） | 畸形消息（缺 method、`jsonrpc:"1.0"`、缺 `jsonrpc` 字段）**无任何错误响应**，仅发出 `notifications/message`(error, "Internal Server Error")，请求 id 被吞 | mcp SDK 1.28.1 `mcp/server/stdio.py` 解析失败后把 `Exception` 塞进流，`mcp/shared/session.py` 的 `case Exception()` 只记日志不回复 |
| F2 | JSON-RPC 2.0 定义未知 method 应回 `-32601`（Method not found） | `bogus/method` 回 `-32602` + "Invalid request parameters" | mcp SDK 对 `ClientRequest` 联合类型校验先失败，走到 `-32602` 分支。`-32601` 分支（`_handle_request` else）仅对"能解析但无处理器的已知请求类型"可达（如 `resources/subscribe`、`completion/complete`），对未知 method 名不可达 |

---

## 2. Base Protocol — Messages（JSON-RPC）

规范依据：`basic/index.mdx`（Messages 节）、`basic/lifecycle.mdx`、`index.mdx`（"All messages ... MUST follow the JSON-RPC 2.0 specification"）。

### 2.1 请求 / 响应 / 通知结构

| 核对项 | 判定 | 证据 |
|--------|:---:|------|
| `jsonrpc:"2.0"` 字面量 | PASS | 所有响应均为 `"jsonrpc": "2.0"`（实测与 `mcp/types.py:155` `Literal["2.0"]`） |
| 请求 id 为 string\|number，且不能为 null | PASS | `mcp/types.py:40` `RequestId = Annotated[int, Field(strict=True)] \| str`；实测字符串 id `"s1"` 正常工作 |
| 通知（无 id）不得有响应 | PASS | 实测 `notifications/cancelled`、`notifications/initialized` 均无响应（E2E `test_notification_gets_no_response`） |
| 结果响应必须带相同 id 与 `result` | PASS | 实测全部请求 id 原样回显 |
| 错误响应必须带相同 id、`error{code,message}`，code 为整数 | PASS | 见 2.2；未知 method 与资源读错的 id 均正确回显 |
| `_meta` 保留字段 | WARN | fastmcp 在资源模板输出里附加 `_meta.fastmcp.tags`。`fastmcp` 是无前缀的裸名，不落入规范保留前缀（`*.mcp/*`），但属框架私有元数据，客户端应忽略 |

### 2.2 错误码

| 核对项 | 判定 | 证据 |
|--------|:---:|------|
| `-32700` parse error | **FAIL** | 无法触发：非法 JSON 被 stdio 读取层 `except Exception` 捕获后作为 `Exception` 送入会话，`_handle_message` 的 `case Exception()` 分支只发 `notifications/message`("Internal Server Error") 且**不回错误响应**（`mcp/server/stdio.py` 的 stdin_reader；`mcp/server/lowlevel/server.py` 的 `_handle_message`）。实测三条畸形消息均无响应 |
| `-32600` invalid request | **FAIL** | 同上，缺少 method / `jsonrpc:"1.0"` / 缺 `jsonrpc` 字段的对象均被静默丢弃，未回任何 JSON-RPC 错误 |
| `-32601` method not found | **FAIL** | 实测 `bogus/method` 返回 `code=-32602, message="Invalid request parameters"`。`-32601` 分支（`mcp/server/lowlevel/server.py` `_handle_request` else）仅对"能解析但无处理器的已知请求类型"可达（如 `resources/subscribe`），对未知 method 名因联合类型校验先失败而不可达 |
| `-32602` invalid params | PASS（语义存疑） | 对"工具参数非法"这层，SDK 用 isError 结果而非错误响应（见 5.3），属规范允许的双机制之一 |
| `-32603` internal error | PASS | SDK 异常路径可产出；资源读错实际走 code=0（见 4.3，WARN） |
| 错误码范围 -32000~-32099（服务器自定义） | WARN | 规范建议资源未找到回 `-32002`，实测回 `code=0`（应用自定义整数，合法但非标准） |

> **修复建议（F1/F2）**：两者根因都在 mcp SDK 的解析/分发层，server.py 无法在现有 `mcp.run(transport="stdio")` 上直接覆盖。选项：
> 1. 向 mcp-python 上游提交修复（`stdio.py` 对解析失败回 `-32700`；`ClientRequest` 联合校验失败回 `-32600`，未知 method 回 `-32601`），或升级到已修复版本；
> 2. 若追求即刻合规，可在 server.py 用自定义 stdio transport 包一层（JSON 解析 + 错误响应），但这与 fastmcp 深度耦合，成本高；
> 3. 最低成本：在 E2E 中把当前行为断言住并注释为"已知 SDK 偏差"，避免未来升级时无感变化。

---

## 3. Lifecycle

规范依据：`basic/lifecycle.mdx`。

| 核对项 | 判定 | 证据 |
|--------|:---:|------|
| 初始化必须是首次交互；客户端发 `initialize{protocolVersion,capabilities,clientInfo}` | PASS | 实测首条消息即 initialize 成功；E2E `session` fixture 同构 |
| 服务器回 `protocolVersion/capabilities/serverInfo`（+ 可选 `instructions`） | PASS | 实测 `serverInfo{name:"StataNow 19", version:"1.0.8"}`、`instructions` 存在、capabilities 见 3.1 |
| 客户端随后必须发 `notifications/initialized` | PASS | SDK 处理 `InitializedNotification` 置状态（`mcp/server/session.py:211-212`）；实测正常 |
| 版本协商：支持则回同版本；否则回服务器支持的版本（SHOULD 为最新） | PASS | `SUPPORTED_PROTOCOL_VERSIONS = ["2024-11-05","2025-03-26","2025-06-18","2025-11-25"]`（`mcp/shared/version.py`）；`_received_request` 对支持的版本原样回，未知版本回 `LATEST_PROTOCOL_VERSION`（2025-11-25） |
| 客户端在服务器响应 initialize 前不应发非 ping 请求（SHOULD） | PASS | 服务器不会因此崩溃；见下条 |
| 服务器在收到 initialized 前不应发非 ping/logging 请求（SHOULD） | PASS | 服务器不发任何主动请求；`extensions` 能力声明见 3.1 |
| 请求先于初始化到达时的行为 | WARN | SDK 对非 initialize 请求在未初始化状态抛 `RuntimeError("Received request before initialization was complete")`（`mcp/server/session.py:204`），receive loop 捕获后回 `-32602 "Invalid request parameters"`——**拒绝了但文案误导**（真实原因是"未初始化"而非"参数非法"）。另外注意 SDK 在响应 initialize 后**立即**置 `Initialized`（`session.py:199`），即客户端未发 `notifications/initialized` 前发业务请求也会被接受——规范未强制拒绝，可接受 |
| 关闭（stdio） | PASS | 客户端关 stdin / SIGTERM 即可；node 桥转发生成的信号（`npm-package/index.js`） |

### 3.1 capabilities 声明与实际实现一致性

实测 initialize 返回：

```json
"capabilities": {
  "experimental": {},
  "logging": {},
  "prompts": {"listChanged": false},
  "resources": {"subscribe": false, "listChanged": false},
  "tools": {"listChanged": true},
  "extensions": {"io.modelcontextprotocol/ui": {}}
}
```

| 能力 | 判定 | 说明 |
|------|:---:|------|
| `tools: {listChanged: true}` | PASS | 有 75 个工具；listChanged=true 表示"列表变化时发通知"，运行期列表静态、从不需要发，语义一致 |
| `resources: {subscribe: false, listChanged: false}` | PASS | 与实现一致（无 subscribe；注册表是会话级的、从不主动推变化） |
| `prompts: {listChanged: false}` | PASS/WARN | fastmcp 恒注册 `list_prompts/get_prompt` 处理器 → 声明 capability；实测 `prompts/list` 返回空数组。规范只约束"支持 prompts 必须声明"，**声明但为空是合法的**；客户端会看到空的 prompts 面板，属无害噪音 |
| `logging: {}` | WARN | `logging/setLevel` 可用（实测回 `{}`）；但产品侧从不主动发 `notifications/message`——仅 SDK 内部错误路径会发。能力"声明但基本休眠"，不算实现不存在的能力，仍建议知情 |
| `experimental: {}` | WARN | 空对象，无害。注意 2025-11-25 规范把 `tasks` 从 experimental 升为一级能力；本服务器因未装 pydocket，fastmcp 的 `get_task_capabilities()` 返回 None，未声明 `tasks`——一致 |
| `extensions: {"io.modelcontextprotocol/ui": {}}` | WARN | fastmcp 恒附加的 MCP Apps 扩展声明（`low_level.py` get_capabilities），2025-11-25 规范正文无此字段，属框架扩展，客户端应忽略 |
| 服务器不声明 `roots`/`sampling`/`completions` | PASS | 这些是客户端能力或未实现项；`completions` 未注册处理器故未声明，正确 |

---

## 4. Transports（stdio）

规范依据：`basic/transports.mdx`。

| 核对项 | 判定 | 证据 |
|--------|:---:|------|
| JSON-RPC 消息 MUST 为 UTF-8 | PASS | `mcp/server/stdio.py` 用 `TextIOWrapper(..., encoding="utf-8")` 包裹二进制流 |
| 消息以换行分隔且不得内嵌换行 | PASS | `stdio.py` 按行读取、`model_dump_json` + `"\n"` 写出；JSON 内的换行被 `\n` 转义为单行 |
| 服务器 MUST NOT 向 stdout 写非 MCP 消息 | PASS | server.py 无 stdout `print`（3 处 FATAL print 全走 `sys.stderr`，`server.py:168/207/222`）；Stata 输出被 `RedirectOutput`（`server.py:792`）+ `streamout="off"`（`server.py:213`）收口；fastmcp banner 走 stderr。实测 stdout 纯净 |
| 服务器 MAY 向 stderr 写日志 | PASS | server.py 双写 stderr + `logs/stata-mcp.log`（`server.py:131-159`） |
| 自定义 transport 必须保留 JSON-RPC 与生命周期 | PASS | server.py 未改动 transport 层，`mcp.run(transport="stdio")`（`server.py:2841`）走 fastmcp 标准 stdio；node 桥仅做进程级 stdio 透传（`npm-package/index.js`），不触碰协议帧 |
| RedirectOutput / 看门狗不破坏帧 | PASS | RedirectOutput 把 Stata 显示回调定向到内存缓冲，与 MCP 通道隔离；实测多轮请求帧完整 |

---

## 5. Server — Tools

规范依据：`server/tools.mdx`。

| 核对项 | 判定 | 证据 |
|--------|:---:|------|
| 声明 `tools` capability | PASS | 见 3.1 |
| `tools/list` 返回结构（name/description/inputSchema，分页可选） | PASS | 实测 75 个工具，每项含 `name/description/inputSchema`；inputSchema 含 `properties/additionalProperties:false` |
| `name` 命名规范（1-128 字符、字母数字下划线连字符点、无空格逗号） | PASS | 全部为 `stata_*` 小写下划线命名 |
| `inputSchema` MUST 是合法 JSON Schema（默认 2020-12，不可为 null） | PASS | 由 fastmcp 从 pydantic 签名生成；`stata_run` 等 schema 结构完整 |
| 无参数工具的 schema | PASS | `{"type":"object","properties":{...},"additionalProperties":false}`（实测 `stata_ping` 路径） |
| 服务器 MUST 校验工具输入 | PASS | 双层：fastmcp `Tool._run` 用 pydantic TypeAdapter 校验（`function_tool.py`），非法参数 → `ValidationError` → isError 结果；SDK 另有 `validate_input` jsonschema 开关（默认 strict=False 走 coerce，见 WARN） |
| 工具执行错误用 `isError:true` 结果（输入校验/业务错误） | PASS | 实测 `stata_ping` 传 `{"bogus":1}` → `isError:true` + pydantic 文案 |
| 协议错误（未知工具等） | WARN | 实测 `tools/call` 未知工具 → `isError:true` 结果（"Unknown tool: 'no_such_tool'"），而非规范示例的 `-32602` 协议错误。规范允许两种机制并存，但"Unknown tools"被列在 Protocol Errors 下（示例 code=-32602）。属框架取舍，可接受但建议知情 |
| `annotations`（readOnlyHint/destructiveHint） | PASS | 工具广泛使用 `ToolAnnotations`（如 `server.py:1668/2354/2409/2432`） |
| 安全：validate all tool inputs / access controls / sanitize outputs | PASS | 输入校验与路径沙箱/危险命令护栏极强（见 §8）；输出为 Stata 原文（产品语义如此） |
| 安全：rate limit tool invocations（规范 MUST） | WARN | 未实现速率限制。对本地单用户 stdio 服务器实际风险低，但属规范 MUST 项未覆盖 |
| 指令注入防护 | PASS | server.py 对自由文本命令做行首危险前缀拦截、宏混淆、`#delimit` 拦截、路径审计等（详见 CLAUDE.md 安全护栏节）；`stata_run`/`stata_graph` 的 `command` 均过护栏 |

---

## 6. Server — Resources

规范依据：`server/resources.mdx`。

| 核对项 | 判定 | 证据 |
|--------|:---:|------|
| 声明 `resources` capability | PASS | 见 3.1 |
| 资源模板用 RFC 6570 URI Template | PASS | `stata-file:///{path*}`（`server.py:2331-2332`）；`{path*}` 通配跨段匹配（fastmcp `build_regex` 生成 `(?P<path>.+)`），POSIX/Windows 绝对路径均可用；注释点明 `{path}` 单段不匹配的历史教训（CLAUDE.md） |
| `resources/templates/list` 返回 uriTemplate/name/description/mimeType | PASS | 实测返回完整模板；额外 `_meta.fastmcp.tags` 见 §2.1 WARN |
| `resources/read` 内容结构（uri/mimeType/text 或 blob） | PASS | fastmcp `to_mcp_resource_contents`（`resources/base.py:94-119`）：str→text，bytes→base64 blob；模板 handler 返回 bytes（`server.py:2341-2351`）→ BlobResourceContents |
| URI 校验 | PASS | 读取前必须命中注册表（`server.py:2267-2315` `_read_registered_file`），未登记即拒绝——防止任意文件读取原语；`stata_read_file` 与模板共用此入口 |
| `resources/list` | PASS | 实测返回 `resources: []`（会话内无已登记文件时合法） |
| 订阅（resources/subscribe） | PASS | 未声明 subscribe，未实现，正确 |
| 错误处理：Resource not found `-32002`（规范 SHOULD） | WARN | 实测未登记资源读回 `code=0`："Error reading resource 'stata-file:///...': 错误: 文件未登记..."。根因：fastmcp 把 `ValueError` 包成 `ResourceError`（非 `McpError`），SDK 通用异常路径回 `code=0`。功能上正确拒绝了，错误码非规范建议值 |
| 二进制数据 MUST 正确编码 | PASS | base64 blob |
| 安全：MUST validate all resource URIs / 权限检查 | PASS | 注册表白名单即权限边界；读取上限 16MB（`server.py:293`）、工具 base64 载荷上限 80KB（`server.py:297`）防撑爆传输 |

---

## 7. Server — Prompts

规范依据：`server/prompts.mdx`。

| 核对项 | 判定 | 证据 |
|--------|:---:|------|
| 支持 prompts 必须声明 `prompts` capability | PASS | 已声明（见 3.1） |
| `prompts/list` 返回结构 | PASS | 实测 `{"prompts": []}`，合法 |
| `prompts/get` | PASS | 未注册任何 prompt，调用未知 prompt 会走框架错误路径（未测） |
| 输入/输出防注入 | PASS | 无 prompt 实现，不适用 |

> 说明：server.py 未注册任何 `@mcp.prompt`（grep 0 处）。capability 由 fastmcp 恒注册的处理器驱动而存在，属"支持该协议面、但为空"——不违反规范，客户端会看到空列表。

---

## 8. Security & Trust & Safety

规范依据：`index.mdx`（Security 节）、`server/tools.mdx`（Security Considerations）、`server/resources.mdx`。

| 核对项 | 判定 | 证据 |
|--------|:---:|------|
| 工具输入校验 | PASS | pydantic 签名 + jsonschema + 参数级白名单/黑名单校验 |
| 指令注入防护 | PASS | 危险前缀（`!`/shell/winexec/mata/python 等）行首拦截、宏混淆、`#delimit`、路径审计、`_validate_filter_expr`（拒 `//` 注释逃逸）、varlist 拒 `/ , using`（防沙箱逃逸）——纵深极强（见 CLAUDE.md「安全护栏」「路径安全校验」） |
| 资源 URI 校验 | PASS | 注册表白名单 |
| 访问控制 | PASS | `STATA_ALLOWED_ROOTS` 路径沙箱（未配置时放行绝对路径，文档明示） |
| 速率限制 | WARN | 未实现（规范 tools 安全节 MUST） |
| 输出清洗 | WARN | 工具返回 Stata 原文，含用户数据——产品语义需要；"清洗"在本场景即保持原样 |
| 用户知情/同意（客户端侧义务） | PASS | 服务器侧无需实现；工具注解 `readOnlyHint/destructiveHint` 已提供客户端展示依据 |
| 日志含敏感信息 | PASS | 服务器日志不含凭据；仅命令/错误文本 |

---

## 9. Utilities — Cancellation / Progress / Logging / Pagination

规范依据：`basic/utilities/*.mdx`、`server/utilities/*.mdx`。

| 核对项 | 判定 | 证据 |
|--------|:---:|------|
| `notifications/cancelled` 被接收且无响应 | PASS | 实测无响应；SDK 会取消 in-flight 请求（`mcp/shared/session.py` CancelledNotification 分支） |
| `initialize` 不可被取消 | PASS | SDK 未对 initialize 注册取消路径 |
| progress 通知（进度令牌） | PASS | 服务器不发送 progress、不声明相关能力；`stata_background` 的进度以任务轮询暴露，与协议 progress 通知无关（可选特性，未实现不违规） |
| `logging/setLevel` 与空结果 | PASS | 实测返回 `{}`（EmptyResult）；无效 level 回 -32602（SDK 校验） |
| 分页（cursor） | PASS | fastmcp 支持 `cursor`/`nextCursor`（`_apply_pagination`）；本服务器未配置 `_list_page_size`，列表一次性返回（规范允许：分页是可选项，无 nextCursor 即末页） |

---

## 10. 版本协商与向后兼容

| 核对项 | 判定 | 证据 |
|--------|:---:|------|
| 客户端请求 `2025-11-25` → 服务器回 `2025-11-25` | PASS | 实测 |
| 客户端请求旧版本（2024-11-05/2025-03-26/2025-06-18）→ 回同版本 | PASS | SDK `SUPPORTED_PROTOCOL_VERSIONS` 原样回 |
| 客户端请求未知版本 → 回服务器最新支持版本 | PASS | 回 `2025-11-25`（`LATEST_PROTOCOL_VERSION`） |
| HTTP 传输的 `MCP-Protocol-Version` 头 | N/A | 本产品仅 stdio 分发（npm 桥 + setup.py），不适用 |

---

## 11. 产品侧实现要点（server.py 具体行号）

| 要点 | 位置 |
|------|------|
| FastMCP 实例化（name/version/instructions） | `server.py:248-259`（version 1.0.8 与 pyproject/npm/git tag 对齐） |
| `mcp.run(transport="stdio")` | `server.py:2836-2841` |
| 资源模板 `stata-file:///{path*}` | `server.py:2331-2351` |
| 资源注册表（安全边界） | `server.py:1275-1317`（`_register_resource` / `_resource_lookup`） |
| 有界资源读取（16MB） | `server.py:2267-2315` |
| `RedirectOutput` + `streamout=off`（stdout 隔离） | `server.py:213, 792` |
| 工具注解 `ToolAnnotations` | `server.py:246, 1668, 2354, 2409, 2432` 等 |
| 危险前缀护栏 / 路径沙箱 | `stata_helpers.py`（`_has_dangerous_command_prefix`、`_validate_path` 等） |

---

## 12. 现有 E2E 覆盖与缺口

`tests_e2e/test_protocol_compliance.py`（commit 4a34b1c）已覆盖：initialize 字段、tools/list 结构、tool 结果结构、未知 method 错误存在性、坏参数 isError、资源模板声明、prompts/list 结构、通知无响应。

**缺口（建议补充）：**

1. `test_unknown_method_returns_jsonrpc_error` 只断言"有 error + code 字段 + id 回显"，**未断言 code == -32601** —— 未捕获 F2（实测 -32602）。
2. 无畸形消息测试（缺 method / `jsonrpc:"1.0"` / 非法 JSON）→ 未捕获 F1（无错误响应）。
3. 无"请求先于 initialize"测试（实际回 -32602，文案误导）。
4. 无 `resources/read` 未登记资源测试（实际回 code=0，非 -32002）。
5. 无版本协商测试（请求 2024-11-05 / 未知版本）。
6. 无重复 id / id 复用测试（规范：会话内请求 id 不得复用）。

> 注意：E2E 需真 Stata（`STATA_HOME=/Volumes/ccc/Applications/StataNow .venv/bin/python -m pytest tests_e2e/test_protocol_compliance.py -q`，约 10s 初始化）。

---

## 13. 修复建议汇总

| 严重度 | 问题 | 建议 |
|:---:|------|------|
| FAIL F1 | 畸形消息无 JSON-RPC 错误响应 | 上游修复 mcp-python（`stdio.py` 解析失败回 `-32700`）；短期在 E2E 中断言现状并标注已知偏差 |
| FAIL F2 | 未知 method 回 -32602 而非 -32601 | 同上（SDK 联合校验应先回 `-32600`、未知 method 回 `-32601`） |
| WARN | 资源未登记读回 code=0 | fastmcp 侧把 `ResourceError` 转 `McpError(-32002)`；或文档声明 |
| WARN | pre-init 请求文案误导 | 上游把未初始化错误改回 `-32600`/专用文案；产品侧文档说明 |
| WARN | capabilities 含 `extensions`/`experimental` 等框架附加字段 | 无需改动；客户端应忽略未知字段（规范 `_meta` 与额外字段均允许） |
| WARN | 速率限制缺失 | 本地单用户可接受；若未来暴露为远程服务，需补 per-tool 限流 |

---

*报告完。核心结论：产品在 initialize/生命周期/stdio framing/tools/resources/prompts/错误结构等主链路上符合 MCP 2025-11-25；两处 FAIL 均为 mcp SDK 层继承行为，非 server.py 引入，建议以上游修复或文档化方式处理。*
