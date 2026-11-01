# Host Skills Integration Guide

This guide covers **agent-specific setup** for integrating OpenSpace. For installation and general concepts, see the [main README](../../README.md#-quick-start).

Run setup commands from `AgentPilot/openspace/`, with that version's dedicated virtual environment activated. For MCP hosts launched outside this shell, use the absolute path to that environment's `openspace-mcp` executable (the `Scripts` directory on Windows).

**Quick recommendation:**
- Use **stdio** if you want the simplest setup.
- For **nanobot**, prefer **SSE** if you want OpenSpace to run as a standalone server.
- For **openclaw**, prefer **streamable-http** for remote HTTP transport.

**Common remote endpoints:**
- Start `openspace-mcp --transport sse --host 127.0.0.1 --port 8080` and use `http://127.0.0.1:8080/sse`
- Start `openspace-mcp --transport streamable-http --host 127.0.0.1 --port 8081` and use `http://127.0.0.1:8081/mcp`

The endpoint is common; the **host config syntax is not**. nanobot uses `tools.mcpServers`, while openclaw uses `openclaw mcp set`.

**Pick your agent:**

| Agent | Setup Guide |
|------------|-------------|
| **[nanobot](https://github.com/HKUDS/nanobot)** | [Setup for nanobot](#setup-for-nanobot) |
| **[openclaw](https://github.com/openclaw/openclaw)** | [Setup for openclaw](#setup-for-openclaw) |
| **Other agents** | Follow the [generic setup](../../README.md#-path-a-for-your-agent) in the main README |

---

## Setup for nanobot

### 1. Copy host skills

```bash
cp -r openspace/host_skills/skill-discovery/ /path/to/nanobot/nanobot/skills/
cp -r openspace/host_skills/delegate-task/ /path/to/nanobot/nanobot/skills/
```

### 2. Option A: stdio (simplest)

```json
{
  "tools": {
    "mcpServers": {
      "openspace": {
        "command": "/path/to/AgentPilot/openspace/.venv/bin/openspace-mcp",
        "toolTimeout": 1200,
        "env": {
          "OPENSPACE_HOST_SKILL_DIRS": "/path/to/nanobot/nanobot/skills",
          "OPENSPACE_WORKSPACE": "/path/to/AgentPilot/openspace",
          "OPENSPACE_CLOUD_MODE": "live",
          "OPENSPACE_CLOUD_API_KEY": "sk-xxx"
        }
      }
    }
  }
}
```

> [!TIP]
> LLM credentials are auto-detected from nanobot's `providers.*` config — no need to set `OPENSPACE_LLM_API_KEY`.

### 3. Option B: remote HTTP transport

```json
{
  "tools": {
    "mcpServers": {
      "openspace": {
        "type": "sse",
        "url": "http://127.0.0.1:8080/sse",
        "toolTimeout": 1200
      }
    }
  }
}
```

Or:

```json
{
  "tools": {
    "mcpServers": {
      "openspace": {
        "type": "streamableHttp",
        "url": "http://127.0.0.1:8081/mcp",
        "toolTimeout": 1200
      }
    }
  }
}
```

`toolTimeout` still matters here. Changing transport to `sse` or `streamableHttp` does **not** remove nanobot's per-call timeout for slow MCP tools.

---

## Setup for openclaw

### 1. Copy host skills

```bash
cp -r openspace/host_skills/skill-discovery/ /path/to/openclaw/skills/
cp -r openspace/host_skills/delegate-task/ /path/to/openclaw/skills/
```

### 2. Option A: stdio via mcporter

openclaw uses [mcporter](https://github.com/steipete/mcporter) as its MCP runtime. Register the server and pass env vars in one command:

```bash
mcporter config add openspace --command "openspace-mcp" \
  --env OPENSPACE_HOST_SKILL_DIRS=/path/to/openclaw/skills \
  --env OPENSPACE_WORKSPACE=/path/to/AgentPilot/openspace \
  --env OPENSPACE_CLOUD_MODE=live \
  --env OPENSPACE_CLOUD_API_KEY=sk-xxx
```

### 3. Option B: remote HTTP transport

```bash
openclaw mcp set openspace '{"url":"http://127.0.0.1:8081/mcp","transport":"streamable-http","connectionTimeoutMs":10000}'
```

If you specifically want legacy SSE instead, OpenClaw also supports:

```bash
openclaw mcp set openspace '{"url":"http://127.0.0.1:8080","connectionTimeoutMs":10000}'
```

`connectionTimeoutMs` controls connection establishment for the remote server. It does **not** guarantee unlimited runtime for a long-running MCP tool call.

---

## Environment Variables (Agent-Specific)

The env vars in each agent's setup above are the most important. For the **full env var list**, config files reference, and advanced settings, see the [Configuration Guide](../config/README.md).

<details>
<summary>What needs <code>OPENSPACE_CLOUD_API_KEY</code>?</summary>

| Capability | Without API Key | With API Key |
|-----------|----------------|--------------|
| `cloud_auth_flow` | ✅ creates/stores an agent key with user credentials | ✅ verifies the current key |
| `execute_task` | ✅ works (local skills only) | ✅ + cloud skill search |
| `search_skills` | ✅ works (local results only) | ✅ local results only |
| `cloud_browse_skills` | ❌ fails | ✅ lets the agent browse cloud packages and import selected skills into the local package taxonomy |
| `fix_skill` | ✅ drains repair job locally | ✅ drains repair job locally |
| `upload_skill` | ❌ fails | ✅ resolves package placement and uploads to cloud |

Run `openspace-cloud-auth bootstrap-agent-key --email you@example.com --agent-name openspace-local-agent` to create or recover an owner-scoped agent key and store `OPENSPACE_CLOUD_MODE=live` together with `OPENSPACE_CLOUD_API_KEY`. `execute_task` falls back to local-only when cloud is off. Explicit cloud-only operations report a configuration error when cloud is disabled.

</details>

---

## How It Works

```
Your Agent (nanobot / openclaw / ...)
  │
  │  MCP protocol (stdio | HTTP/SSE | streamable-http)
  ▼
openspace-mcp                ← cloud, execution, evolution, and upload tools
  ├── cloud_auth_flow          ← register/login and provision cloud agent keys
  ├── execute_task             ← multi-step grounding agent loop
  ├── search_skills            ← local skill search
  ├── cloud_browse_skills      ← LLM-guided cloud package/skill browsing and local taxonomy import
  ├── fix_skill                ← run a manual FIX job through evolution
  └── upload_skill             ← resolve placement and push skill to cloud community
```

The two host skills teach the agent **when and how** to call these tools:

| Skill | MCP Tools | Purpose |
|-------|-----------|---------|
| **skill-discovery** | `cloud_auth_flow` `search_skills` `cloud_browse_skills` | Search local skills, browse cloud packages step by step when needed → decide: follow it yourself, delegate, or skip |
| **delegate-task** | `cloud_auth_flow` `execute_task` `search_skills` `cloud_browse_skills` `fix_skill` `upload_skill` | Set up cloud access, delegate tasks, use local or LLM-guided cloud skill discovery, run skill repair jobs, upload evolved skills |

Skills auto-evolve inside `execute_task` (**FIX** / **DERIVED** / **CAPTURED**). `fix_skill` is only complete when it returns `fixed`; `accepted_audit_only`, `rejected`, and `failed` are not uploadable repairs. After every call, your agent reports results to the user via its messaging tool.

> [!NOTE]
> For full parameter tables, examples, and decision trees, see each skill's SKILL.md directly.
