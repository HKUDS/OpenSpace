# Codex MCP Guard Design

**Date:** 2026-04-19

**Goal**

Add a repo-owned diagnostics and cleanup system for Codex Desktop MCP residue, focused on `openspace.mcp_proxy` and `SkyComputerUseClient mcp`, with a launcher-friendly command surface for status inspection, threshold checks, event history, and bounded cleanup.

**Problem**

Current evidence shows large numbers of stale MCP child processes accumulating under a single long-lived Codex Desktop `app-server`. The residue is not limited to OpenSpace; `openspace.mcp_proxy` and `SkyComputerUseClient mcp` both accumulate. This looks more like host lifecycle leakage than intentional caching, but we want a repeatable diagnostic system before treating it as a cleanup-only problem.

**Non-Goals**

- Do not modify Codex Desktop internals.
- Do not kill the main `codex app-server`.
- Do not clean unrelated MCP servers or arbitrary child processes.
- Do not auto-restart Codex Desktop in the first iteration.
- Do not assume every high process count is bad without supporting evidence.

## Architecture

The system will be a repo-owned guard utility with two layers:

1. A read-only diagnostics layer that inventories relevant MCP residue, classifies process ownership, records health state, and reports whether the situation looks like normal session activity, stale residue, or an operator-attention condition.
2. A bounded cleanup layer that only targets stale child processes matching explicit markers and thresholds, with dry-run support and a clear event trail.

This follows the same shape as the Shadowrocket Guard reference: a single script with `status / check / clean / tail / help` commands and a `daemon` mode. The first implementation will emphasize safe manual operations and state visibility; any autonomous cleanup will be gated by explicit thresholds and launcher flags.

## Components

### 1. Guard Script

Add a new script under `scripts/` that owns all MCP residue diagnostics and cleanup behavior.

Responsibilities:

- inspect the current Codex Desktop `app-server`
- enumerate target child processes under that host
- classify `openspace.mcp_proxy` processes by mode and age
- count `SkyComputerUseClient mcp` residue alongside OpenSpace residue
- write structured state and event logs
- expose manual commands and daemon mode

Proposed command surface:

- `status`: show latest known state
- `check`: perform one fresh sample, no cleanup
- `clean`: perform bounded cleanup if thresholds say it is safe/reasonable
- `tail`: show recent events
- `help`: print usage
- `daemon`: run a sampling loop and maintain state files

### 2. State Directory

Store runtime outputs in a dedicated repo-local directory, similar to the Shadowrocket Guard pattern.

Proposed directory:

- `logs/codex_mcp_guard/`

Proposed files:

- `state.json`
- `events.jsonl`
- `samples.jsonl`

Purpose:

- `state.json`: latest status summary for launcher and humans
- `events.jsonl`: transitions and cleanup actions
- `samples.jsonl`: periodic snapshots for later debugging

### 3. Launcher Integration

Integrate the guard into the existing repo launch path so the user can inspect or clean without remembering a separate script path.

Initial integration points:

- `scripts/codex-desktop-evolution`
- `scripts/codex-openspace`

Initial launcher affordances:

- pass-through subcommands like `guard status`, `guard check`, `guard clean`, `guard tail`
- a simple environment flag to enable background daemon mode in phase 2

We will not make launcher startup always block on the guard. It should remain an operational tool, not a boot gate.

## Detection Model

The diagnostics layer will classify only explicitly targeted MCP residue:

- `openspace.mcp_proxy --kind main --transport stdio`
- `openspace.mcp_proxy --kind evolution --transport stdio`
- `SkyComputerUseClient mcp`

It will capture:

- PID, PPID, elapsed time, command
- whether the process is a descendant of the current `codex app-server`
- for `openspace.mcp_proxy`, whether it is in `direct` or `daemon` mode when recoverable from env/command context
- open stdio/pipe/socket attachments where feasible
- counts grouped by type and by owning host PID

### Residue Heuristics

The guard will not equate "exists" with "bad". It will calculate:

- total count per target type
- count under current host session
- count older than a stale-age threshold
- oldest age per target type
- whether many processes share the same parent
- whether many old processes show only startup-time activity in their paired logs

Initial status levels:

- `ok`: counts below warning thresholds and no stale-age anomalies
- `warning`: counts or ages suggest residue, but no cleanup requested
- `threshold_exceeded`: thresholds crossed and cleanup is recommended
- `cleaned`: a cleanup action ran successfully
- `cleanup_failed`: a cleanup action was attempted and failed

## Cleanup Policy

Cleanup must be narrow, reversible in intent, and auditable.

Initial cleanup target:

- stale `openspace.mcp_proxy` and `SkyComputerUseClient mcp` child processes belonging to the current `codex app-server`

Initial cleanup exclusions:

- do not target `codex app-server`
- do not target unrelated MCP processes
- do not touch shared daemon metadata for unrelated state dirs
- do not kill fresh processes below age threshold unless explicitly forced

Initial cleanup decision model:

- only on explicit `clean` command in v1
- dry-run available
- age threshold plus count threshold must both be visible in state
- process markers must match a known allowlist

This keeps v1 focused on safe operator-driven remediation while preserving a clean path to threshold-triggered cleanup in phase 2.

## Read-Only Localization Goal

The diagnostics system should also help answer whether this is caching strategy or bug.

The expected evidence model is:

- if processes are intentionally cached, we should see bounded counts, reuse over time, and session-scoped ownership patterns
- if they are leaked, we should see monotonic accumulation, many old idle descendants under one long-lived host, and little or no evidence of reuse

The guard will not hardcode that verdict, but it will surface the exact facts needed to support it.

## Testing

We will add tests for:

- process parsing and classification
- threshold/status evaluation
- cleanup target filtering
- state/event file generation
- launcher pass-through behavior

Where live process behavior is too environment-specific, unit tests will operate on mocked `ps`/`lsof`-style snapshots and synthetic sample data.

## Rollout

### Phase 1

- add the guard script
- add `status / check / clean / tail / help`
- write state/events/samples
- integrate launcher pass-through
- keep cleanup manual only

### Phase 2

- add `daemon` loop
- add threshold-based recommendation and cooldown tracking
- expose an opt-in auto-clean mode only after phase 1 diagnostics prove the thresholds are stable

## Risks

- Over-cleaning active children if thresholds are too aggressive
- Misclassifying current-session processes as stale when Codex Desktop is actively using them
- Assuming all residue under one parent is safe to kill

Mitigations:

- default to read-only plus explicit `clean`
- dry-run support
- allowlist-based targeting
- threshold and age gates
- event logging for every action

## Decision

Proceed with a Shadowrocket-Guard-style MCP residue guard as a repo-owned operational tool, integrated into the launcher, with v1 prioritizing read-only diagnostics and manual bounded cleanup for `openspace.mcp_proxy` and `SkyComputerUseClient mcp`.
