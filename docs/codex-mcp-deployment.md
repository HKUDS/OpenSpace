# Codex MCP Deployment

This guide bootstraps OpenSpace MCP in a fresh Codex environment after
cloning this repository. It is intended for the customized Codex setup where
OpenSpace is available as global MCP servers and optional sidecar evolution.

## Target Setup

After deployment:

- `openspace` MCP is available from Codex through a global wrapper.
- `openspace_evolution` MCP is available for sidecar skill capture.
- Wrappers resolve the active workspace to the Git repository root when
  possible.
- Shared daemon mode is used by default.
- If Codex Desktop starts without a usable workspace (`PWD=/`), wrappers fall
  back to direct mode instead of creating daemon state for `/`.
- The MCP guard can inspect and clean stale OpenSpace or Computer Use MCP
  child processes.

## 1. Clone And Install

Use the repository and branch that contain this document.

```bash
git clone https://github.com/CCLCK/OpenSpace-1.git ~/PycharmProjects/openspace
cd ~/PycharmProjects/openspace

python3 -m venv .venv
./.venv/bin/python -m pip install -U pip
./.venv/bin/pip install -e .
```

For development and test tooling, install the `dev` extra instead:

```bash
./.venv/bin/pip install -e ".[dev]"
```

Quick install check:

```bash
./.venv/bin/python - <<'PY'
from importlib.metadata import version
print("openspace", version("openspace"))
PY

test -x .venv/bin/openspace-mcp
test -x .venv/bin/openspace-evolution-mcp
```

## 2. Configure Provider Environment

Create `openspace/.env` from the example:

```bash
cp openspace/.env.example openspace/.env
```

Recommended OpenAI-compatible setup:

```bash
OPENSPACE_MODEL=gpt-5.4
OPENSPACE_LLM_API_KEY=sk-xxx
OPENSPACE_LLM_API_BASE=http://127.0.0.1:8080/v1
OPENSPACE_LLM_OPENAI_STREAM_COMPAT=true

OPENSPACE_SKILL_EMBEDDING_BACKEND=local
OPENSPACE_SKILL_EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
```

Notes:

- `OPENSPACE_LLM_*` is the canonical provider surface for OpenSpace.
- Keep skill embeddings local unless you intentionally have a remote
  `/v1/embeddings` endpoint.
- If you change these values later, restart Codex Desktop and any existing MCP
  daemon processes so they do not keep old environment variables.

## 3. Install Global Codex MCP Wrappers

Run the installer from the repository root:

```bash
./scripts/install-global-codex-openspace
```

It creates:

```text
~/.codex/bin/openspace-global-mcp
~/.codex/bin/openspace-evolution-global-mcp
```

Rerun this installer whenever you move the repository or change wrapper
behavior. The generated wrapper scripts contain absolute paths to this checkout.

The installer does not edit `~/.codex/config.toml` or `~/.codex/AGENTS.md`.

## 4. Wire Codex Config

Edit `~/.codex/config.toml` and point Codex at the generated wrappers.
Use literal absolute paths; TOML will not expand `$HOME`.

```toml
[mcp_servers.openspace]
command = "/Users/YOUR_USER/.codex/bin/openspace-global-mcp"
args = []

[mcp_servers.openspace.env]
OPENSPACE_MCP_PROXY_MODE = "daemon"
OPENSPACE_MCP_IDLE_TIMEOUT_SECONDS = "900"
OPENSPACE_MCP_PROXY_IDLE_TIMEOUT_SECONDS = "180"
OPENSPACE_MCP_MAX_DAEMONS_PER_KIND = "8"

[mcp_servers.openspace_evolution]
command = "/Users/YOUR_USER/.codex/bin/openspace-evolution-global-mcp"
args = []

[mcp_servers.openspace_evolution.env]
OPENSPACE_MCP_PROXY_MODE = "daemon"
OPENSPACE_MCP_IDLE_TIMEOUT_SECONDS = "900"
OPENSPACE_EVOLUTION_MCP_IDLE_TIMEOUT_SECONDS = "1800"
OPENSPACE_MCP_PROXY_IDLE_TIMEOUT_SECONDS = "180"
OPENSPACE_MCP_MAX_DAEMONS_PER_KIND = "8"
```

Optional but recommended: keep global Codex instructions in
`~/.codex/AGENTS.md` aligned with this repository's `AGENTS.md`, especially the
rules for:

- project-scoped skill routing through
  `~/.codex/tools/route_codex_skills_via_openspace.py`
- sidecar evolution through `openspace_evolution.evolve_from_context`
- treating missing Git repositories as bootstrap issues for project work

Restart Codex Desktop after editing `config.toml`.

## 5. Configure Codex Auto Invocation

The MCP entries above only make the tools available. Codex decides when to call
them from instructions, so automatic sidecar use also needs an `AGENTS.md`
policy.

For machine-wide behavior, add this to `~/.codex/AGENTS.md`. For one project
only, add it to that project's `AGENTS.md`.

```markdown
## OpenSpace Sidecar Evolution

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

Pass `workspace_dir` explicitly as the current repository root, use
`max_skills = 1` by default, and do not modify code during the evolution pass.
```

