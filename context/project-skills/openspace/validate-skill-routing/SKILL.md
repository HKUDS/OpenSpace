---
name: validate-skill-routing
description: Diagnose and repair OpenSpace project skill routing in this repo, including skill routing checks, router sanity check, bucket fallback validation, 自动路由, 项目 skill 路由, 路由自检, bucket 回退, 检查自动路由是否生效, and wrapper/index/config drift affecting routing.
---

# Validate Skill Routing

Use this workflow when the task is about verifying or fixing OpenSpace routing behavior for this repository.

Keep the scope narrow:

- validate repo root to project-bucket mapping
- validate project/default/global candidate bucket order
- interpret router output
- distinguish task-wording mismatch from a real routing bug
- inspect wrapper-derived `OPENSPACE_WORKSPACE`
- inspect `OPENSPACE_HOST_SKILL_DIRS` coverage
- detect project skill index drift
- decide whether stale process state requires restart

Do not broaden into generic MCP health or leak cleanup unless the routing checks point there.

## Workflow

### 1. Confirm repo root and expected bucket

Derive the canonical repo root with git, not only the current working directory.

Use the repo basename as the expected bucket name.

For this repository, healthy routing means the bucket resolves to `openspace`.

### 2. Inspect the active routing path

Inspect the active installed launcher and config path, not only repo templates.

Check:

- `~/.codex/bin/openspace-global-mcp`
- `~/.codex/AGENTS.md`
- `~/.codex/config.toml`

Validate:

- launcher canonicalizes workspace to git root when possible
- launcher exports `OPENSPACE_WORKSPACE` from that canonical root
- launcher includes the project skill dir before generic global skills
- the configured MCP entry still points at the launcher you expect

Do not conclude repo-tracked scripts are broken until the installed launchers under `~/.codex` have been checked.

### 3. Run the router with three probes

Use `/Users/admin/.codex/tools/route_codex_skills_via_openspace.py --cwd <cwd> --task "<task>" --json`.

Run these exact probes:

1. Project-bucket probe
   - `修复 provider rollout 验证流程，确认本地 wrapper 和 repo tracked 配置一致`
   - expected: an `openspace` project skill hit

2. Default-bucket probe
   - `重启 MCP 之后做一次健康检查，确认进程祖先和 live tool probe 都恢复正常`
   - expected: a `default` bucket hit

3. Routing-self-check probe
   - `检查 openspace 项目 skill 路由、router sanity check、验证 project/default/global bucket fallback`
   - expected: `validate-skill-routing` hit after this skill exists

If useful, rerun from a nested subdirectory inside the repo to confirm the bucket still resolves to `openspace`.

### 4. Compare router outputs

Compare these fields across probes:

- `bucket`
- `candidate_dirs`
- `prefilter_ranked`
- `selected`
- `selection_record`

Treat the route as healthy only when:

- the project probe hits the intended `openspace` project bucket
- the default probe still hits the intended `default` bucket
- the routing-self-check probe no longer falls back generically

### 5. Localize the failure in this order

If routing is wrong, diagnose in this order:

1. task wording or frontmatter mismatch
2. missing or weak project skill description
3. stale `SKILL_INDEX.md`
4. wrapper or config drift
5. stale long-lived process state

A global fallback on a vague meta-task is not automatically a bug.

Prefer diagnosing wording and metadata mismatch before blaming the router.

### 6. Apply the minimal repair sequence

Apply the smallest fix that explains the failure:

1. strengthen or add project-skill trigger terms in frontmatter description
2. regenerate the openspace project skill index
3. reinstall global wrappers only if launcher behavior changed
4. restart Codex or daemons only if env or wrapper changes require a fresh process state

Do not restart long-lived processes just because a probe was vague and matched a global skill.

### 7. Re-run and record before/after

Re-run the same probes after any repair.

Record:

- the probe strings used
- selected bucket before and after
- whether the installed launcher/config changed
- whether index regeneration was required
- whether restart was required

## Guardrails

- Validate actual installed launchers before diagnosing repo-tracked launcher bugs.
- Treat vague routing-self-check prompts as ambiguous until probe evidence shows a real regression.
- Prefer precise trigger wording when validating a project skill.
- Keep default-bucket behavior intact while improving project-skill coverage.
