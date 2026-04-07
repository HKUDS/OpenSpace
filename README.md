<div align="center">

<picture>
    <img src="assets/logo.png" width="320px" style="border: none; box-shadow: none;" alt="OpenSpace Logo">
</picture>

## ✨ OpenSpace: Make Your Agents: Smarter, Low-Cost, Self-Evolving ✨

| 🔋 **46% Fewer Tokens** | **💰 $11K earned in 6 Hours** | 🧬 **Self-Evolving Skills** | 🌐 **Agents Experience Sharing** |

[![Agents](https://img.shields.io/badge/Agents-Claude_Code%20%7C%20Codex%20%7C%20OpenClaw%20%7C%20nanobot%20%7C%20...-99C9BF.svg)](https://modelcontextprotocol.io/)
[![Python](https://img.shields.io/badge/Python-3.12+-FCE7D6.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-C1E5F5.svg)](https://opensource.org/licenses/MIT/)
[![Feishu](https://img.shields.io/badge/Feishu-Group-E9DBFC?style=flat&logo=larksuite&logoColor=white)](./COMMUNICATION.md)
[![WeChat](https://img.shields.io/badge/WeChat-Group-C5EAB4?style=flat&logo=wechat&logoColor=white)](./COMMUNICATION.md)
[![中文文档](https://img.shields.io/badge/文档-中文版-F5C6C6?style=flat)](./README_CN.md)

**One Command to Evolve All Your AI Agents**: OpenClaw, nanobot, Claude Code, Codex, Cursor and etc.

<img src="assets/cli-typing.gif" width="500px" alt="openspace --query your task">

</div>

---

## 📢 News

- **2026-04-03** 🚀 Released **v0.1.0** — Skill quality monitoring: structural patterns extracted from high-quality skills now evaluate every new submission daily. Faster, more relevant cloud search. Production-grade vertical skill clusters emerging organically from the community. Frontend now supports Chinese (zh) i18n.
- **2026-04-02** ⚡ Cloud search upgraded for higher relevance and lower latency.
- **2026-03-31** 🛡️ Security hardening: hardened zip extraction and `import_skill` against path traversal. CLI now respects `OPENSPACE_MODEL` and `OPENSPACE_LLM_*` env vars; MiniMax compatibility; workflow ID collision fixes.
- **2026-03-29** 🔒 Pinned litellm to <1.82.7 to avoid PYSEC-2026-2 supply-chain attack.
- **2026-03-28** 🔧 Idempotent skill registration — `register_skill_dir` now returns existing `SkillMeta` for already-registered skills. Updated OpenClaw setup docs.
- **2026-03-27** 🪟 Fixed stdio deadlock on Windows; improved evolver confirmation parsing with stem-style keyword matching.
- **2026-03-26** 🌱 Dynamic skill directory re-scanning on each call, lightweight local skill search, and streamlined documentation.
- **2026-03-25** 🎉 OpenSpace is now open source!

---

## The Problem with Today's AI Agents

Today's AI agents — [OpenClaw](https://github.com/openclaw/openclaw), [nanobot](https://github.com/HKUDS/nanobot), [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Codex](https://github.com/openai/codex), [Cursor](https://cursor.com), etc. — are powerful, but they have a critical weakness: they never **Learn**, **Adapt**, and **Evolve** from real-world experience — let alone **Share** with each other.
- **❌ Massive Token Waste** - How to reuse successful task patterns instead of reasoning from scratch and burning tokens every time?
- **❌ Repeated Costly Failures** - How to share solutions across agents instead of repeating the same costly exploration and mistakes?
- **❌ Poor and Unreliable Skills** - How to maintain skill reliability as tools and APIs evolve — while ensuring community-contributed skills meet rigorous quality standards?

## 🎯 What is OpenSpace?

**🚀 🚀 The self-evolving engine where every task makes every agent smarter and more cost-efficient.**

https://github.com/user-attachments/assets/c50f70ab-f6db-47bf-9498-3210c0f0abae

OpenSpace plugs into any agent as skills and evolves it with three superpowers:

### 🧬 Self-Evolution
Skills that learn and improve themselves automatically
- ✅ **AUTO-FIX** — When a skill breaks, it fixes itself instantly
- ✅ **AUTO-IMPROVE** — Successful patterns become better skill versions
- ✅ **AUTO-LEARN** — Captures winning workflows from actual usage
- ✅ **Quality monitoring** — Tracks skill performance, error rates, and execution success across all tasks.

**Skills that continuously evolve — turning every failure into improvement, every success into optimization.**

### 🌐 Collective Agent Intelligence
Turn individual agents into a shared brain
- ✅ **Shared evolution**: One agent's improvement becomes every agent's upgrade
- ✅ **Network effects**: More agents → richer data → faster evolution for every agent
- ✅ **Easy sharing** — Upload and download evolved skills with one simple command
- ✅ **Access control** — Choose public, private, or team-only access for each skill

**One agent learns, all agents benefit — collective intelligence at scale.**

### 💰 Token Efficiency
Smarter agents, dramatically lower costs
- ✅ **Stop repeating work** → Reuse successful solutions instead of starting from zero each time
- ✅ **Tasks get cheaper** → As skills improve, similar work costs less and less
- ✅ **Small updates only** → Fix what's broken, don't rebuild everything
- ✅ **Real savings**: 4.2× better performance with 46% fewer tokens on real-world tasks, delivering measurable economic value. ([GDPVal](#-benchmark-gdpval))

Do more, spend less — agents that actually save you money over time.

---

### The Difference

**❌ Current Agents**
- Skills degrade silently as tools evolve
- Failed patterns repeat with no learning mechanism
- Knowledge remains trapped in individual agents

**✅ OpenSpace-Powered Agents**
- Multi-layer monitoring catches problems and auto-triggers repairs
- Successful workflows become reusable, shareable skills
- When one agent learns something useful, all agents get that knowledge instantly

### 📊 OpenSpace: Turn Your Agent into a Money-Making Coworker

**🎯 Real-World Results That Matter**
On 50 professional tasks (**📈 [GDPVal Economic Benchmark](#-benchmark-gdpval)**) across 6 industries, OpenSpace agents earn **4.2× more money** than baseline ([ClawWork](https://github.com/HKUDS/ClawWork)) agents using the same backbone LLM (Qwen 3.5-Plus). While cutting 46% of costly tokens through skill evolution.

<div align="center">
<img src="assets/benchmark_kpi.png" width="100%" alt="GDPVal Benchmark — Key Results" />
</div>

**💼 These Aren't Toy Problems**
- Building payroll calculators from complex union contracts
- Preparing tax returns from 15 scattered PDF documents
- Drafting legal memoranda on California privacy regulations
- Creating compliance forms and engineering specifications

**📈 Consistent Wins Across All Fields**
- Compliance work: +18.5% higher earnings
- Engineering projects: +8.7% better performance
- Professional documents: 56% fewer tokens needed
- Every category improved — no exceptions

<div align="center">
<img src="assets/benchmark_task_showcase.png" width="100%" alt="GDPVal Benchmark — Task Showcase by Category" />
</div>

**OpenSpace doesn't just make agents smarter** — it makes them economically viable. Real work, real money, measurable results.

## Use Case for Autonomous System Development with OpenSpace

**🖥️ [My Daily Monitor](showcase/README.md)** — OpenSpace empowers your agent to complete large-scale system development. This personal behavior monitoring system with 20+ live dashboard panels was built entirely by the agent — 60+ skills evolved from scratch through OpenSpace, demonstrating autonomous end-to-end software development capabilities.

<div align="center">
<img src="assets/my_daily_monitor_dark.png" width="100%" alt="My Daily Monitor – Dark Mode" />
</div>

---

## 📋 Table of Contents

- [⚡ Quick Start](#-quick-start)
  - [🤖 Path A: For Your Agent](#-path-a-for-your-agent)
  - [👤 Path B: As Your Co-Worker](#-path-b-as-your-co-worker)
  - [📊 Local Dashboard](#-local-dashboard)
- [📈 Benchmark: GDPVal](#-benchmark-gdpval)
- [📊 Showcase: My Daily Monitor](#-showcase-my-daily-monitor)
- [🏗️ Framework](#️-framework)
  - [🧬 Self-Evolution Engine](#-self-evolution-engine)
  - [🌐 Cloud Skill Community](#-cloud-skill-community)
- [🔧 Advanced Configuration](#-advanced-configuration)
- [📖 Code Structure](#-code-structure)
- [🤝 Contribute & Roadmap](#-contribute--roadmap)
- [🔗 Related Projects](#-related-projects)

---

## ⚡ Quick Start

🌐 **Just want to explore?** Browse community skills, evolution lineage at **[open-space.cloud](https://open-space.cloud)** — no installation needed.

```bash
git clone https://github.com/HKUDS/OpenSpace.git && cd OpenSpace
pip install -e .
openspace-mcp --help   # verify installation
```

> [!TIP]
> **Slow clone?** The `assets/` folder (~50 MB of images) makes the default clone large. Use this lightweight alternative to skip it:
> ```bash
> git clone --filter=blob:none --sparse https://github.com/HKUDS/OpenSpace.git
> cd OpenSpace
> git sparse-checkout set '/*' '!assets/'
> pip install -e .
> ```

**Choose your path:**
- **[Path A](#-path-a-for-your-agent)** — Plug OpenSpace into your agent
- **[Path B](#-path-b-as-your-co-worker)** — Use OpenSpace directly as your AI co-worker

### 🤖 Path A: For Your Agent

Works with any agent that supports skills (`SKILL.md`) — [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Codex](https://github.com/openai/codex), [OpenClaw](https://github.com/openclaw/openclaw), [nanobot](https://github.com/HKUDS/nanobot), etc.

**① Add OpenSpace to your agent's MCP config:**

```json
{
  "mcpServers": {
    "openspace": {
      "command": "openspace-mcp",
      "toolTimeout": 600,
      "env": {
        "OPENSPACE_HOST_SKILL_DIRS": "/path/to/your/agent/skills",
        "OPENSPACE_WORKSPACE": "/path/to/OpenSpace",
        "OPENSPACE_API_KEY": "sk-xxx (optional, for cloud)"
      }
    }
  }
}
```

> [!TIP]
> Credentials (API key, model) are **auto-detected** from your agent's config; you usually don't need to set them manually.

> [!NOTE]
> OpenSpace supports 3 launch modes:
> - **stdio**: keep `command: "openspace-mcp"` in the host config.
> - **SSE**: start `openspace-mcp --transport sse --host 127.0.0.1 --port 8080`.
> - **streamable HTTP**: start `openspace-mcp --transport streamable-http --host 127.0.0.1 --port 8081`.
>
> Common remote endpoints:
> - SSE endpoint: `http://127.0.0.1:8080/sse`
> - streamable HTTP endpoint: `http://127.0.0.1:8081/mcp`
>
> `stdio` is the simplest option. HTTP modes keep OpenSpace as a standalone server, but **host-specific registration syntax** and **host-side timeouts** still apply.

**② Copy skills** into your agent's skills directory:

```bash
cp -r OpenSpace/openspace/host_skills/delegate-task/ /path/to/your/agent/skills/
cp -r OpenSpace/openspace/host_skills/skill-discovery/ /path/to/your/agent/skills/
```

Done. These two skills teach your agent when and how to use OpenSpace — no additional prompting needed. Your agent can now self-evolve skills, execute complex tasks, and access the cloud skill community. You can also add your own custom skills — see [`openspace/skills/README.md`](openspace/skills/README.md).

> [!NOTE]
> **Cloud community (optional):** Register at **[open-space.cloud](https://open-space.cloud)** to get a `OPENSPACE_API_KEY`, then add it to the `env` block above. Without it, all local capabilities (task execution, evolution, local skill search) work normally.

📖 Per-agent config (OpenClaw / nanobot), all env vars, advanced settings: [`openspace/host_skills/README.md`](openspace/host_skills/README.md)

### 👤 Path B: As Your Co-Worker

Use OpenSpace directly — coding, search, tool use, and more — with self-evolving skills and cloud community built in.

> [!NOTE]
> Create a `.env` file with your LLM API key and optionally `OPENSPACE_API_KEY` for cloud community access (refer to [`openspace/.env.example`](openspace/.env.example)).

```bash
# Interactive mode
openspace

# Execute task
openspace --model "anthropic/claude-sonnet-4-5" --query "Create a monitoring dashboard for my Docker containers"
```

Add your own custom skills: [`openspace/skills/README.md`](openspace/skills/README.md).

**Cloud CLI** — manage skills from the command line:

```bash
openspace-download-skill <skill_id>         # download a skill from the cloud
openspace-upload-skill /path/to/skill/dir   # upload a skill to the cloud
```

<details>
<summary><b>Python API</b></summary>

```python
import asyncio
from openspace import OpenSpace

async def main():
    async with OpenSpace() as cs:
        result = await cs.execute("Analyze GitHub trending repos and create a report")
        print(result["response"])

        for skill in result.get("evolved_skills", []):
            print(f"  Evolved: {skill['name']} ({skill['origin']})")

asyncio.run(main())
```

</details>

### 📊 Local Dashboard

See how your skills evolve — browse skills, track lineage, compare diffs.

> Requires **Node.js ≥ 20**.

```bash
# Terminal 1. Start backend API
openspace-dashboard --port 7788

# Terminal 2: Start frontend dev server
cd frontend
npm install        # only needed once
npm run dev    
```

📖 **Frontend setup guide**: [`frontend/README.md`](frontend/README.md)

<div align="center">
<table>
<tr>
<td width="50%"><img src="assets/frontend_1.gif" width="100%" alt="Skill Classes" /></td>
<td width="50%"><img src="assets/frontend_2.gif" width="100%" alt="Cloud Skill Records" /></td>
</tr>
<tr>
<td align="center"><sub>Skill Classes — Browse, Search & Sort</sub></td>
<td align="center"><sub>Cloud — Browse & Discover Skill Records</sub></td>
</tr>
<tr>
<td width="50%"><img src="assets/frontend_3.gif" width="100%" alt="Version Lineage" /></td>
<td width="50%"><img src="assets/frontend_4.gif" width="100%" alt="Workflow Sessions" /></td>
</tr>
<tr>
<td align="center"><sub>Version Lineage — Skill Evolution Graph</sub></td>
<td align="center"><sub>Workflow Sessions — Execution History & Metrics</sub></td>
</tr>
</table>
</div>

---

## 📈 Benchmark: GDPVal

We evaluate OpenSpace on [GDPVal](https://huggingface.co/datasets/openai/gdpval) — 220 real-world professional tasks spanning 44 occupations — using the [ClawWork](https://github.com/HKUDS/ClawWork) evaluation protocol with identical productivity tools and LLM-based scoring. Our two-phase design (Cold Start → Warm Rerun) demonstrates how accumulated skills reduce token consumption over time.

Fair Benchmark: OpenSpace uses Qwen 3.5-Plus as its backbone LLM — identical to a ClawWork baseline agent — ensuring that performance differences stem purely from skill evolution, not model capabilities.

Real Economic Value: Tasks range from building payroll calculators to preparing tax returns to drafting legal memoranda — the same professional work that generates actual GDP, evaluated on both quality and cost efficiency.

<div align="center">
<img src="assets/benchmark_income.png" width="100%" alt="GDPVal Benchmark — Income Comparison" />
</div>

- **4.2× Higher Income** vs ClawWork with the same backbone LLM (Qwen 3.5-Plus)
- **72.8% Value Capture** — $11,484 earned out of $15,764 task value, outperforming all agents
- **70.8% Average Quality** — +30pp above the best ClawWork agent (40.8%)
− **45.9% Token Usage** in Phase 2 vs Phase 1 — better results with dramatically lower costs

<div align="center">
<img src="assets/benchmark_quality_tokens.png" width="100%" alt="GDPVal Benchmark — Quality & Token Efficiency" />
</div>

### What Real-World Tasks Can OpenSpace Handle?

The 50 GDPVal tasks span 6 real-world work categories. 
- **Phase 1 (Cold Start)** runs all 50 tasks sequentially — skills accumulate in a shared database as each task completes.
- **Phase 2 (Warm Rerun)** re-executes the same 50 tasks with the full evolved skill database from Phase 1.

Income Capture = actual payment earned ÷ maximum possible task value

<div align="center">
<img src="assets/benchmark_task_showcase.png" width="100%" alt="GDPVal Benchmark — Task Showcase by Category" />
</div>

## 🎯 Where Evolution Delivers Maximum Impact — And Why:

| Category | Income Δ | Token Δ | Why |
|---|---|---|---|
| **📝 Documents & Correspondence** (7) | 71→74% (+3.3pp) | −56% | Polished formal output — California privacy law memoranda, surveillance investigation reports, child support case reports. The `document-gen-fallback` skill family evolved through 13 versions, making structure and error recovery near-automatic. |
| **📋 Compliance & Form** (11) | 51→70% (+18.5pp) | −51% | Structured PDFs — tax returns from 15 source documents, pharmacy compliance checklists, clinical handoff templates. The PDF skill chain (checklist logic → reportlab layout → verification) evolves once, then all form tasks reuse the full pipeline. |
| **🎬 Media Production** (3) | 53→58% (+5.8pp) | −46% | Audio/video via Python and ffmpeg — bossa-nova instrumental from drum reference, bass stem editing from 5 tracks, CGI show reel from 13 source videos. Evolved skills encode working ffmpeg flags and codec fallbacks, eliminating sandbox trial-and-error. |
| **🛠️ Engineering** (4) | 70→78% (+8.7pp) | −43% | Multi-deliverable technical projects — Web3 full-stack (Solidity + React + tests), CNC workcell safety system (report + layout + hardware table), aerospace CFD report. Coordination skills transfer universally across these diverse tasks. |
| **📊 Spreadsheets** (15) | 63→70% (+7.3pp) | −37% | Functional .xlsx tools — payroll calculators from union contracts, sales forecasts from historical data, pricing models with competitor benchmarking. Spreadsheet patterns (formulas, merged cells, validation) are identical across domains. |
| **📈 Strategy & Analysis** (10) | 88→89% (+1.0pp) | −32% | Strategic recommendations — supplier negotiation strategies, nonprofit program evaluations, energy trading analysis for a $300M desk. Already highest quality (88%); savings from reusing document structure and multi-file orchestration. |

### What Did Evolution Produce? (165 Skills)

Across 50 Phase 1 tasks, OpenSpace autonomously evolved **165 skills**. The breakthrough insight: these aren't just domain knowledge — they're **resilient execution patterns** and **quality assurance workflows**. The agent learned how to reliably deliver results in an imperfect, real-world environment.

**Key Discovery**: Most skills focus on tool reliability and error recovery, not task-specific knowledge.

<div align="center">
<img src="assets/benchmark_skill_taxonomy.png" width="100%" alt="GDPVal Benchmark — Evolved Skill Taxonomy" />
</div>

| Purpose | Count | What It Teaches the Agent |
|---|---|---|
| **File Format I/O** | 44 | PDF extraction fallbacks, DOCX parsing, Excel merged-cell handling, PPTX creation. 32/44 *captured* from real failures — each one is a production bug solved. |
| **Execution Recovery** | 29 | Layered fallback: sandbox fails → shell → file-write-then-run → heredoc. 28/29 *captured* from actual crashes. The foundation that makes everything else reliable. |
| **Document Generation** | 26 | End-to-end doc pipeline. `document-gen-fallback` evolved from 1 imported skill into **13 derived versions** — the most deeply iterated skill family. |
| **Quality Assurance** | 23 | Post-write verification: check Excel row counts, validate PDF pages, proof-gate spreadsheet formulas. Why P2 quality improves — the agent *verifies*, not just produces. |
| **Task Orchestration** | 17 | Multi-file tracking, ZIP packaging, zero-iteration failure detection. Meta-skills that help across all task types with multiple deliverables. |
| **Domain Workflow** | 13 | SOAP notes, audio production (**4 generations** from 1 template), video pipelines. Small count but deep evolution within each domain. |
| **Web & Research** | 11 | SSL/proxy debugging, search fallbacks, JS-heavy page handling. Includes 2 *fixed* skills — web access is inherently unstable. |

**Reproduce experiments, analysis tools, and results**: [`gdpval_bench/README.md`](gdpval_bench/README.md)

---

## 📊 Showcase: My Daily Monitor

> **Zero human code was written.** 60+ skills evolved from scratch to build a fully working live dashboard.

**My Daily Monitor** is an always-on dashboard streaming processes, servers, news, markets, email, and schedules — with a built-in AI agent.

<div align="center">
<img src="assets/my_daily_monitor_light.png" width="90%" alt="My Daily Monitor – Light Mode" />
</div>

### How OpenSpace Built It (From Zero)

| Phase | What Happened | Skills |
|-------|--------------|--------|
| 🌱 **Seed** | Analyzed open-source [WorldMonitor](https://github.com/koala73/worldmonitor), extracted reference patterns | 6 initial skills |
| 🏗️ **Scaffold** | Generated project structure, Vite config, TypeScript setup | +8 skills |
| 🎨 **Build** | Created 20+ panels with data services, API routes, grid layout | +25 skills |
| 🔧 **Fix** | Auto-repaired broken TypeScript, API mismatches, CSS conflicts | +12 FIX evolutions |
| 🧬 **Evolve** | Derived enhanced patterns, merged complementary skills | +15 DERIVED skills |
| 📦 **Capture** | Extracted reusable patterns from successful executions | +8 CAPTURED skills |

### 📈 Skill Evolution Graph

<div align="center">
<img src="assets/my_daily_monitor_evograph.png" width="90%" alt="Skill Evolution Graph" />
</div>

> Each node is a skill that OpenSpace learned, extracted, or refined. The full evolution history is open-sourced in [`showcase/.openspace/openspace.db`](showcase/.openspace/openspace.db) — load it in any SQLite browser to explore lineage, diffs, and quality metrics.

**Full details**: [`showcase/README.md`](showcase/README.md)

---

## 🏗️ OpenSpace's Framework

<div align="center">
<img src="assets/framework.png" width="90%" alt="OpenSpace Framework" />
</div>

### 🧬 Self-Evolution Engine

The core of OpenSpace. Skills aren't static files — they're living entities that automatically select, apply, monitor, analyze, and evolve themselves.

#### 🔄 Autonomous & Continuous Evolution

- **Full Lifecycle Management**: From discovery to application to evolution — all without human intervention. OpenSpace completes tasks regardless of whether matching skills exist.

**Three Evolution Modes**:
- 🔧 FIX — Repair broken or outdated instructions in-place. Same skill, new version.
- 🚀 DERIVED — Create enhanced or specialized versions from parent skills. New skill directory, coexists with parents.
- ✨ CAPTURED — Extract novel reusable patterns from successful executions. Brand new skill, no parent.

**Three Independent Triggers**: Multiple lines of defense against skill degradation — both successful and failed executions drive evolution.
- **📈 Post-Execution Analysis** — Runs after every task. Analyzes full recordings and suggests FIX/DERIVED/CAPTURED for involved skills.
- **⚠️ Tool Degradation** — When tool success rates drop, quality monitor finds all dependent skills and batch-evolves them.
- **📊 Metric Monitor** — Periodically scans skill health metrics (applied rate, completion rate, fallback rate) and evolves underperformers.

#### 📊 Full-Stack Quality Monitoring
Multi-Layer Tracking: Quality monitoring covers the entire execution stack — from high-level workflows to individual tool calls:
- **🎯 Skills** — applied rate, completion rate, effective rate, fallback rate
- **🔨 Tool Calls** — success rate, latency, flagged issues
- **⚡ Code Execution** — execution status, error patterns

**Cascade Evolution**: When any component degrades — skill workflow or single tool call — evolution automatically triggers for all upstream dependent skills, maintaining system-wide coherence.

#### 🔧 Intelligent & Safe Evolution
**🤖 Autonomous Evolution**: Each evolution explores the codebase, discovers root causes, and decides fixes autonomously — gathering real evidence before making changes, not generating blindly.

**⚡ Diff-Based & Token-Efficient**: Produces minimal, targeted diffs rather than full rewrites, with automatic retry on failure. Every version stored in a version DAG with full lineage tracking.

**🛡️ Built-in Safeguards**:
- Confirmation gates reduce false-positive triggers
- Anti-loop guards prevent runaway evolution cycles
- Safety checks flag dangerous patterns (prompt injection, credential exfiltration)
- Evolved skills are validated before replacing predecessors

**🌐 Collaborative Skill Community**
A collaborative registry where agents share evolved skills. When one agent evolves an improvement, every connected agent can discover, import, and build on it — turning individual progress into collective intelligence.

- **🔐 Flexible Sharing**: Share skills publicly, within groups, or keep them private. Smart search finds what you need and auto-imports it. Every evolution is lineage-tracked with full diffs.

- **☁️ Collaborative Platform**: open-space.cloud — register for an API key, browse community skills, and manage your groups.

---

## 🔧 Advanced Configuration

For most users, [Quick Start](#-quick-start) is all you need. For advanced options (environment variables, execution modes, security policies, etc.), see [`openspace/config/README.md`](openspace/config/README.md).

---

<a id="-code-structure"></a>
<details>
<summary><b>📖 Code Structure</b></summary>

> **Legend**: ⚡ Core modules &nbsp;|&nbsp; 🧬 Skill evolution &nbsp;|&nbsp; 🌐 Cloud &nbsp;|&nbsp; 🔧 Supporting modules

```
OpenSpace/
├── openspace/
│   ├── tool_layer.py                     # OpenSpace main class & OpenSpaceConfig
│   ├── mcp_server.py                     # MCP Server (4 tools for your agent)
│   ├── __main__.py                       # CLI entry point (python -m openspace)
│   ├── dashboard_server.py               # Web dashboard API server
│   │
│   ├── ⚡ agents/                         # Agent System
│   │   ├── base.py                       # Base agent class
│   │   └── grounding_agent.py            # Execution agent (tool calling, iteration, skill injection)
│   │
│   ├── ⚡ grounding/                      # Unified Backend System
│   │   ├── core/
│   │   │   ├── grounding_client.py       # Unified interface across all backends
│   │   │   ├── search_tools.py           # Smart Tool RAG (BM25 + embedding + LLM)
│   │   │   ├── quality/                  # Tool quality tracking & self-evolution
│   │   │   ├── security/                 # Policies, sandboxing, E2B
│   │   │   ├── system/                   # System-level provider & tools
│   │   │   ├── transport/                # Connectors & task managers
│   │   │   └── tool/                     # Tool abstraction (base, local, remote)
│   │   └── backends/
│   │       ├── shell/                    # Shell command execution
│   │       ├── gui/                      # Anthropic Computer Use
│   │       ├── mcp/                      # Model Context Protocol (stdio, HTTP, WebSocket)
│   │       └── web/                      # Web search & browsing
│   │
│   ├── 🧬 skill_engine/                  # Self-Evolving Skill System
│   │   ├── registry.py                   # Discovery, BM25+embedding pre-filter, LLM selection
│   │   ├── analyzer.py                   # Post-execution analysis (agent loop + tool access)
│   │   ├── evolver.py                    # FIX / DERIVED / CAPTURED evolution (3 triggers)
│   │   ├── patch.py                      # Multi-file FULL / DIFF / PATCH application
│   │   ├── store.py                      # SQLite persistence, version DAG, quality metrics
│   │   ├── skill_ranker.py               # BM25 + embedding hybrid ranking
│   │   ├── retrieve_tool.py              # Skill retrieval tool for agents
│   │   ├── fuzzy_match.py                # Fuzzy matching for skill discovery
│   │   ├── conversation_formatter.py     # Format execution history for analysis
│   │   ├── skill_utils.py                # Shared skill utilities
│   │   └── types.py                      # SkillRecord, SkillLineage, EvolutionSuggestion
│   │
│   ├── 🌐 cloud/                         # Cloud Skill Community
│   │   ├── client.py                     # HTTP client (upload, download, search)
│   │   ├── search.py                     # Hybrid search engine
│   │   ├── embedding.py                  # Embedding generation for skill search
│   │   ├── auth.py                       # API key management
│   │   └── cli/                          # CLI tools (download_skill, upload_skill)
│   │
│   ├── 🔧 platform/                      # Platform abstraction (system info, screenshots)
│   ├── 🔧 host_detection/                # Auto-detect nanobot / openclaw credentials
│   ├── 🔧 host_skills/                   # SKILL.md definitions for agent integration
│   │   ├── delegate-task/SKILL.md        # Teaches agent: execute, fix, upload
│   │   └── skill-discovery/SKILL.md      # Teaches agent: search & discover skills
│   ├── 🔧 prompts/                       # LLM prompt templates (grounding + skill engine)
│   ├── 🔧 llm/                           # LiteLLM wrapper with retry & rate limiting
│   ├── 🔧 config/                        # Layered configuration system
│   ├── 🔧 local_server/                  # GUI/Shell backend Flask server (server mode)
│   ├── 🔧 recording/                     # Execution recording, screenshots & video capture
│   ├── 🔧 utils/                         # Logging, UI, telemetry
│   └── 📦 skills/                        # Built-in skills (lowest priority, user can add here)
│
├── frontend/                             # Dashboard UI (React + Tailwind)
├── gdpval_bench/                         # GDPVal benchmark experiments & results
├── showcase/                             # My Daily Monitor (60+ evolved skills)
│   ├── my-daily-monitor/                 # The full app (zero human code)
│   └── skills/                           # 60+ evolved skills with full lineage
├── .openspace/                           # Runtime: embedding cache + skill DB
└── logs/                                 # Execution logs & recordings
```

</details>

---

## 🤝 Contribute & Roadmap

We welcome contributions! OpenSpace today evolves *how to do X*. The next frontier: **evolving how agents organize doing X together**. 

Group infrastructure (visibility, sharing, permissions) is already live. What comes next:

- [ ] **[Kanban](https://github.com/BloopAI/vibe-kanban)-style orchestration** — Shared task board with skill-aware scheduling; scheduling itself evolves
- [ ] **Collaboration pattern evolution** — Decomposition, handoff, prioritization strategies captured and improved from completed tasks
- [ ] **Role emergence** — Agents develop role profiles through practice, not configuration
- [ ] **Cross-group pattern transfer** — Coordination patterns discovered by one group available to others via cloud registry

---

## 🔗 Related Projects

OpenSpace builds upon the following open-source projects. We sincerely thank their authors and contributors:

- **[AnyTool](https://github.com/HKUDS/AnyTool)** — Plug-and-play universal tool-use layer for any AI agent
- **[ClawWork](https://github.com/HKUDS/ClawWork)** - Transforms AI assistants into true AI coworkers
- **[WorldMonitor](https://github.com/koala73/worldmonitor)** - Real-time global intelligence dashboard

---

<div align="center">

## ⭐ Star History

If you find OpenSpace helpful, please consider giving us a star! ⭐

<div align="center">
  <a href="https://star-history.com/#HKUDS/OpenSpace&Date">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=HKUDS/OpenSpace&type=Date&theme=dark" />
      <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=HKUDS/OpenSpace&type=Date" />
      <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=HKUDS/OpenSpace&type=Date" />
    </picture>
  </a>
</div>

**🧬 Make You Agent Self-Evolve · 🌐 A Community That Grows Together · 💰 Fewer Tokens, Smarter Agents**

</div>

---

<p align="center">
  <em> ❤️ Thanks for visiting ✨ OpenSpace!</em><br><br>
  <img src="https://visitor-badge.laobi.icu/badge?page_id=HKUDS.OpenSpace&style=for-the-badge&color=00d4ff"
  alt="Views">
</p>
