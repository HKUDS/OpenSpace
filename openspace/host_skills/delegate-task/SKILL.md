---
name: delegate-task
description: Delegate tasks to OpenSpace — a full-stack autonomous worker for coding, DevOps, web research, and desktop automation, backed by an extensive MCP tool and skill library. Skills auto-improve through use, reducing token consumption over time. A cloud community lets agents share and collectively evolve reusable skills.
---

# Delegate Tasks to OpenSpace

OpenSpace is connected as an MCP server. Whether the host uses `stdio`, `sse`, or `streamable-http`, you have the same 4 tools available: `execute_task`, `search_skills`, `fix_skill`, `upload_skill`.

## When to use

- **You lack the capability** — the task requires tools or capabilities beyond what you can access
- **You tried and failed** — you produced incorrect results; OpenSpace may have a tested skill for it
- **Complex multi-step task** — the task involves many steps, tools, or environments that benefit from OpenSpace's skill library and orchestration
- **User explicitly asks** — user requests delegation to OpenSpace

## Tools

### execute_task

Delegate a task to OpenSpace. It will search for relevant skills, execute, and auto-evolve skills if needed.

```
execute_task(task="Monitor Docker containers, find the highest memory one, restart it gracefully", search_scope="all")
```

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `task` | yes | — | Task instruction in natural language |
| `search_scope` | no | `"all"` | Local + cloud; falls back to local-only if no API key |
| `max_iterations` | no | `20` | Max agent iterations — increase for complex tasks, decrease for simple ones |

Check response for `evolved_skills`. If present with `upload_ready: true`, decide whether to upload (see "When to upload" below).

```json
{
  "status": "success",
  "response": "Task completed successfully",
  "evolved_skills": [
    {
      "skill_dir": "/path/to/skills/new-skill",
      "name": "new-skill",
      "origin": "captured",
      "change_summary": "Captured reusable workflow pattern",
      "upload_ready": true
    }
  ]
}
```

### search_skills

Search for available skills before deciding whether to handle a task yourself or delegate.

```
search_skills(query="docker container monitoring", source="all")
```

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `query` | yes | — | Search query (natural language or keywords) |
| `source` | no | `"all"` | Local + cloud; falls back to local-only if no API key |
| `limit` | no | `20` | Max results |
| `auto_import` | no | `true` | Auto-download top cloud skills locally |

### fix_skill

Manually fix a broken skill.

```
fix_skill(
  skill_dir="/path/to/skills/weather-api",
  direction="The API endpoint changed from v1 to v2, update all URLs and add the new 'units' parameter"
)
```

| Parameter | Required | Description |
|-----------|----------|-------------|
| `skill_dir` | yes | Path to skill directory (must contain SKILL.md) |
| `direction` | yes | What's broken and how to fix — be specific |

Response has `upload_ready: true` → decide whether to upload.

### upload_skill

Upload a skill to the cloud community. For evolved/fixed skills, metadata is pre-saved — just provide `skill_dir` and `visibility`.

```
upload_skill(
  skill_dir="/path/to/skills/weather-api",
  visibility="public"
)
```

For new skills (no auto metadata — defaults apply, but richer metadata improves discoverability):

```
upload_skill(
  skill_dir="/path/to/skills/my-new-skill",
  visibility="public",
  origin="imported",
  tags=["weather", "api"],
  created_by="my-bot",
  change_summary="Initial upload of weather API skill"
)
```

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `skill_dir` | yes | — | Path to skill directory (must contain SKILL.md) |
| `visibility` | no | `"public"` | `"public"` or `"private"` |
| `origin` | no | auto | How the skill was created |
| `parent_skill_ids` | no | auto | Parent skill IDs |
| `tags` | no | auto | Tags |
| `created_by` | no | auto | Creator |
| `change_summary` | no | auto | What changed |

### When to upload

| Situation | Action |
|-----------|--------|
| Skill was originally from the cloud | Upload back as `"public"` — return the improvement to the community |
| Fix/evolution is generally useful | Upload as `"public"` |
| Fix/evolution is project-specific | Upload as `"private"`, or skip |
| User says to share | Upload with the visibility the user wants |

## Notes

- `execute_task` may take minutes — this is expected for multi-step tasks.
- If `execute_task` times out, first check the host's MCP timeout settings. Changing from `stdio` to HTTP (`sse` or `streamable-http`) does not remove host-side per-call time limits.
- `upload_skill` requires a cloud API key; if it fails, the evolved skill is still saved locally.
- After every OpenSpace call, **tell the user** what happened: task result, any evolved skills, and your upload decision.
