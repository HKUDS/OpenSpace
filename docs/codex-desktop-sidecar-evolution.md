# Codex Desktop Sidecar Evolution Integration

## Goal

This integration keeps the normal Codex Desktop workflow unchanged while moving OpenSpace skill capture and self-evolution onto a separate provider-backed sidecar path.

The target user experience is:

- Main coding still happens in Codex Desktop with the user's normal subscription login.
- OpenSpace does not take over the main task loop.
- Sidecar evolution can be invoked explicitly after a task and spend provider API tokens instead of the main Codex Desktop session.

## Short Answer: Was this mainly an API-level dual routing change?

No.

The final effect does **not** come from a simple in-process "dual route" inside one OpenSpace runtime where:

- coding uses Codex Desktop subscription auth, and
- evolution uses a provider API

That approach is not viable because Codex Desktop subscription login is not exposed to the Python process as a reusable API credential.

Instead, the final implementation uses **process-level split routing**:

- the main coding session remains in Codex Desktop
- self-evolution runs through an isolated OpenSpace sidecar with its own provider-backed MCP server

API compatibility work was still necessary, but it is only one part of the solution.

## Embedding Split Routing

The sidecar now also supports a separate skill-embedding route from the main LLM.

Recommended setup:

```bash
OPENSPACE_MODEL=gpt-5.4
OPENSPACE_LLM_API_KEY=sk-xxx
OPENSPACE_LLM_API_BASE=http://127.0.0.1:8080/v1

OPENSPACE_SKILL_EMBEDDING_BACKEND=local
OPENSPACE_SKILL_EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
```

If you want a dedicated remote endpoint for skill embeddings instead of local
fastembed, set:

```bash
OPENSPACE_SKILL_EMBEDDING_BACKEND=remote
OPENSPACE_SKILL_EMBEDDING_API_KEY=sk-embed-xxx
OPENSPACE_SKILL_EMBEDDING_API_BASE=https://example.com/v1
OPENSPACE_SKILL_EMBEDDING_MODEL=openai/text-embedding-3-small
```

## What Was Implemented

### 1. OpenAI-compatible provider bridge for OpenSpace

File:

- `openspace/llm/client.py`

Why it was needed:

- The third-party relay worked with Codex's `/responses` path.
- OpenSpace uses LiteLLM / OpenAI-style chat completion flows.
- The relay was not reliable enough for OpenSpace's normal streaming path.

What changed:

- Added an OpenAI-compatible streaming fallback that talks directly to `/chat/completions`.
- Reconstructed streamed text, reasoning content, and tool calls into the shape OpenSpace already expects.
- Enabled this path through `OPENSPACE_LLM_OPENAI_STREAM_COMPAT`.

Effect:

- OpenSpace can use the relay provider for evolution workloads.

### 2. Evolution-only MCP sidecar

File:

- `openspace/evolution_mcp_server.py`

Why it was needed:

- The user wanted OpenSpace to handle only post-task evolution and skill capture.
- The main coding loop had to stay outside OpenSpace.

What changed:

- Added a separate MCP server exposing only `evolve_from_context`.
- This server builds context from the current workspace, conversation summary, and git diff.
- It captures reusable skills without becoming the main task executor.

Effect:

- OpenSpace now has a narrow sidecar role instead of replacing the host coding agent.

### 3. Sidecar-capable skill engine without full task recording

File:

- `openspace/tool_layer.py`

Why it was needed:

- The original skill evolution path assumed a fuller OpenSpace task/recording pipeline.
- The new sidecar path needed to create skills without enabling the normal OpenSpace recording flow.

What changed:

- Added `enable_skill_engine_without_recording`.
- Kept execution analysis tied to recording.
- Allowed skill evolution and skill store initialization in sidecar mode without enabling full task recordings.

Effect:

- Sidecar capture can work independently without creating full OpenSpace task sessions.

### 4. Isolated Desktop launcher overlay

File:

- `scripts/codex-desktop-evolution`

Why it was needed:

- The main Codex Desktop session had to keep the user's normal login and defaults.
- The sidecar config had to be added without polluting `~/.codex`.

What changed:

- Created an overlay `CODEX_HOME` at `~/.codex-openspace-desktop`.
- Copied the primary Desktop auth and config base into the overlay.
- Added only one extra MCP server: `openspace_evolution`.
- Scrubbed `OPENSPACE_*` variables before launching the main Codex process.
- Avoided inheriting arbitrary shell state or leaking sidecar credentials into the main coding session.

Effect:

- Main Codex Desktop remains normal.
- The sidecar is available only in the isolated overlay profile.

### 5. Agent instruction trigger for sidecar capture

File:

- `AGENTS.md`

Why it was needed:

- The sidecar should be callable naturally from the Desktop workflow.
- The user should not need to restate the full MCP call every time.

What changed:

- Added a repo-level instruction that maps phrases like `sidecar 自进化一下` to `openspace_evolution.evolve_from_context`.
- Limited the default behavior to:
  - no code changes
  - no main-task takeover
  - at most one high-reuse skill by default

Effect:

- The sidecar behaves like a narrow post-task tool integrated into the normal Desktop workflow.

## Other Supporting Changes

### MCP stdout flush fix

File:

- `openspace/mcp_server.py`

What changed:

- Avoided a final stdout flush crash when the MCP stdio transport closes before Python exit.

### Missing dependency for MCP backend

Files:

- `pyproject.toml`
- `requirements.txt`

What changed:

- Added `websockets>=15.0.0`
- Added `openspace-evolution-mcp` as a console entrypoint

### Frontend dependency refresh

File:

- `frontend/package-lock.json`

What changed:

- Updated `lodash-es`
- Updated `vite`

This was a maintenance fix and is not part of the sidecar architecture itself.

## Architecture Summary

The final architecture is:

1. Codex Desktop remains the main coding agent.
2. Codex Desktop keeps using the user's normal subscription login.
3. A separate overlay profile adds an `openspace_evolution` MCP server.
4. That MCP server runs OpenSpace with provider-backed credentials.
5. OpenSpace uses the provider only for post-task evolution and skill capture.

This means the practical "dual routing" exists at the workflow/process boundary, not as a single shared in-process auth router.

## Usage

Launch the Desktop profile that includes the sidecar:

```bash
cd /Users/admin/PycharmProjects/openspace
./scripts/codex-desktop-evolution app
```

Inside that Desktop session, trigger sidecar capture with:

```text
sidecar 自进化一下
```

or the longer explicit form:

```text
对当前这轮工作做一次 sidecar 自进化。不要改代码，不要接管任务。请调用 openspace_evolution.evolve_from_context，基于当前对话、git diff 和关键改动，自动提炼 task/summary，最多生成 1 个高复用 skill，并告诉我 skill 名称、路径、为什么值得保留。
```

## Result

The implemented effect is:

- normal Codex Desktop coding stays unchanged
- OpenSpace self-evolution is available on demand
- provider token spend is isolated to the sidecar path
- the sidecar does not silently take over the main workflow
