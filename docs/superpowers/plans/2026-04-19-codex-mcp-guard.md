# Codex MCP Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a launcher-friendly diagnostics and cleanup system for stale Codex Desktop MCP child processes, focused on `openspace.mcp_proxy` and `SkyComputerUseClient mcp`.

**Architecture:** Add one repo-owned Python guard script that inventories current Codex Desktop `app-server` MCP residue, writes structured state/events/samples, and supports `status`, `check`, `clean`, `tail`, `help`, and `daemon` commands. Integrate the script into the existing repo launchers as a `guard` subcommand, and keep v1 cleanup manual and allowlist-based.

**Tech Stack:** Python 3, existing shell launchers, `ps`/`lsof`/`pgrep`-style host commands, `pytest`.

---

### Task 1: Create Guard Test Coverage

**Files:**
- Create: `tests/test_codex_mcp_guard.py`
- Modify: `tests/conftest.py` if shared helpers are needed
- Test: `tests/test_codex_mcp_guard.py`

- [ ] **Step 1: Write failing tests for process classification**

```python
def test_sample_snapshot_counts_target_processes_by_type(tmp_path: Path) -> None:
    snapshot = guard.build_snapshot(
        process_rows=[
            {"pid": 10, "ppid": 1, "etime": "10:00", "command": "/Applications/Codex.app/... app-server --analytics-default-enabled"},
            {"pid": 11, "ppid": 10, "etime": "09:00", "command": "python -m openspace.mcp_proxy --kind main --transport stdio"},
            {"pid": 12, "ppid": 10, "etime": "09:00", "command": "python -m openspace.mcp_proxy --kind evolution --transport stdio"},
            {"pid": 13, "ppid": 10, "etime": "08:00", "command": "SkyComputerUseClient mcp"},
        ],
        now_ts=1_700_000_000,
    )

    assert snapshot["counts"]["openspace_main"] == 1
    assert snapshot["counts"]["openspace_evolution"] == 1
    assert snapshot["counts"]["computer_use_mcp"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/pytest -q tests/test_codex_mcp_guard.py -k snapshot_counts_target_processes_by_type`
Expected: FAIL because the guard module/test helper does not exist yet.

- [ ] **Step 3: Add failing tests for cleanup targeting**

```python
def test_cleanup_targets_only_stale_allowlisted_children() -> None:
    candidates = [
        guard.ManagedChild(pid=101, ppid=10, kind="openspace_main", age_seconds=5000, command="python -m openspace.mcp_proxy --kind main --transport stdio"),
        guard.ManagedChild(pid=102, ppid=10, kind="computer_use_mcp", age_seconds=6000, command="SkyComputerUseClient mcp"),
        guard.ManagedChild(pid=103, ppid=10, kind="openspace_main", age_seconds=30, command="python -m openspace.mcp_proxy --kind main --transport stdio"),
        guard.ManagedChild(pid=104, ppid=10, kind="other", age_seconds=7000, command="unrelated mcp"),
    ]

    selected = guard.select_cleanup_candidates(
        candidates,
        age_threshold_seconds=3600,
        max_target_count=1,
        include_kinds={"openspace_main", "openspace_evolution", "computer_use_mcp"},
    )

    assert [item.pid for item in selected] == [101, 102]
```

- [ ] **Step 4: Run test to verify it fails**

Run: `./.venv/bin/pytest -q tests/test_codex_mcp_guard.py -k cleanup_targets_only_stale_allowlisted_children`
Expected: FAIL because candidate selection is not implemented yet.

- [ ] **Step 5: Add failing launcher integration test**

```python
def test_codex_desktop_evolution_guard_subcommand_invokes_guard_script(tmp_path: Path) -> None:
    # Assert that `scripts/codex-desktop-evolution guard status`
    # execs the repo guard entrypoint instead of codex app/codex exec.
```
```

- [ ] **Step 6: Run test to verify it fails**

Run: `./.venv/bin/pytest -q tests/test_codex_mcp_guard.py -k guard_subcommand`
Expected: FAIL because launcher pass-through is not implemented yet.

### Task 2: Implement Guard Script

**Files:**
- Create: `scripts/codex_mcp_guard.py`
- Modify: `scripts/cleanup_openspace_daemons.py`
- Test: `tests/test_codex_mcp_guard.py`

- [ ] **Step 1: Implement normalized target model and snapshot builder**

```python
@dataclass
class ManagedChild:
    pid: int
    ppid: int
    kind: str
    age_seconds: int
    command: str

def build_snapshot(process_rows: list[dict[str, object]], now_ts: int) -> dict[str, object]:
    # classify app-server, openspace main/evolution, and SkyComputerUseClient mcp
    ...
```

- [ ] **Step 2: Run focused tests**

Run: `./.venv/bin/pytest -q tests/test_codex_mcp_guard.py -k "snapshot_counts_target_processes_by_type"`
Expected: PASS

- [ ] **Step 3: Implement status/check/tail/help/clean/daemon command handlers**

```python
def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "status":
        ...
    elif args.command == "check":
        ...
    elif args.command == "clean":
        ...
```

- [ ] **Step 4: Reuse and narrow existing cleanup logic**

```python
def select_cleanup_candidates(...):
    # only current app-server descendants
    # only allowlisted kinds
    # only age-threshold-matching stale processes
```

- [ ] **Step 5: Run focused tests**

Run: `./.venv/bin/pytest -q tests/test_codex_mcp_guard.py -k "cleanup_targets_only_stale_allowlisted_children or status"`
Expected: PASS

### Task 3: Integrate Launchers

**Files:**
- Modify: `scripts/codex-desktop-evolution`
- Modify: `scripts/codex-openspace`
- Test: `tests/test_codex_mcp_guard.py`

- [ ] **Step 1: Add `guard` subcommand pass-through**

```bash
if [[ "${1:-}" == "guard" ]]; then
  shift
  exec "$REPO_PYTHON" "$REPO_ROOT/scripts/codex_mcp_guard.py" "$@"
fi
```

- [ ] **Step 2: Keep existing app/exec behavior unchanged**

Run: inspect branches for `app` and default execution in both launchers.
Expected: only the new `guard` fast-path is added ahead of current logic.

- [ ] **Step 3: Run launcher-focused tests**

Run: `./.venv/bin/pytest -q tests/test_codex_mcp_guard.py -k guard_subcommand`
Expected: PASS

### Task 4: Verify End-to-End Behavior

**Files:**
- Modify: `docs/global-codex-integration.md`
- Test: `tests/test_codex_mcp_guard.py`, `tests/test_global_mcp_wrapper_installation.py`, `tests/test_mcp_preflight.py`

- [ ] **Step 1: Document the new operational commands**

```markdown
- `./scripts/codex-desktop-evolution guard status`
- `./scripts/codex-desktop-evolution guard check`
- `./scripts/codex-desktop-evolution guard clean --dry-run`
```

- [ ] **Step 2: Run the targeted test suite**

Run: `./.venv/bin/pytest -q tests/test_codex_mcp_guard.py tests/test_global_mcp_wrapper_installation.py tests/test_mcp_preflight.py`
Expected: PASS

- [ ] **Step 3: Run one manual diagnostics smoke**

Run: `./scripts/codex-desktop-evolution guard check`
Expected: emits a structured current-state snapshot without killing processes.

- [ ] **Step 4: Run one manual cleanup dry-run smoke**

Run: `./scripts/codex-desktop-evolution guard clean --dry-run`
Expected: prints only allowlisted stale candidates and no destructive action.
