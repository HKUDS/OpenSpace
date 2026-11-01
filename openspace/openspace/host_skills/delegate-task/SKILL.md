---
name: delegate-task
description: Delegate tasks to OpenSpace — a full-stack autonomous worker for coding, DevOps, web research, and desktop automation, backed by an extensive MCP tool and skill library. Skills auto-improve through use, reducing token consumption over time. A cloud community lets agents share and collectively evolve reusable skills.
---

# Delegate Tasks to OpenSpace

OpenSpace is connected as an MCP server. Whether the host uses `stdio`, `sse`, or `streamable-http`, you have the same tools available: `cloud_auth_flow`, `execute_task`, `search_skills`, `cloud_browse_skills`, `fix_skill`, `upload_skill`.

## When to use

- **You lack the capability** — the task requires tools or capabilities beyond what you can access
- **You tried and failed** — you produced incorrect results; OpenSpace may have a tested skill for it
- **Complex multi-step task** — the task involves many steps, tools, or environments that benefit from OpenSpace's skill library and orchestration
- **User explicitly asks** — user requests delegation to OpenSpace

## Tools

### cloud_auth_flow

Set up OpenSpace cloud access for cloud skill search or skill upload.

Use it only when the user asks for cloud features, or when a cloud operation reports a missing/invalid key.

Ask for `email` and `agent_name`. Ask for `password` only through secure secret input; it must be 8 to 72 characters. Do not ask for passcode/OTP because this tool does not accept one.

If secure password input is available, call:

```
cloud_auth_flow(
  action="bootstrap_agent_key",
  email="user@example.com",
  password="<securely-collected-password>",
  agent_name="openspace-local-agent"
)
```

If secure password input is not available, ask the user to run:

```bash
openspace-cloud-auth bootstrap-agent-key --email user@example.com --agent-name openspace-local-agent
```

After setup, report only whether the key was saved and verified. Never print or repeat passwords, bearer tokens, or raw API keys.

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

Search locally installed skills before deciding whether to handle a task yourself or delegate.

```
search_skills(query="docker container monitoring")
```

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `query` | yes | — | Search query (natural language or keywords) |
| `limit` | no | `20` | Max results |

Use `search_skills` for local discovery. If you need cloud results, use `cloud_browse_skills` so you can inspect packages and choose the skill explicitly.

### cloud_browse_skills

Use this single stepwise tool for LLM-guided cloud package/skill selection. Continue calling the same tool with the returned `next_actions[].action`.

Recommended flow:

```
cloud_browse_skills(
  action="search_skills",
  query="browser login automation",
  limit=5
)
```

Inspect `results[]`: each item has `cloud_skill_id`, `title`, `summary`, `package_id`, and `package_path`.

Use package discovery only when you need package outlines before choosing a skill:

```
cloud_browse_skills(
  action="recall",
  query="browser login automation",
  limit=5
)
cloud_browse_skills(
  action="pull_projection",
  search_id="<search_id>",
  package_ids=["<package_id>"]
)
```

Inspect `pulls[].packages[]`, `pulls[].skills[]`, `projection_hash`, and `root_package_path`. This is JSON projection, not the real zip files.

If you need concrete skill ranking inside one package, use the scoped skill-first search:

```
cloud_browse_skills(
  action="search_skills",
  package_id="<package_id>",
  query="browser login automation"
)
```

Before import, optionally inspect exact metadata:

```
cloud_browse_skills(action="fetch_skill_detail", cloud_skill_id="<cloud_skill_id>")
```

Choose or create the local taxonomy path:

```
cloud_browse_skills(
  action="local_placement",
  query="browser login automation"
)
cloud_browse_skills(
  action="local_placement",
  local_category_path="technology/computing/browser-automation"
)
```

Inspect `existing_path_candidates`, `new_child_path_examples`, and
`local_category_path_policy`. You may choose an existing path or create a nearby
new child path. For DERIVED/CAPTURED suggestions, put the selected path in
`local_category_path`.

Import the exact chosen cloud skill with the selected local path:

```
cloud_browse_skills(
  action="import_skill",
  cloud_skill_id="<cloud_skill_id>",
  local_category_path="technology/computing/browser-automation/login"
)
```

Choose `local_category_path` as a local package taxonomy path. It uses the same
classification style as cloud package paths, but is stored independently. It can
start from a cloud-like path and diverge with finer local child paths.

