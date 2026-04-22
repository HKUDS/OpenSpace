# Codex OpenSpace Auto Invocation Template

Copy this template into `~/.codex/AGENTS.md` for machine-wide behavior, or into
a repository-level `AGENTS.md` for project-only behavior.

This template assumes Codex already has these MCP servers configured:

- `openspace`
- `openspace_evolution`

## OpenSpace Project Skill Routing

When the current working directory is inside a real project repository, prefer
repo-scoped OpenSpace skill captures before generic global skills.

Routing order:

1. exact project bucket: `~/.codex/projects/<repo-basename>/`
2. shared local bucket: `~/.codex/projects/default/`
3. common global skills from `~/.codex/skills`

Routing workflow:

- Derive the project bucket name from the current repo root basename.
- For repo-scoped non-trivial tasks, prefer running:

```bash
$HOME/.codex/tools/route_codex_skills_via_openspace.py --cwd <cwd> --task "<task>" --json
```

- If the router returns selected skills, open only those `SKILL.md` files.
- If the router returns no strong match, fall back to:
  - `~/.codex/projects/<bucket>/SKILL_INDEX.md`
  - `~/.codex/projects/default/SKILL_INDEX.md`
- Only after project/default buckets are considered, add common global skills
  for auxiliary capabilities such as browser automation, Figma, PDF/DOCX,
  GitHub, screenshots, or eval tooling.
- Do not ask the user to choose from a long skill list unless they explicitly
  ask to browse skills.

Prefer project skills for repo-specific workflows, conventions, deployment
paths, migration patterns, and local architecture.

## Codex Desktop Sidecar Evolution

Use this workflow when the user is coding in Codex Desktop with their normal
subscription login and wants OpenSpace to do post-task skill capture through the
isolated `openspace_evolution` sidecar.

Rules:

- Keep the main coding workflow unchanged.
- Do not switch the main Codex Desktop session to a provider-backed model.
- Do not modify code as part of sidecar evolution unless the user separately
  asks for code changes.
- Do not let OpenSpace take over the main task.
- Use the sidecar only for post-task skill capture.
- Prefer at most 1 new high-reuse skill per invocation unless the user
  explicitly asks for more.

When the user asks for sidecar self-evolution, call:

- `openspace_evolution.evolve_from_context`

Trigger phrases:

- `sidecar 自进化一下`
- `做一次 sidecar 自进化`
- `对当前这轮工作做一次 sidecar 自进化`
- `用 sidecar 沉淀一个 skill`
- `基于当前改动做一次 sidecar skill capture`
- `不要改代码，做一次 sidecar 自进化`

If the user uses one of these phrases, default to this workflow automatically
unless they explicitly ask for a different behavior.

Derive the tool inputs from:

- the current conversation
- the current `git diff`
- the key changed files

Behavior:

- Infer a concise `task`.
- Infer a concise but specific `summary`.
- Pass the current repository root as `workspace_dir`.
- Pass the most relevant changed files in `file_paths`.
- Use `max_skills = 1` by default.
- After the tool returns, report the skill name, path, and why it is worth
  keeping.

Recommended user-facing invocation:

```text
对当前这轮工作做一次 sidecar 自进化。不要改代码，不要接管任务。请调用 openspace_evolution.evolve_from_context，基于当前对话、git diff 和关键改动，自动提炼 task/summary，最多生成 1 个高复用 skill，并告诉我 skill 名称、路径、为什么值得保留。
```

## OpenSpace Auto Evolution

If `openspace_evolution` MCP is available, and the assistant is about to
conclude a non-trivial top-level task, run a sidecar evolution pass before
sending the final user-facing completion message.

Use this as the default behavior for:

- code changes
- config changes
- debugging sessions
- meaningful repo analysis with reusable patterns
- code review and audit sessions
- deployment, release, CI/CD, and git workflow tasks
- MCP, provider, auth, login, environment, and migration tasks
- system design or implementation-planning work tied to a real project repo

Do not auto-run evolution for:

- casual chat
- simple factual Q&A
- trivial one-line answers with no reusable work
- tasks where the user explicitly says not to evolve or not to use OpenSpace
- tasks where evolution already ran in the same completion path
- subagent-only threads or delegated worker completions
- general device/how-to questions not tied to a project repo
- broad world-knowledge or report-writing tasks with no concrete local-project
  workflow to capture
- pure audit/review/explanation tasks with no implementation or validation
  evidence
- pure log reading or one-off inspection with no reusable engineering workflow
- doc-only edits unless the user explicitly asks to capture a reusable
  documentation workflow

When auto-running evolution:

- Call `openspace_evolution.evolve_from_context`.
- Do not modify code during the evolution pass.
- Derive `task`, `summary`, and `file_paths` from the current conversation and
  the most relevant changed files.
- Pass `workspace_dir` explicitly as the current repo root.
- Use `max_skills = 1` by default unless the user explicitly asks for more.
- Prefer high-reuse workflow or debugging patterns over narrow one-off captures.

After the evolution call:

- If a skill is created, report the skill name, path, and why it is worth
  keeping.
- If no skill is created, continue with the normal completion message without
  inventing one.

## New Project Bootstrap

If the user wants this sidecar workflow in a new repository, treat it as a
project bootstrap task first.

Bootstrap order:

- Add or update a project launcher before relying on sidecar evolution.
- Point `OPENSPACE_WORKSPACE` at the new repository root.
- Keep the user's main Codex Desktop workflow unchanged.
- Do not modify global `~/.codex` defaults unless the user explicitly asks.

Expected bootstrap outputs:

- a project-level launcher such as `scripts/codex-desktop-evolution`
- a project-level `AGENTS.md` section documenting the sidecar trigger phrases
- sidecar skill output routed to
  `~/.codex-openspace-desktop/projects/<project-name>/skills`

When a user asks to initialize a new project for this workflow, default to:

- creating the launcher first
- wiring `OPENSPACE_WORKSPACE` to the repository root
- preserving the normal Codex Desktop login path
- only then enabling phrases like `sidecar 自进化一下`
