# OpenSpace（子代理C速读）

## 一句话目标
OpenSpace 是给 AI Agent 用的“技能仓库+训练场”：它不只让 Agent 记住技能（Skill），更要让 Agent 记住哪些技能在真实任务里真的有效。

## 核心模块（怎么理解）
- `openspace/application.py`：对外入口。
  - 你调用 OpenSpace 时，先经过这里，像门面一样把配置和运行参数整理好。
- `openspace/runtime/`：任务运行核心。
  - 负责会话、状态、执行生命周期，保证 CLI、MCP、Dashboard 共用一套执行逻辑。
- `openspace/skill_engine/`：技能记忆与进化脑。
  - `registry.py`：建技能目录，发现本地和云端技能。
  - `analyzer.py`：任务后分析，判断技能是否起效。
  - `evolver.py`：生成 FIX / DERIVED / CAPTURED 版本。
  - `store.py`：把技能版本、谱系、质量分数持久化。
  - `skill_ranker.py`：按质量+匹配度给技能排序。
- `openspace/grounding/`：能力执行层。
  - 统一不同后端（shell、gui、mcp、web、meta）执行工具。
- `openspace/agents/`：Agent 主体。
  - 负责与大模型协作、工具调度和任务循环。
- `openspace/cloud/`：技能社区连接层。
  - 上传、下载、搜索公开/私有技能，做质量证据上链。
- `openspace/communication/`：多渠道对接。
  - 支持 WhatsApp、Feishu 等消息入口。
- `openspace/recording/` 与 `openspace/persistence/`：证据仓库。
  - 记录执行日志、截图、对话、指标，用于质量判断。
- `apps/`：可视化界面。
  - Dashboard 和 TUI，把“哪些技能在变好/变差”展示给人看。

## 给第一次看的人（超简版）
想象你有一个助理（AI Agent），平时它会不断尝试很多“操作小剧本”（Skill）。

OpenSpace 的作用：
1. 让助理先选对剧本。
2. 把每次执行结果都记录下来：是成功、失败还是临时放弃。
3. 把这些记录变成“质量信号”，给更可靠的技能更高优先级。
4. 允许自动改进，但用“先试用后验证再信任”的方式，避免乱改。
5. 最后把能用得更好、来源更清楚的技能，按包（package）共享出去。

你可以把它想成：
- 一个会“记账”的技能库（有证据）
- 一个会“练习”的进化系统（按结果改进）
- 一个“本地执行、云端发现”的协作平台

## 一个使用流程（从 0 到 1）
- 你发任务
- Agent 从本地/云端找到候选 Skill
- 选中一个 Skill 去执行（可能调用 shell、MCP、浏览器等）
- 执行结束后写入证据记录
- 记录决定该 Skill 是否值得继续重用
- 触发演化时，产生新版本并做验证
- 通过质量和审核规则后，推送到更高信任状态

## 我最先看的入口（建议）
- 先读：
  - `README.md` 的 “Quick Start”和“Framework”
  - `README.md` 的 “Code Structure” 代码树
  - `openspace/host_skills/README.md`（想让 Agent 接入 MCP 时）
  - 本文档：`docs/analysis/openspace.md`
