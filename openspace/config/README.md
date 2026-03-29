# 🔧 Configuration Guide

All configuration applies to both Path A (host agent) and Path B (standalone). Configure once before the first run.

## 1. API Keys (`.env`)

> [!NOTE]
> Create a `.env` file and add your API keys (refer to [`.env.example`](../../.env.example)). When used via host agent (Path A), LLM keys are auto-detected from your agent's config — `.env` is mainly needed for standalone mode.

## 2. Environment Variables

Set via `.env`, MCP config `env` block, or system environment. OpenSpace reads these at startup.

| Variable | Required | Description |
|----------|----------|-------------|
| `OPENSPACE_HOST_SKILL_DIRS` | Path A only | Your agent's skill directories (comma-separated). Auto-registered on startup. |
| `OPENSPACE_WORKSPACE` | Recommended | OpenSpace project root. Used for recording logs and workspace resolution. |
| `OPENSPACE_API_KEY` | No | Cloud API key (`sk-xxx`). Register at https://open-space.cloud. |
| `OPENSPACE_MODEL` | No | LLM model override (default: auto-detected or `openrouter/anthropic/claude-sonnet-4.5`). |
| `OPENSPACE_MAX_ITERATIONS` | No | Max agent iterations per task (default: `20`). |
| `OPENSPACE_BACKEND_SCOPE` | No | Enabled backends, comma-separated (default: all — `shell,gui,mcp,web,system`). |

### Advanced env overrides (rarely needed)

| Variable | Description |
|----------|-------------|
| `OPENSPACE_LLM_API_KEY` | LLM API key (auto-detected from host agent in Path A) |
| `OPENSPACE_LLM_API_BASE` | LLM API base URL |
| `OPENSPACE_LLM_EXTRA_HEADERS` | Extra HTTP headers for LLM requests (JSON string) |
| `OPENSPACE_LLM_CONFIG` | Arbitrary litellm kwargs (JSON string) |
| `OPENSPACE_API_BASE` | Cloud API base URL (default `https://open-space.cloud/api/v1`) |
| `OPENSPACE_CONFIG_PATH` | Custom grounding config JSON (deep-merged with defaults) |
| `OPENSPACE_SHELL_CONDA_ENV` | Conda environment for shell backend |
| `OPENSPACE_SHELL_WORKING_DIR` | Working directory for shell backend |
| `OPENSPACE_MCP_SERVERS_JSON` | MCP server definitions (JSON string, merged into `mcpServers`) |
| `OPENSPACE_ENABLE_RECORDING` | Record execution traces (default: `true`) |
| `OPENSPACE_LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

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
> **Use local mode** for most use cases. For server mode setup (how to enable, platform-specific deps, remote VM control), see [`../local_server/README.md`](../local_server/README.md).

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

## 6. Supported LLM Providers

OpenSpace uses [LiteLLM](https://docs.litellm.ai/docs/providers) for model routing. Set your model via `--model` flag, `OPENSPACE_MODEL` env var, or host agent config.

| Provider | Model format | API Key env var |
|----------|-------------|-----------------|
| OpenRouter | `openrouter/anthropic/claude-sonnet-4.5` | `OPENROUTER_API_KEY` |
| Anthropic | `anthropic/claude-sonnet-4-5` | `ANTHROPIC_API_KEY` |
| OpenAI | `openai/gpt-4o` | `OPENAI_API_KEY` |
| DeepSeek | `deepseek/deepseek-chat` | `DEEPSEEK_API_KEY` |
| MiniMax | `minimax/MiniMax-M2.7` | `MINIMAX_API_KEY` |

### MiniMax

[MiniMax](https://platform.minimax.io) offers high-performance LLMs with 204K context at competitive pricing.

**Available models:**

| Model | Context | Description |
|-------|---------|-------------|
| `MiniMax-M2.7` | 204K | Peak performance, ultimate value |
| `MiniMax-M2.7-highspeed` | 204K | Same performance, faster and more agile |

**Quick setup:**

```bash
# Set your API key
export MINIMAX_API_KEY=your-key-here

# Run with MiniMax
openspace --model "minimax/MiniMax-M2.7" --query "your task"
```

**API docs:** [OpenAI-compatible API](https://platform.minimax.io/docs/api-reference/text-openai-api)

> [!NOTE]
> MiniMax temperature is automatically clamped to `(0.0, 1.0]` by OpenSpace. The `response_format` parameter is not supported and is automatically removed.

