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
3. Global `~/.codex/AGENTS.md` tells Codex:
   - to prefer project skill routing
   - to auto-run sidecar evolution for non-trivial repo work
   - to treat missing `git init` as a repo bootstrap issue

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
