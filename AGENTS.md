# AGENTS

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
