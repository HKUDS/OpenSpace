# OpenSpace 完全教程

**让你的 AI Agent 更聪明、更省钱、自我进化**

---

## 目录

1. [什么是 OpenSpace？](#1-什么是-openspace)
2. [核心原理](#2-核心原理)
3. [安装与快速开始](#3-安装与快速开始)
4. [两种使用方式](#4-两种使用方式)
5. [Skill 自我进化系统](#5-skill-自我进化系统)
6. [云端 Skill 社区](#6-云端-skill-社区)
7. [本地仪表盘](#7-本地仪表盘)
8. [GDPVal 基准测试](#8-gdpval-基准测试)
9. [代码结构解析](#9-代码结构解析)
10. [高级配置](#10-高级配置)
11. [常见问题](#11-常见问题)

---

## 1. 什么是 OpenSpace？

**OpenSpace** 是一个自我进化的 AI Agent 引擎，让每一个任务都能使每个 Agent 变得更聪明、更高效。

### 核心愿景

现在的 AI Agent（如 Claude Code、Codex、Cursor、OpenClaw 等）虽然强大，但有一个致命弱点：**它们从不学习、适应和进化**，更不用说相互共享知识了。

### OpenSpace 的三大超能力

| 能力 | 描述 |
|------|------|
| 🧬 **自我进化** | Skill 自动学习并持续提升，失败变改进，成功变优化 |
| 🌐 **集体智慧** | 一个 Agent 学会，所有 Agent 受益，网络效应加速进化 |
| 💰 **Token 效率** | 复用成功方案，4.2 倍性能提升，Token 消耗减少 46% |

### 性能数据

- **4.2 倍收入提升** vs 基础 Agent（使用相同 LLM）
- **46% Token 节省** 在真实任务上
- **6 小时赚取 $11K**

---

## 2. 核心原理

### 2.1 Skill 是什么？

Skill 是**可复用的任务执行模式**，以 `SKILL.md` 文件形式存在，包含：

- **What**: 这个 Skill 做什么
- **When**: 何时使用
- **How**: 如何执行
- **Examples**: 使用示例

### 2.2 自我进化机制

OpenSpace 的 Skill 不是静态文件，而是**活实体**，会自动选择、应用、监控、分析和进化自己。

**三种进化模式：**

| 模式 | 描述 | 触发条件 |
|------|------|----------|
| 🔧 **FIX** | 原地修复损坏的指令 | 技能执行失败 |
| 🚀 **DERIVED** | 从父技能创建增强版本 | 成功执行后优化 |
| ✨ **CAPTURED** | 从成功执行中提取新模式 | 发现可复用工作流 |

**三种触发器：**

1. **📈 执行后分析** — 每次任务后运行，分析并建议进化
2. **⚠️ 工具降级检测** — 当工具成功率下降时触发
3. **📊 指标监控** — 定期扫描技能健康度

### 2.3 质量监控系统

多层次跟踪，覆盖整个执行栈：

- **🎯 Skills**: 应用率、完成率、有效率、回退率
- **🔨 工具调用**: 成功率、延迟、标记问题
- **⚡ 代码执行**: 执行状态、错误模式

### 2.4 统一后端系统

OpenSpace 提供统一的工具接入层，支持：

- **Shell**: 命令行执行
- **GUI**: Anthropic Computer Use
- **MCP**: Model Context Protocol
- **Web**: 网页搜索与浏览

---

## 3. 安装与快速开始

### 3.1 安装

```bash
# 克隆项目
git clone https://github.com/HKUDS/OpenSpace.git
cd OpenSpace

# 安装
pip install -e .

# 验证安装
openspace-mcp --help
```

> ⚠️ **注意**: 项目使用 Python 3.12+

### 3.2 轻量克隆（可选）

如果克隆速度慢，可以跳过 assets 文件夹：

```bash
git clone --filter=blob:none --sparse https://github.com/HKUDS/OpenSpace.git
cd OpenSpace
git sparse-checkout set '/*' '!assets/'
pip install -e .
```

---

## 4. 两种使用方式

### 4.1 方式 A：集成到你的 Agent

适用于支持 Skill 的 Agent：Claude Code、Codex、OpenClaw、nanobot 等。

**步骤 1: 添加到 MCP 配置**

```json
{
  "mcpServers": {
    "openspace": {
      "command": "openspace-mcp",
      "toolTimeout": 600,
      "env": {
        "OPENSPACE_HOST_SKILL_DIRS": "/path/to/your/agent/skills",
        "OPENSPACE_WORKSPACE": "/path/to/OpenSpace",
        "OPENSPACE_API_KEY": "sk-xxx (可选，用于云端)"
      }
    }
  }
}
```

**步骤 2: 复制 Skill 到 Agent 目录**

```bash
cp -r OpenSpace/openspace/host_skills/delegate-task/ /path/to/your/agent/skills/
cp -r OpenSpace/openspace/host_skills/skill-discovery/ /path/to/your/agent/skills/
```

这两个 Skill 教会你的 Agent 何时以及如何使用 OpenSpace。

### 4.2 方式 B：直接作为 AI 同事使用

创建 `.env` 文件（参考 `openspace/.env.example`）：

```bash
# 运行交互模式
openspace

# 执行特定任务
openspace --model "anthropic/claude-sonnet-4-5" --query "Create a monitoring dashboard"
```

### 4.3 Python API

```python
import asyncio
from openspace import OpenSpace

async def main():
    async with OpenSpace() as cs:
        result = await cs.execute("Analyze GitHub trending repos")
        print(result["response"])
        
        for skill in result.get("evolved_skills", []):
            print(f"  Evolved: {skill['name']}")

asyncio.run(main())
```

---

## 5. Skill 自我进化系统

### 5.1 Skill 结构

每个 Skill 是一个目录，包含：

```
my-skill/
├── SKILL.md          # Skill 定义（必需）
├── skill.yaml        # 元数据
├── versions/         # 版本历史
│   ├── v1/
│   ├── v2/
│   └── v3/
└── lineage.json     # 进化血缘
```

### 5.2 SKILL.md 格式

```markdown
# Skill Name

## What
描述这个 Skill 做什么。

## When
何时使用这个 Skill。

## How
如何执行这个 Skill。

## Examples
使用示例。
```

### 5.3 进化触发示例

当 Agent 执行任务失败时：

```
🔄 执行后分析 → 发现失败原因：PDF 解析失败
🔧 FIX: 更新 PDF 解析 Skill，添加备用方案
   → 保存为 v2 版本
```

当任务成功完成时：

```
📈 执行后分析 → 发现可优化点：成功的工作流
🚀 DERIVED: 创建更专业的版本
   → 保存为 v2 版本
```

### 5.4 Skill 血缘追踪

所有进化都被追踪，形成版本 DAG：

```
v1 (原始) → v2 (FIX) → v3 (DERIVED)
              ↓
           v2.1 (FIX)
```

---

## 6. 云端 Skill 社区

### 6.1 注册与配置

1. 访问 [open-space.cloud](https://open-space.cloud) 注册
2. 获取 API Key
3. 添加到环境变量：

```bash
export OPENSPACE_API_KEY="sk-xxx"
```

### 6.2 上传 Skill

```bash
openspace-upload-skill /path/to/skill/dir
```

### 6.3 下载 Skill

```bash
openspace-download-skill <skill_id>
```

### 6.4 访问控制

- **公开**: 所有人可见
- **私有**: 仅自己可见
- **团队**: 指定团队可见

---

## 7. 本地仪表盘

查看 Skill 如何进化——浏览技能、跟踪血缘、比较差异。

> 需要 Node.js ≥ 20

```bash
# 终端 1: 启动后端 API
openspace-dashboard --port 7788

# 终端 2: 启动前端
cd frontend
npm install
npm run dev
```

仪表盘功能：
- **Skill 分类**: 浏览、搜索、排序
- **云端技能**: 浏览社区 Skill 记录
- **版本血缘**: Skill 进化图
- **执行历史**: 运行记录与指标

---

## 8. GDPVal 基准测试

### 8.1 什么是 GDPVal？

GDPVal 是一个真实世界经济任务评估基准，包含：
- **220 个专业任务**，覆盖 44 个职业
- **50 个测试任务**，6 个行业类别

### 8.2 测试结果

| 指标 | 数值 |
|------|------|
| 收入提升 | 4.2 倍 |
| Token 节省 | 46% |
| 价值捕获 | 72.8% ($11,484 / $15,764) |
| 平均质量 | 70.8% (+30pp vs 基础) |

### 8.3 各类别表现

| 类别 | 收入变化 | Token 变化 |
|------|----------|------------|
| 📝 文档 | +3.3% | -56% |
| 📋 合规 | +18.5% | -51% |
| 🎬 媒体 | +5.8% | -46% |
| 🛠️ 工程 | +8.7% | -43% |
| 📊 表格 | +7.3% | -37% |
| 📈 战略 | +1.0% | -32% |

### 8.4 进化的 Skill 统计

在 50 个 Phase 1 任务中，OpenSpace 自动进化了 **165 个 Skill**：

| 目的 | 数量 | 说明 |
|------|------|------|
| 文件格式 I/O | 44 | PDF/DOCX/Excel/PPTX 处理 |
| 执行恢复 | 29 | 分层回退机制 |
| 文档生成 | 26 | 端到端文档管道 |
| 质量保证 | 23 | 写后验证 |
| 任务编排 | 17 | 多文件跟踪 |
| 领域工作流 | 13 | 专业领域模式 |
| 网页与研究 | 11 | SSL/代理调试 |

---

## 9. 代码结构解析

```
OpenSpace/
├── openspace/
│   ├── tool_layer.py           # 主类与配置
│   ├── mcp_server.py          # MCP 服务器
│   ├── __main__.py            # CLI 入口
│   ├── dashboard_server.py    # 仪表盘 API
│   │
│   ├── ⚡ agents/             # Agent 系统
│   │   ├── base.py           # 基础 Agent 类
│   │   └── grounding_agent.py # 执行 Agent
│   │
│   ├── ⚡ grounding/          # 统一后端
│   │   ├── core/              # 核心功能
│   │   │   ├── grounding_client.py
│   │   │   ├── search_tools.py
│   │   │   └── quality/      # 质量跟踪
│   │   └── backends/          # 后端实现
│   │       ├── shell/         # Shell 执行
│   │       ├── gui/           # GUI 控制
│   │       ├── mcp/           # MCP 协议
│   │       └── web/           # 网页访问
│   │
│   ├── 🧬 skill_engine/      # 自我进化系统
│   │   ├── registry.py        # 发现与检索
│   │   ├── analyzer.py        # 执行分析
│   │   ├── evolver.py         # 进化逻辑
│   │   ├── patch.py           # 补丁应用
│   │   ├── store.py           # SQLite 存储
│   │   └── skill_ranker.py   # 排序算法
│   │
│   ├── 🌐 cloud/              # 云端社区
│   │   ├── client.py          # HTTP 客户端
│   │   ├── search.py         # 搜索
│   │   ├── embedding.py      # 向量嵌入
│   │   └── cli/              # CLI 工具
│   │
│   ├── 🔧 platform/           # 平台抽象
│   ├── 🔧 host_skills/       # Agent 集成 Skill
│   ├── 🔧 prompts/           # LLM 提示模板
│   ├── 🔧 llm/               # LiteLLM 封装
│   ├── 🔧 config/            # 配置系统
│   ├── 🔧 recording/         # 执行录制
│   └── 📦 skills/            # 内置 Skills
│
├── frontend/                   # React 仪表盘
├── gdpval_bench/              # 基准测试
└── showcase/                  # 演示项目
```

### 核心模块说明

| 模块 | 功能 |
|------|------|
| `skill_engine` | Skill 的注册、分析、进化、存储 |
| `grounding` | 统一的后端系统，支持多种工具接入 |
| `cloud` | 云端技能社区的客户端 |
| `agents` | Agent 执行逻辑 |
| `recording` | 执行过程录制与回放 |

---

## 10. 高级配置

### 10.1 环境变量

| 变量 | 描述 | 默认值 |
|------|------|--------|
| `OPENSPACE_HOST_SKILL_DIRS` | Agent Skills 目录 | - |
| `OPENSPACE_WORKSPACE` | 工作空间路径 | 当前目录 |
| `OPENSPACE_API_KEY` | 云端 API Key | - |
| `OPENSPACE_MODEL` | 使用的 LLM | anthropic/claude-sonnet-4-20250514 |

### 10.2 详细配置

参考 `openspace/config/README.md` 获取完整配置选项。

---

## 11. 常见问题

### Q: OpenSpace 与普通 Agent 有什么区别？

普通 Agent 每次任务都从零开始，而 OpenSpace 会**学习、进化和共享**成功的工作流。

### Q: 需要多少 Token 才能开始？

无需特殊准备。OpenSpace 使用轻量级 LLM 调用进行 Skill 分析。

### Q: Skill 进化会导致无限循环吗？

不会。内置**防循环保护**，防止进化失控。

### Q: 如何确保进化质量？

1. **确认门**: 减少误触发
2. **安全检查**: 标记危险模式
3. **验证机制**: 进化后的 Skill 会先验证再替换

### Q: 支持哪些 Agent？

支持任何支持 `SKILL.md` 的 Agent：
- Claude Code
- Codex
- OpenClaw
- nanobot
- Cursor（通过 MCP）

---

## 快速参考

```bash
# 安装
pip install -e .

# 验证
openspace-mcp --help

# 运行交互模式
openspace

# 执行任务
openspace --query "your task"

# 下载云端 Skill
openspace-download-skill <skill_id>

# 上传 Skill
openspace-upload-skill /path/to/skill

# 启动仪表盘
openspace-dashboard --port 7788
```

---

## 相关链接

- [GitHub 仓库](https://github.com/HKUDS/OpenSpace)
- [云端平台](https://open-space.cloud)
- [GDPVal 基准](https://huggingface.co/datasets/openai/gdpval)

---

**让每个 Agent 自我进化 · 社区共同成长 · 更少 Token 更聪明**

*感谢使用 OpenSpace！*
