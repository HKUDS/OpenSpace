# AGENTS

## Project Skill Bucket

For this repository, the project-scoped OpenSpace skill bucket is:

- `~/.codex/projects/openspace/skills`
- index: `~/.codex/projects/openspace/SKILL_INDEX.md`

Routing preference for work inside this repo:

1. project bucket `openspace`
2. shared local bucket `default`
3. common global skills

Mirror OpenSpace's own pattern:
- first run `/Users/admin/.codex/tools/route_codex_skills_via_openspace.py`
- prefilter by skill header metadata first
- only open the most likely 1-2 `SKILL.md` files
- avoid scanning every project skill file unless the user explicitly asks

## Codex Desktop Sidecar Evolution

Use this workflow when the user is coding in Codex Desktop with their normal subscription login and wants OpenSpace to do post-task skill capture through the isolated `openspace_evolution` sidecar.

Rules:
- Keep the main coding workflow unchanged.
- Do not switch the main Codex Desktop session to a provider-backed model.
- Do not modify code as part of sidecar evolution unless the user separately asks for code changes.
- Do not let OpenSpace take over the main task.
- Use the sidecar only for post-task skill capture.
- Prefer at most 1 new high-reuse skill per invocation unless the user explicitly asks for more.

When the user asks for sidecar self-evolution, call:
- `openspace_evolution.evolve_from_context`

Trigger phrases:
- `sidecar 自进化一下`
- `做一次 sidecar 自进化`
- `对当前这轮工作做一次 sidecar 自进化`
- `用 sidecar 沉淀一个 skill`
- `基于当前改动做一次 sidecar skill capture`
- `不要改代码，做一次 sidecar 自进化`

If the user uses one of these phrases, default to this workflow automatically unless they explicitly ask for a different behavior.

Derive the tool inputs from:
- the current conversation
- the current `git diff`
- the key changed files

Behavior:
- Infer a concise `task`
- Infer a concise but specific `summary`
- Pass the most relevant changed files in `file_paths`
- Use `max_skills = 1` by default
- After the tool returns, report:
  - the skill name
  - the skill path
  - why the skill is worth keeping

Recommended user-facing invocation:

```text
对当前这轮工作做一次 sidecar 自进化。不要改代码，不要接管任务。请调用 openspace_evolution.evolve_from_context，基于当前对话、git diff 和关键改动，自动提炼 task/summary，最多生成 1 个高复用 skill，并告诉我 skill 名称、路径、为什么值得保留。
```

## New Project Bootstrap

If the user wants this sidecar workflow in a new repository, treat it as a project bootstrap task first.

Bootstrap order:
- Add or update a project launcher before relying on sidecar evolution.
- Point `OPENSPACE_WORKSPACE` at the new repository root.
- Keep the user's main Codex Desktop workflow unchanged.
- Do not modify global `~/.codex` defaults unless the user explicitly asks.

Expected bootstrap outputs:
- a project-level launcher such as `scripts/codex-desktop-evolution`
- a project-level `AGENTS.md` section documenting the sidecar trigger phrases
- sidecar skill output routed to `~/.codex-openspace-desktop/projects/<project-name>/skills`

When a user asks to initialize a new project for this workflow, default to:
- creating the launcher first
- wiring `OPENSPACE_WORKSPACE` to the repository root
- preserving the normal Codex Desktop login path
- only then enabling phrases like `sidecar 自进化一下`
