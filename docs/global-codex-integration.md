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

Use:

```bash
cd /path/to/OpenSpace
./scripts/install-global-codex-openspace
```

This script recreates:

- `~/.codex/bin/openspace-global-mcp`
- `~/.codex/bin/openspace-evolution-global-mcp`

It does **not** overwrite your `~/.codex/config.toml` or `~/.codex/AGENTS.md`.

## Practical Outcome

With the global integration in place:

- opening a new project in Codex does not require per-project `.mcp.json`
- OpenSpace MCP is available globally
- OpenSpace evolution MCP is available globally
- repo-scoped skill routing and sidecar evolution use the current project automatically

## Related Docs

- `docs/current-routing-flow.md`
- `docs/release-note-local-customization.md`
- `docs/codex-desktop-sidecar-evolution.md`
