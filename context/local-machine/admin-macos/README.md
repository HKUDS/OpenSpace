## Admin macOS Local Context

This directory keeps machine-specific snapshots that are useful for future
deployment, migration, and debugging on other machines, while avoiding noise in
the repo root.

Included here:

- `mcp/repo-local.mcp.json`
  - snapshot of the repo-local MCP wiring that was used during local debugging
- `gdpval_bench/...`
  - selected benchmark result snapshots that were useful during local call-rate
    and provider-path investigation

Intentional choices:

- absolute local paths are preserved because they are part of the context
- localhost API base values are preserved because they document the local stack
- secrets are not preserved
  - any benchmark config copied here has API keys redacted

Intentionally omitted from this snapshot:

- SQLite/WAL benchmark databases
- raw recording directories
- ad hoc `tmp/` scratch files

Those source locations remain local-only and are ignored via `.gitignore`.
