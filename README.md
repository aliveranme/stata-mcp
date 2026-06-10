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
│  │  - 语法规范   │          │  - 19 个工具       │  │
│  │  - 分析模板   │          │  - pystata 直接调用 │  │
│  │  - 常见陷阱   │          │  - StataNow 19.5 MP │  │
│  └──────────────┘          └──────────────────┘  │
└──────────────────────────────────────────────────┘
```

## 前置条件

- **Windows** 操作系统
- **StataNow 19**（或 Stata 18+ MP/SE/BE），安装在 `D:\StataNow19`（可通过环境变量自定义）
- **Python 3.10+**（推荐 3.12+）
- **Claude Code**（最新版本）

## 快速开始

### 1. 配置环境

```bash
# 克隆仓库
git clone <repo-url>
cd temp

# 创建 Python 虚拟环境并安装依赖
cd mcp-stata-server
uv venv
source .venv/Scripts/activate  # Windows Git Bash
uv pip install fastmcp
```

### 2. 配置 Stata 路径（可选）

如果 Stata 安装在非默认路径，设置环境变量：

```bash
export STATA_HOME="E:/Stata18"      # 默认 D:/StataNow19
export STATA_EDITION="se"           # 默认 mp，可选 se/be
```

### 3. 配置 Claude Code

项目根目录的 `.mcp.json` 已包含 MCP Server 配置：

```json
{
  "mcpServers": {
    "stata": {
      "command": "F:/Projects/temp/temp/temp/mcp-stata-server/.venv/Scripts/python.exe",
      "args": ["F:/Projects/temp/temp/temp/mcp-stata-server/server.py"]
    }
  }
}
```

重启 Claude Code 或运行 `/reload-plugins` 即可连接。

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
| `stata_run` | **执行任意 Stata 命令** |
| `stata_run_do_file` | 执行 .do 文件 |
| `stata_graph` | 生成图形 |

### 包管理
| 工具 | 说明 |
|------|------|
| `stata_install_package` | 安装扩展包（ssc/net） |
| `stata_find_package` | 搜索扩展包 |
| `stata_list_packages` | 列出已安装包 |
| `stata_status` | 会话状态 |

## 项目结构

```
temp/
├── .mcp.json                          # MCP Server 配置
├── .gitignore
├── README.md                          # 本文档
├── mcp-stata-server/
│   ├── server.py                      # MCP Server 主程序（19 个工具）
│   └── requirements.txt               # Python 依赖（fastmcp）
└── .claude/
    ├── skills/
    │   └── stata/
    │       └── SKILL.md               # Stata 编程知识 Skill
    └── settings.local.json            # Claude Code 本地配置
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
StataSO_Execute(cmd)          ← 同步调用
  ├── 阶段 1：快速轮询        ← 5ms 间隔，连续 5 次空转后进入阶段 2
  ├── 阶段 2：延迟等待        ← 80ms 等待 Stata 生产尾部输出
  └── 最终检查                ← 再轮询 10 次确认无遗漏
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
