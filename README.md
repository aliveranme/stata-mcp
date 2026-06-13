# Stata MCP Server + Skill for Claude Code

让 AI Agent 完全自动化地撰写并执行 Stata 命令 — 通过 MCP Server 提供执行能力，Skill 提供 Stata 编程知识。

[![Stata Version](https://img.shields.io/badge/Stata-Now%2019.5%20MP-blue)](https://www.stata.com)
[![Python](https://img.shields.io/badge/Python-3.10+-green)](https://python.org)
[![MCP](https://img.shields.io/badge/MCP-stdio-orange)](https://modelcontextprotocol.io)

## 架构

```
┌──────────────────────────────────────────────────┐
│                  Claude Code Agent                │
│  ┌──────────────┐          ┌──────────────────┐  │
│  │  stata Skill │◄────────►│  stata MCP Server │  │
│  │  (知识层)     │  指导     │  (执行层)          │  │
│  │  - 语法规范   │          │  - 22 个工具       │  │
│  │  - 分析模板   │          │  - pystata 直接调用 │  │
│  │  - 常见陷阱   │          │  - StataNow 19.5 MP │  │
│  └──────────────┘          └──────────────────┘  │
└──────────────────────────────────────────────────┘
```

## 前置条件

- **Windows** 操作系统
- **StataNow 19** 或 **Stata 18+**（MP / SE / BE 版本均可）
- **Python 3.10+**（推荐 3.12+）
- **Claude Code**（最新版本）

## 快速开始

### 1. 克隆并运行安装脚本

```bash
git clone https://gitea.aliveranme.space/aliveranme/stata-mcp.git
cd stata-mcp
python setup.py
```

`setup.py` 自动完成：
1. 检测 Stata 安装路径（检查常见目录 + 环境变量 `STATA_HOME`）
2. 创建 Python 虚拟环境并安装 `fastmcp`
3. 生成 `.mcp.json` 配置文件（含正确路径）
4. 验证 MCP Server 可正常启动

### 2. 手动安装（备选）

如果自动检测失败，先设置环境变量再运行：

```bash
# 设置 Stata 路径
set STATA_HOME=D:/StataNow19
set STATA_EDITION=mp

# 创建虚拟环境
cd mcp-stata-server
uv venv

# Windows Git Bash / MSYS2:
source .venv/Scripts/activate
# Windows PowerShell:
# .venv\Scripts\Activate.ps1
# Windows CMD:
# .venv\Scripts\activate.bat

uv pip install fastmcp

# 复制并编辑 .mcp.json
cd ..
cp .mcp.json.example .mcp.json
# 编辑 .mcp.json，将 <repo-path> 替换为实际路径
```

### 3. 连接 Claude Code

重启 Claude Code（或运行 `/reload-plugins`），`.mcp.json` 中配置的 `stata` MCP Server 会自动连接。

### 4. 验证

在 Claude Code 中输入：

> 帮我加载 auto.dta 数据并做描述统计

Agent 应自动使用 `stata_use_dataset` → `stata_describe` → `stata_summarize` 完成分析。

## MCP 工具列表

### 数据管理
| 工具 | 说明 | destructiveHint |
|------|------|:---:|
| `stata_use_dataset` | 加载 .dta 数据文件 | ✓ |
| `stata_save_dataset` | 保存数据为 .dta | ✓ |
| `stata_set_cwd` | 更改工作目录 | ✓ |

### 数据探索
| 工具 | 说明 |
|------|------|
| `stata_describe` | 变量基本信息（类型、标签） |
| `stata_codebook` | 详细变量字典（值标签、分布） |
| `stata_summarize` | 描述统计量（均值、标准差等） |
| `stata_list` | 查看数据值 |
| `stata_tabulate` | 频数表 / 交叉表 |
| `stata_display` | 表达式计算 / 查看返回值 |

### 统计分析
| 工具 | 说明 |
|------|------|
| `stata_regress` | 线性回归 (OLS) |
| `stata_logistic` | Logistic 回归 (Logit) |
| `stata_ttest` | t 检验 |

### 通用执行
| 工具 | 说明 |
|------|------|
| `stata_run` | **执行任意 Stata 命令**（含分页） |
| `stata_run_do_file` | 执行 .do 文件 |
| `stata_graph` | 生成图形 |
| `stata_more` | **翻页浏览大输出** |

### 导出
| 工具 | 说明 |
|------|------|
| `stata_export_excel` | 数据集导出为 .xlsx；回归结果导出为 CSV |

### 包管理
| 工具 | 说明 |
|------|------|
| `stata_install_package` | 安装扩展包（ssc 或完整 from() URL） |
| `stata_find_package` | 搜索扩展包 |
| `stata_list_packages` | 列出已安装包 |
| `stata_status` | 会话状态 |

## 项目结构

```
stata-mcp/
├── .mcp.json                          # MCP Server 配置（setup.py 自动生成）
├── .mcp.json.example                  # MCP Server 配置模板（手动安装用）
├── .gitignore
├── README.md                          # 本文档
├── setup.py                           # 一键安装脚本
├── mcp-stata-server/
│   ├── server.py                      # MCP Server 主程序（22 个工具）
│   ├── requirements.txt               # Python 依赖
│   ├── pyproject.toml                 # 项目配置与测试配置
│   └── tests/                         # pytest 测试套件
└── .claude/
    ├── skills/
    │   └── stata/
    │       └── SKILL.md               # Stata 编程知识 Skill
    └── settings.local.json            # Claude Code 本地配置（插件启用）
```

## Skill 内容概览

| 模块 | 内容 |
|------|------|
| **核心原则** | 分析前先了解数据、变量名大小写、路径规范、返回值检查 |
| **语法要点** | 命令结构、if 条件陷阱、因子变量、egen 函数 |
| **分析模板** | 数据探索、OLS/Logistic 回归、分组比较、数据清洗、面板数据、工具变量、DID |
| **常见陷阱** | 变量名冲突、缺失值、字符串转换、路径、do 文件模板 |
| **高级工作流** | 回归诊断、结果存储与输出、包管理 |
| **协作规范** | 完整流程、错误排查顺序、图形导出 |

## 技术细节

### 为什么使用 pystata 而不是 subprocess？

- **真会话持久**：Stata DLL 在服务器生命周期内保持初始化，数据在工具调用间保持
- **低延迟**：无进程启动开销，命令执行在毫秒级
- **完整输出**：通过 `StataSO_GetOutputBuffer` 直接获取输出缓冲
- **线程安全**：使用 threading.Lock 确保命令串行执行

### 输出轮询机制

```
执行前: _drain_output(50ms)  — 短排空残留缓冲（50ms 上限 + 10ms 安静退出）
执行中: StataSO_Execute       — 同步调用，60s 超时看门狗
执行后: 快轮询(300×1ms)       — 收集主体输出，3次空转即退出
        _drain_output()       — 智能清尾：小输出 50ms | 大输出 100ms
        截断 120K chars        — 防止 MCP 缓冲溢出
        自动分页 4K chars       — 大输出自动分页，支持 stata_more 翻页
```

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `STATA_HOME` | `D:\StataNow19` | Stata 安装目录 |
| `STATA_EDITION` | `mp` | Stata 版本（mp/se/be） |

## 开发

### 启动服务器（调试模式）

```bash
cd mcp-stata-server
source .venv/Scripts/activate
python server.py
```

### 安装新依赖

```bash
uv pip install <package>
uv pip freeze > requirements.txt
```

## 许可证

MIT License

## 兼容性

| 组件 | 要求 |
|------|------|
| **操作系统** | Windows（pystata 依赖 Windows DLL） |
| **Stata 版本** | StataNow 19 / Stata 18（MP / SE / BE） |
| **Python** | 3.10+ |
| **Claude Code** | 最新版本（支持 MCP stdio） |

> **注意**：macOS / Linux 用户需使用 [stata_kernel](https://github.com/kylebarron/stata_kernel)
> 或 subprocess 方式调用 Stata CLI，本项目的 pystata 方案仅支持 Windows。
