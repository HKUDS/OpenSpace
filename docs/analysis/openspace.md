# OpenSpace（子代理C）初学者速读

## 1）这个仓库是做什么的（一句话版）

OpenSpace 是一个给 AI Agent 用的技能中心（Skill Hub）。
它的目标不是“加更多技能”，而是让 Agent 在真实任务里形成一个循环：
先执行 → 记录结果 → 判断技能可靠性 → 只保留和复用值得信任的技能。

你可以把它理解成：

- 一个让 Skill 能够被**发现**的仓库（本地 + 云端）
- 一个把每次执行都变成**证据**的记录系统
- 一个在证据基础上自动改进 Skill 的进化系统

## 2）关键模块（按“怎么先看懂它”来分）

- `openspace/application.py` + `openspace/runtime/`
  - OpenSpace 的主入口和运行核心。
  - 负责把任务、状态、会话、配置串起来，CLI / MCP / Dashboard 共用同一套执行逻辑。
- `openspace/skill_engine/`
  - 技能引擎，负责 Skill 的发现、排序、执行后评估、进化和版本记录。
  - 你会看到 `analyzer.py`、`evolver.py`、`store.py`、`skill_ranker.py` 这些核心文件。
- `openspace/grounding/`
  - 工具执行层，连接不同后端能力（Shell、Web、MCP、GUI 等），用于真正“做事”。
- `openspace/agents/` 和 `openspace/tool_runtime/`
  - Agent 的决策与工具调用运行时，决定什么时候用哪个技能、怎么调用工具。
- `openspace/cloud/`
  - 与云端技能社区连接：搜索、下载、上传技能，处理账号和访问权限。
- `openspace/communication/`
  - 消息网关能力，面向多渠道（例如 WhatsApp、Feishu）执行任务入口。
- `openspace/recording/` + `openspace/telemetry/` + `openspace/persistence/`
  - 负责把执行过程、质量指标、日志、录像等写入“证据库”，后续用于决策和回放。
- `openspace/cli/`、`openspace/entrypoints/`、`apps/`
  - 用户如何使用 OpenSpace 的入口层：命令行、MCP 服务、Dashboard、TUI、Gateway 等。

## 3）新手友好执行流程（先懂这条线）

1. 你给 Agent 一个任务。
2. OpenSpace 从本地/云端找候选 Skill。
3. 通过 skill_engine + grounding 执行任务。
4. 任务结束后写入执行记录（成功/失败/回退）。
5. 根据记录更新技能质量分。
6. 对低质量技能提出改进候选（FIX / DERIVED / CAPTURED）。
7. 经验证与确认后进入更高可信状态，再供后续任务复用。

## 4）我建议先读的地方（按优先级）

1. `README.md` 的 `Framework` 和 `Code Structure`（中文可先读 `README_zh.md`）
2. `openspace/host_skills/README.md`（想接入 Agent 时）
3. `openspace/skill_engine/README.md`（如果有的话）
4. 本文档：`docs/analysis/openspace.md`
