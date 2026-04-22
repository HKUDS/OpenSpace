# Global Codex Integration

This document records the machine-wide Codex integration used in the local customized OpenSpace setup.

## What Is Global vs Repo-Tracked

### Repo-tracked

These are versioned in this repository:

- OpenSpace runtime changes
- split routing for main LLM vs skill embeddings
- project launchers under `scripts/`
- integration docs under `docs/`
- installer for global MCP wrappers:
  - `scripts/install-global-codex-openspace`

### Local-only

These live under `~/.codex` on the local machine and are intentionally not committed directly:

- `~/.codex/config.toml`
- `~/.codex/AGENTS.md`
- `~/.codex/bin/openspace-global-mcp`
- `~/.codex/bin/openspace-evolution-global-mcp`

These files are machine-specific because they may contain:

- absolute local paths
- user-specific Codex settings
- local MCP wiring
- secrets or provider-specific credentials

## Why `.mcp.json` Was Not Committed

The repo-local `.mcp.json` is also intentionally excluded from GitHub because it is a local override with:

- absolute paths into this machine
- host-agent-specific skill directories
- local workflow assumptions

That file is useful for local experimentation, but it is not the stable source of truth for the global Codex integration.

## Canonical Global Setup

The intended machine-wide setup is:

1. Global Codex config points MCP servers to:
   - `~/.codex/bin/openspace-global-mcp`
   - `~/.codex/bin/openspace-evolution-global-mcp`
2. Those wrapper scripts:
   - detect the current project directory
   - normalize it to the git repo root when possible
   - set `OPENSPACE_WORKSPACE`
   - when Codex Desktop only provides `PWD=/` and no explicit workspace, fall back to `OPENSPACE_MCP_PROXY_MODE=direct` instead of creating shared daemons scoped to `/`
   - route project skills to `~/.codex/projects/<repo>/skills`
   - include common global skills from `~/.codex/skills`
   - call the shared `stdio` proxy entrypoint
   - default `openspace` to `OPENSPACE_MCP_PROXY_MODE=daemon`
   - default `openspace_evolution` to `OPENSPACE_MCP_PROXY_MODE=daemon`
   - place per-instance daemon state under `OPENSPACE_MCP_DAEMON_STATE_DIR` unless an override is already set
3. Global `~/.codex/AGENTS.md` tells Codex:
   - to prefer project skill routing
   - to auto-run sidecar evolution for non-trivial repo work
   - to treat missing `git init` as a repo bootstrap issue

The reusable template for these instructions is:

- `docs/templates/codex-openspace-agents-template.md`

## Daemon / Proxy V1

The global and local launchers keep the same wrapper names and the same MCP config shape, but they now sit in front of a shared-daemon topology:

- Codex still talks to stdio wrapper scripts.
- The wrapper scripts keep the existing command names but route into `openspace.mcp_proxy`.
- Both main and evolution now default to `OPENSPACE_MCP_PROXY_MODE=daemon`.
- The proxy path resolves or starts a per-instance daemon using `OPENSPACE_MCP_DAEMON_STATE_DIR`.
- The daemon owns the long-lived OpenSpace engine and serves it over localhost transport.

This keeps the external Codex contract stable while reducing the number of overlapping OpenSpace engine processes.

### Fallbacks

The proxy surface supports two internal overrides:

- `OPENSPACE_MCP_PROXY_MODE=direct` restores the old direct stdio behavior for debugging or rollback.
- `OPENSPACE_MCP_DAEMON_STATE_DIR=/custom/path` moves daemon state to a different local directory.
- `OPENSPACE_MCP_PROXY_IDLE_TIMEOUT_SECONDS=<seconds>` lets stdio proxy processes reap themselves sooner than the daemon timeout; it falls back to `OPENSPACE_MCP_IDLE_TIMEOUT_SECONDS`, then defaults to `180`.

### Invalid Workspace Fallback

If Codex Desktop launches the global wrappers without an explicit `OPENSPACE_WORKSPACE` and only exposes `PWD=/`, the generated wrappers now:

- do not export `OPENSPACE_WORKSPACE=/`
- force `OPENSPACE_MCP_PROXY_MODE=direct`
- route skills through the safe default bucket only:
  - `~/.codex/projects/default/skills`
  - `~/.codex/skills`
- print a warning that shared daemons were disabled and workspace-aware tools must rely on explicit `workspace_dir`

This is an intentional containment path to prevent shared daemon records keyed to `workspace=/`.

The repo-local `scripts/codex-openspace` helper writes the same daemon defaults into the generated profile so local and global setups stay aligned.

### Daemon State Metadata

Each per-key daemon writes a JSON record under `OPENSPACE_MCP_DAEMON_STATE_DIR` named like:

- `main-<instance_key>.json`
- `evolution-<instance_key>.json`

For the main daemon path, the record now distinguishes two lifecycle phases:

- `ready=true`: the daemon is reachable and `list_tools` has succeeded.
- `warmed=true`: background prewarm has completed, so the local embedding backend and candidate cache are ready.

Useful timestamps:

- `started_at`: child process spawn time
- `ready_at`: first confirmed MCP-ready time
- `warmed_at`: prewarm completion time

This makes it possible to tell the difference between:

- daemon is up but still warming
- daemon is fully warmed and ready for low-latency calls

## Reinstalling the Global Wrappers

For a full fresh-machine setup, start with
[`docs/codex-mcp-deployment.md`](codex-mcp-deployment.md).

Use:

```bash
cd /path/to/OpenSpace
./scripts/install-global-codex-openspace
```

This script recreates:

- `~/.codex/bin/openspace-global-mcp`
- `~/.codex/bin/openspace-evolution-global-mcp`

Rerun it after changing wrapper behavior such as workspace fallback or proxy idle timeout handling.

It does **not** overwrite your `~/.codex/config.toml` or `~/.codex/AGENTS.md`.

## Practical Outcome

With the global integration in place:

- opening a new project in Codex does not require per-project `.mcp.json`
- OpenSpace MCP is available globally
- OpenSpace evolution MCP is available globally
- repo-scoped skill routing and sidecar evolution use the current project automatically

## MCP Guard

The repo launchers now expose a Codex MCP residue guard for operational diagnosis and bounded cleanup of stale child processes under the current Codex Desktop `app-server`.

Commands:

```bash
./scripts/codex-desktop-evolution guard status
./scripts/codex-desktop-evolution guard check
./scripts/codex-desktop-evolution guard clean --dry-run
./scripts/codex-desktop-evolution guard tail
./scripts/codex-desktop-evolution guard daemon
```

The same subcommands are also available through:

```bash
./scripts/codex-openspace guard <subcommand>
```

Scope:

- diagnoses `openspace.mcp_proxy` residue
- diagnoses `SkyComputerUseClient mcp` residue
- only targets allowlisted stale children during cleanup
- never targets the main `codex app-server`

## Related Docs

- `docs/codex-mcp-deployment.md`
- `docs/templates/codex-openspace-agents-template.md`
- `docs/current-routing-flow.md`
- `docs/release-note-local-customization.md`
- `docs/codex-desktop-sidecar-evolution.md`
