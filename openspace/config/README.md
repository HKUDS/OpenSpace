# 🔧 Configuration Guide

## 1. LLM Credentials (`.env`)

> [!NOTE]
> Create `openspace/.env` from [`.env.example`](../../.env.example) and set at least one LLM API key.

Resolution priority (first match wins):

| Priority | Source | Example |
|----------|--------|---------|
| **Tier 1** | `OPENSPACE_LLM_*` env vars | `OPENSPACE_LLM_API_KEY=sk-xxx` |
| **Tier 2** | Provider-native env vars | `OPENROUTER_API_KEY=sk-or-xxx` |
| **Tier 3** | Host agent config | `~/.nanobot/config.json` / `~/.openclaw/openclaw.json` |

> [!IMPORTANT]
> Tier 2 blocks Tier 3 — if `.env` has a provider key, host agent config is skipped.

```bash
# Provider-native — litellm reads automatically
OPENROUTER_API_KEY=sk-or-v1-xxx

# Or: OpenSpace-native — higher priority, same effect
OPENSPACE_LLM_API_KEY=sk-or-v1-xxx
```

## 2. Environment Variables

Set via `.env`, MCP config `env` block, or system environment.

| Variable | Description | Default |
|----------|-------------|---------|
| `OPENSPACE_MODEL` | LLM model | `openrouter/anthropic/claude-sonnet-4.5` |
| `OPENSPACE_LLM_API_KEY` | LLM API key (Tier 1 override) | — |
| `OPENSPACE_LLM_API_BASE` | LLM API base URL | — |
| `OPENSPACE_LLM_EXTRA_HEADERS` | Extra LLM headers (JSON) | — |
| `OPENSPACE_LLM_CONFIG` | Arbitrary litellm kwargs (JSON) | — |
| `OPENSPACE_SKILL_EMBEDDING_BACKEND` | Skill-router embedding backend: `auto`, `local`, or `remote` | `auto` |
| `OPENSPACE_SKILL_EMBEDDING_MODEL` | Skill-router embedding model | `BAAI/bge-small-en-v1.5` in local mode, `openai/text-embedding-3-small` in remote mode |
| `OPENSPACE_SKILL_EMBEDDING_API_KEY` | Dedicated remote embedding API key for skill routing | — |
| `OPENSPACE_SKILL_EMBEDDING_API_BASE` | Dedicated remote embedding API base for skill routing | — |
| `EMBEDDING_API_KEY` | Generic embedding API key (tool search, optional skill-router fallback) | — |
| `EMBEDDING_BASE_URL` | Generic embedding API base URL | — |
| `EMBEDDING_MODEL` | Generic embedding model for tool search | `BAAI/bge-small-en-v1.5` |
| `OPENSPACE_API_KEY` | Cloud API key ([open-space.cloud](https://open-space.cloud)) | — |
| `OPENSPACE_MAX_ITERATIONS` | Max agent iterations per task | `20` |
| `OPENSPACE_BACKEND_SCOPE` | Enabled backends (comma-separated) | `shell,gui,mcp,web,system` |
| `OPENSPACE_HOST_SKILL_DIRS` | Agent skill directories (comma-separated) | — |
| `OPENSPACE_WORKSPACE` | Project root for logs/workspace | — |
| `OPENSPACE_SHELL_CONDA_ENV` | Conda env for shell backend | — |
| `OPENSPACE_SHELL_WORKING_DIR` | Working dir for shell backend | — |
| `OPENSPACE_CONFIG_PATH` | Custom grounding config JSON | — |
| `OPENSPACE_MCP_SERVERS_JSON` | MCP server definitions (JSON) | — |
| `OPENSPACE_ENABLE_RECORDING` | Record execution traces | `true` |
| `OPENSPACE_LOG_LEVEL` | Log level | `INFO` |

### Split-routing example

Keep the main LLM on an OpenAI-compatible provider, but force the
skill-router embedding path to stay local:

```bash
OPENSPACE_MODEL=gpt-5.4
OPENSPACE_LLM_API_KEY=sk-xxx
OPENSPACE_LLM_API_BASE=http://127.0.0.1:8080/v1

OPENSPACE_SKILL_EMBEDDING_BACKEND=local
OPENSPACE_SKILL_EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
```

Or send skill embeddings to a separate endpoint:

```bash
OPENSPACE_SKILL_EMBEDDING_BACKEND=remote
OPENSPACE_SKILL_EMBEDDING_API_KEY=sk-embed-xxx
OPENSPACE_SKILL_EMBEDDING_API_BASE=https://example.com/v1
OPENSPACE_SKILL_EMBEDDING_MODEL=openai/text-embedding-3-small
```

## 3. MCP Servers (`config_mcp.json`)

Register external MCP servers that OpenSpace connects to as a **client** (e.g. GitHub, Slack, databases):

```bash
cp openspace/config/config_mcp.json.example openspace/config/config_mcp.json
```

```json
{
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": { "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}" }
    }
  }
}
```

## 4. Execution Mode: Local vs Server

Shell and GUI backends support two execution modes, set via `"mode"` in `config_grounding.json`:

| | Local Mode (`"local"`, default) | Server Mode (`"server"`) |
|---|---|---|
| **Setup** | Zero config | Start `local_server` first |
| **Use case** | Same-machine development | Remote VMs, sandboxing, multi-machine |
| **How** | `asyncio.subprocess` in-process | HTTP → Flask → subprocess |

> [!TIP]
> **Use local mode** for most use cases. For server mode setup, see [`../local_server/README.md`](../local_server/README.md).

## 5. Config Files (`openspace/config/`)

Layered system — later files override earlier ones:

| File | Purpose |
|------|---------|
| `config_grounding.json` | Backend settings, smart tool retrieval, tool quality, skill discovery |
| `config_agents.json` | Agent definitions, backend scope, max iterations |
| `config_mcp.json` | MCP servers OpenSpace connects to as a client |
| `config_security.json` | Security policies, blocked commands, sandboxing |
| `config_dev.json` | Dev overrides — copy from `config_dev.json.example` (highest priority) |

### Agent config (`config_agents.json`)

```json
{ "agents": [{ "name": "GroundingAgent", "backend_scope": ["shell", "mcp", "web"], "max_iterations": 30 }] }
```

| Field | Description | Default |
|-------|-------------|---------|
| `backend_scope` | Enabled backends | `["gui", "shell", "mcp", "system", "web"]` |
| `max_iterations` | Max execution cycles | `20` |
| `visual_analysis_timeout` | Timeout for visual analysis (seconds) | `30.0` |

### Backend & tool config (`config_grounding.json`)

| Section | Key Fields | Description |
|---------|-----------|-------------|
| `shell` | `mode`, `timeout`, `conda_env`, `working_dir` | `"local"` (default) or `"server"`, command timeout (default: `60`s) |
| `gui` | `mode`, `timeout`, `driver_type`, `screenshot_on_error` | Local/server mode, automation driver (default: `pyautogui`) |
| `mcp` | `timeout`, `sandbox`, `eager_sessions` | Request timeout (`30`s), E2B sandbox, lazy/eager server init |
| `tool_search` | `search_mode`, `max_tools`, `enable_llm_filter` | `"hybrid"` (semantic + LLM), max tools to return (`40`), embedding cache |
| `tool_quality` | `enabled`, `enable_persistence`, `evolve_interval` | Quality tracking, self-evolution every N calls (default: `5`) |
| `skills` | `enabled`, `skill_dirs`, `max_select` | Directories to scan, max skills injected per task (default: `2`) |

### Security config (`config_security.json`)

| Field | Description | Default |
|-------|-------------|---------|
| `allow_shell_commands` | Enable shell execution | `true` |
| `blocked_commands` | Platform-specific blacklists (common/linux/darwin/windows) | `rm -rf`, `shutdown`, `dd`, etc. |
| `sandbox_enabled` | Enable sandboxing for all operations | `false` |
| Per-backend overrides | Shell, MCP, GUI, Web each have independent security policies | Inherit global |