If you want Codex to run sidecar evolution automatically at the end of
substantial repo work, add this stricter opt-in rule as well:

```markdown
## OpenSpace Auto Evolution

If `openspace_evolution` MCP is available, and the assistant is about to
conclude a non-trivial repo-scoped task with implementation and verification
evidence, run `openspace_evolution.evolve_from_context` before the final
completion message.

Use `workspace_dir` as the current repo root, pass the most relevant changed
files in `file_paths`, and use `max_skills = 1` by default.

Do not auto-run evolution for casual chat, simple factual Q&A, pure log
reading, pure audit/review/explanation, or doc-only edits unless the user
explicitly asks to capture a reusable documentation workflow.
```

After changing `~/.codex/AGENTS.md`, start a new Codex session so the new
instructions are loaded.

## 6. Verify Deployment

Run static config and wrapper checks:

```bash
cd ~/PycharmProjects/openspace
./.venv/bin/python scripts/check_openspace_mcp_preflight.py \
  --cwd "$PWD" \
  --codex-home ~/.codex \
  --json
```

Run a real Codex MCP session probe:

```bash
./.venv/bin/python scripts/check_openspace_mcp_preflight.py \
  --cwd "$PWD" \
  --codex-home ~/.codex \
  --probe-session \
  --json
```

If the probe passes, open Codex from a project repository and confirm the
`openspace` and `openspace_evolution` MCP servers are available.

## 7. Optional Launch Modes

### Normal Global Mode

Use this for day-to-day Codex Desktop. The global wrappers in `~/.codex/bin`
are used for any trusted project opened by Codex.

This is the recommended default.

### Desktop Sidecar Evolution Profile

Use this when the main Codex Desktop login should stay unchanged, but
`openspace_evolution` should be injected through an isolated overlay profile:

```bash
cd ~/PycharmProjects/openspace
./scripts/codex-desktop-evolution app
```

Then ask Codex to capture a sidecar skill after meaningful work, for example:

```text
sidecar 自进化一下
```

or the longer explicit form:

```text
对当前这轮工作做一次 sidecar 自进化。不要改代码，不要接管任务。请调用 openspace_evolution.evolve_from_context，基于当前对话、git diff 和关键改动，自动提炼 task/summary，最多生成 1 个高复用 skill，并告诉我 skill 名称、路径、为什么值得保留。
```

This profile keeps provider token spend isolated to the evolution sidecar.

### Provider-Backed OpenSpace Codex Profile

Use this when you want an isolated Codex profile where Codex itself is launched
with the OpenSpace provider settings:

```bash
cd ~/PycharmProjects/openspace
./scripts/codex-openspace app
```

This launcher reads `openspace/.env`, writes an isolated profile under
`~/.codex-openspace`, and enables both `openspace` and `openspace_evolution`
MCP servers in daemon mode.

## 8. Operational Guard

The guard is available through both repository launchers:

```bash
./scripts/codex-desktop-evolution guard status
./scripts/codex-desktop-evolution guard check
./scripts/codex-desktop-evolution guard clean --dry-run
./scripts/codex-desktop-evolution guard tail
./scripts/codex-desktop-evolution guard daemon
```

Equivalent:

```bash
./scripts/codex-openspace guard <subcommand>
```

Use `status` first. Use `clean --dry-run` before any real cleanup.

The guard diagnoses:

- `openspace.mcp_proxy` residue
- `SkyComputerUseClient mcp` residue
- stale child processes under the current Codex Desktop `app-server`

It does not target the main `codex app-server`.

## Troubleshooting

### Codex Starts Wrappers With `PWD=/`

If Codex Desktop does not provide a usable workspace, the wrappers intentionally
fall back to direct mode and print a warning. This prevents daemon records keyed
to `/`.

Fix by opening Codex from a trusted project repository, or by using a launcher
that passes an explicit workspace.

### Raw Provider Works But MCP Fails

Treat this as configuration drift until proven otherwise:

1. Confirm `openspace/.env` has the expected `OPENSPACE_LLM_*` values.
2. Confirm `~/.codex/config.toml` points to the new wrapper paths.
3. Restart Codex Desktop.
4. Re-run `scripts/check_openspace_mcp_preflight.py --probe-session`.
5. If needed, inspect daemon state under `~/.codex/state/openspace`.

### Too Many MCP Child Processes

Check before cleaning:

```bash
./scripts/codex-desktop-evolution guard status
./scripts/codex-desktop-evolution guard check
./scripts/codex-desktop-evolution guard clean --dry-run
```

Only run non-dry cleanup after the dry-run output matches the stale processes
you intended to remove.

## Deployment Checklist

- Repository cloned and virtualenv installed.
- `openspace/.env` contains the intended provider and embedding settings.
- `./scripts/install-global-codex-openspace` completed successfully.
- `~/.codex/config.toml` points to the generated wrapper scripts.
- Codex Desktop was restarted.
- Preflight static check passes.
- Preflight `--probe-session` passes.
- `guard status` reports expected process counts.
