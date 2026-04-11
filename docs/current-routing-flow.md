# Current Routing Flow

This document records the current OpenSpace routing setup for this local environment.

## Effective Split Routing

- Main LLM:
  - model: `gpt-5.4`
  - API base: `http://127.0.0.1:8080/v1`
  - source: `OPENSPACE_LLM_*`
- Skill embeddings:
  - backend: `local`
  - model: `BAAI/bge-small-en-v1.5`
  - source: `OPENSPACE_SKILL_EMBEDDING_*`

This means:

- normal OpenSpace generation and tool-calling still use the OpenAI-compatible provider path
- skill-router semantic re-rank does not depend on remote `/v1/embeddings`
- Codex Desktop main session remains isolated from the sidecar/provider env

## Flow 1: OpenSpace CLI

```mermaid
flowchart LR
    A["User runs ./scripts/openspace.sh"] --> B["Load openspace/.env"]
    B --> C["Set OPENSPACE_LLM_*"]
    B --> D["Set OPENSPACE_SKILL_EMBEDDING_*"]
    C --> E["LLM client"]
    D --> F["SkillRanker"]
    E --> G["sub2api / local OpenAI-compatible gateway<br/>http://127.0.0.1:8080/v1"]
    F --> H["fastembed local model<br/>BAAI/bge-small-en-v1.5"]
    G --> I["GroundingAgent execution"]
    H --> J["BM25 + vector prefilter"]
    J --> I
```

## Flow 2: Codex Desktop With OpenSpace Sidecar

```mermaid
flowchart LR
    A["User runs ./scripts/codex-desktop-evolution app"] --> B["Create isolated CODEX_HOME overlay"]
    B --> C["Main Codex Desktop session"]
    B --> D["openspace_evolution MCP sidecar"]
    C --> E["Normal Codex subscription/API workflow"]
    D --> F["OpenSpace evolution server"]
    F --> G["OPENSPACE_LLM_* -> gpt-5.4 via http://127.0.0.1:8080/v1"]
    F --> H["OPENSPACE_SKILL_EMBEDDING_* -> local fastembed"]
    G --> I["Evolution / skill capture"]
    H --> I
```

## Flow 3: Skill Routing Internals

```mermaid
flowchart LR
    A["Task text"] --> B["Early abstain check"]
    B --> C["BM25 rough rank"]
    C --> D["Local embedding re-rank"]
    D --> E["Top candidate skills"]
    E --> F["Optional LLM selection"]
    F --> G["Injected / selected skills"]
```

## Key Config Inputs

- `OPENSPACE_LLM_API_KEY`
- `OPENSPACE_LLM_API_BASE`
- `OPENSPACE_LLM_OPENAI_STREAM_COMPAT`
- `OPENSPACE_SKILL_EMBEDDING_BACKEND`
- `OPENSPACE_SKILL_EMBEDDING_MODEL`

## Operational Notes

- If the provider does not expose `/v1/embeddings`, the main LLM path still works.
- With the current setup, skill embeddings stay local, so router prefilter remains available.
- If needed later, skill embeddings can be moved to a separate remote endpoint by setting:
  - `OPENSPACE_SKILL_EMBEDDING_BACKEND=remote`
  - `OPENSPACE_SKILL_EMBEDDING_API_KEY`
  - `OPENSPACE_SKILL_EMBEDDING_API_BASE`
  - `OPENSPACE_SKILL_EMBEDDING_MODEL`