If the package outline or bundled artifacts are needed, import the package bundle explicitly:

```
cloud_browse_skills(action="import_package_bundle", package_id="<package_id>")
```

Do not use package bundle import as the default search step. Use it only after a package has been selected and you need package outline files or bundled artifacts.

### fix_skill

Run a manual FIX job for a broken skill through OpenSpace evolution. The tool first creates a TriggerJob, then asks OpenSpace to drain that exact job through the evolution engine. It never directly edits the skill.

```
fix_skill(
  skill_dir="/path/to/skills/weather-api",
  direction="The upstream endpoint path changed; update all URLs and add the new 'units' parameter"
)
```

| Parameter | Required | Description |
|-----------|----------|-------------|
| `skill_dir` | yes | Path to skill directory (must contain SKILL.md) |
| `direction` | yes | What's broken and how to fix — be specific |

Only treat the skill as repaired when `status` is `fixed`. If the result is `accepted_audit_only`, `rejected`, or `failed`, do not call `upload_skill` automatically; report the job/action IDs and reason to the user.

### upload_skill

Upload a trusted skill to the cloud community. Public and private uploads both require a matching `trusted` record in the local SkillStore; provisional and unknown skills remain local. For committed evolved skills, lineage metadata is pre-saved; provide `skill_dir` and `visibility`. For non-fix uploads without pre-saved placement, use `upload_skill` as a step-by-step cloud package picker before uploading. The cloud path is separate from the local `local_category_path`.

Interactive cloud placement flow:

1. Call `upload_skill(skill_dir=...)` without `cloud_package_path`; inspect `domain_index.sub_domain_nodes` and `interaction_flow`.
2. Call `upload_skill(skill_dir=..., cloud_sub_domain_package_id=...)`; inspect one bounded subtree.
3. Choose either `subtree.selectable_regular_packages[].package_path`, or create one new child path by appending one segment under `subtree.creatable_parent_packages[].package_path`.
4. Call `upload_skill(skill_dir=..., visibility=..., cloud_package_path=...)`; the tool resolves the path to confirmed UUID placement, saves `.upload_meta.json`, revalidates, then uploads.

New cloud package paths are allowed, but only as one new regular package segment under an eligible parent. Do not upload directly to domain/sub-domain paths, and do not try to create multiple missing segments in one upload.

```
upload_skill(
  skill_dir="/path/to/skills/weather-api",
  visibility="private",
  cloud_package_path="Technology/Computing/API clients"
)
```

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `skill_dir` | yes | — | Path to skill directory (must contain SKILL.md) |
| `visibility` | no | `"private"` | `"public"` or `"private"` |
| `cloud_package_path` | for non-fix uploads without saved placement | auto | Agent-selected existing regular package path, or one new child regular package segment under an eligible parent |
| `cloud_sub_domain_package_id` | no | — | Browse one upload subtree before selecting/creating `cloud_package_path` |
| `cloud_package_query` | no | — | Filter cloud package picker results |
| `cloud_package_path_prefix` | no | — | Expand/filter one cloud path prefix |
| `cloud_package_limit` | no | `12` | Maximum picker rows returned |
| `origin` | no | auto | How the skill was created |
| `parent_local_skill_ids` | no | auto | Local parent skill IDs; OpenSpace resolves cloud parent IDs before upload |

### When to upload

| Situation | Action |
|-----------|--------|
| Skill is provisional or missing from SkillStore | Keep it local; use it successfully until it becomes trusted |
| Skill was originally from the cloud | Upload as `"private"` unless the user explicitly asks to share the improvement |
| Trusted fix/evolution is generally useful | Upload as `"private"` during broader testing; use `"public"` only with explicit sharing intent |
| Fix/evolution is project-specific | Upload as `"private"`, or skip |
| User says to share | Upload with the visibility the user wants |

## Notes

- `execute_task` may take minutes — this is expected for multi-step tasks.
- If `execute_task` times out, first check the host's MCP timeout settings. Changing from `stdio` to HTTP (`sse` or `streamable-http`) does not remove host-side per-call time limits.
- `upload_skill` requires a cloud API key; if it fails, the evolved skill is still saved locally.
- `SKILL_NOT_TRUSTED`, `SKILL_TRUST_UNKNOWN`, and `SKILL_RECORD_PATH_MISMATCH` stop locally before cloud package browsing or upload.
- After every OpenSpace call, **tell the user** what happened: task result, any evolved skills, and your upload decision.
