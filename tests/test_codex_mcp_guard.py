from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from openspace import codex_mcp_guard as guard


def _host_row(pid: int = 10) -> dict[str, object]:
    return {
        "pid": pid,
        "ppid": 1,
        "etime": "10:00:00",
        "command": "/Applications/Codex.app/Contents/Resources/codex app-server --analytics-default-enabled",
    }


def test_build_snapshot_counts_target_processes_by_type() -> None:
    snapshot = guard.build_snapshot(
        process_rows=[
            _host_row(),
            {
                "pid": 11,
                "ppid": 10,
                "etime": "09:00:00",
                "command": "python -m openspace.mcp_proxy --kind main --transport stdio",
            },
            {
                "pid": 12,
                "ppid": 10,
                "etime": "09:00:00",
                "command": "python -m openspace.mcp_proxy --kind evolution --transport stdio",
            },
            {
                "pid": 13,
                "ppid": 10,
                "etime": "08:00:00",
                "command": "SkyComputerUseClient mcp",
            },
            {
                "pid": 14,
                "ppid": 99,
                "etime": "08:00:00",
                "command": "python -m openspace.mcp_proxy --kind main --transport stdio",
            },
        ],
        now_ts=1_700_000_000,
        stale_age_seconds=3600,
        warn_total_count=3,
        threshold_total_count=4,
        warn_stale_count=2,
        threshold_stale_count=3,
    )

    assert snapshot["host"]["pid"] == 10
    assert snapshot["counts"]["openspace_main"] == 1
    assert snapshot["counts"]["openspace_evolution"] == 1
    assert snapshot["counts"]["computer_use_mcp"] == 1
    assert snapshot["counts"]["targets_total"] == 3
    assert snapshot["stale"]["total"] == 3
    assert snapshot["status"] == "threshold_exceeded"
    assert snapshot["assessment"] == "host_lifecycle_leak_suspected"


def test_select_cleanup_candidates_targets_only_stale_allowlisted_children() -> None:
    candidates = [
        guard.ManagedChild(
            pid=101,
            ppid=10,
            kind="openspace_main",
            age_seconds=5000,
            command="python -m openspace.mcp_proxy --kind main --transport stdio",
            is_descendant=True,
        ),
        guard.ManagedChild(
            pid=102,
            ppid=10,
            kind="computer_use_mcp",
            age_seconds=6000,
            command="SkyComputerUseClient mcp",
            is_descendant=True,
        ),
        guard.ManagedChild(
            pid=103,
            ppid=10,
            kind="openspace_main",
            age_seconds=30,
            command="python -m openspace.mcp_proxy --kind main --transport stdio",
            is_descendant=True,
        ),
        guard.ManagedChild(
            pid=104,
            ppid=10,
            kind="other",
            age_seconds=7000,
            command="unrelated mcp",
            is_descendant=True,
        ),
        guard.ManagedChild(
            pid=105,
            ppid=44,
            kind="openspace_evolution",
            age_seconds=7000,
            command="python -m openspace.mcp_proxy --kind evolution --transport stdio",
            is_descendant=False,
        ),
    ]

    selected = guard.select_cleanup_candidates(
        candidates,
        age_threshold_seconds=3600,
        include_kinds={"openspace_main", "openspace_evolution", "computer_use_mcp"},
    )

    assert [item.pid for item in selected] == [102, 101]


def test_record_snapshot_writes_state_event_and_sample(tmp_path: Path) -> None:
    guard_dir = tmp_path / "logs" / "codex_mcp_guard"
    snapshot = {
        "status": "warning",
        "host": {"pid": 10},
        "counts": {"targets_total": 5},
        "stale": {"total": 4},
        "assessment": "host_lifecycle_leak_suspected",
    }

    guard.record_snapshot(guard_dir, snapshot, event_type="check")

    state = json.loads((guard_dir / "state.json").read_text(encoding="utf-8"))
    events = (guard_dir / "events.jsonl").read_text(encoding="utf-8").strip().splitlines()
    samples = (guard_dir / "samples.jsonl").read_text(encoding="utf-8").strip().splitlines()

    assert state["status"] == "warning"
    assert len(events) == 1
    assert len(samples) == 1
    assert json.loads(events[0])["type"] == "check"


def test_parser_accepts_json_after_subcommand() -> None:
    parser = guard.build_parser()
    args = parser.parse_args(["check", "--json"])

    assert args.command == "check"
    assert args.json is True


def _make_stub_repo(tmp_path: Path, launcher_name: str) -> tuple[Path, Path, Path]:
    repo_root = tmp_path / "stub-repo"
    scripts_dir = repo_root / "scripts"
    scripts_dir.mkdir(parents=True)
    source_launcher = Path(__file__).resolve().parents[1] / "scripts" / launcher_name
    launcher_path = scripts_dir / launcher_name
    launcher_path.write_text(source_launcher.read_text(encoding="utf-8"), encoding="utf-8")
    launcher_path.chmod(0o755)

    python_path = repo_root / ".venv" / "bin" / "python"
    capture_path = tmp_path / f"{launcher_name}.json"
    python_path.parent.mkdir(parents=True)
    python_path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"path = {str(capture_path)!r}\n"
        "payload = {\n"
        "  'argv': sys.argv,\n"
        "  'cwd': os.getcwd(),\n"
        "}\n"
        "with open(path, 'w', encoding='utf-8') as fh:\n"
        "    json.dump(payload, fh)\n",
        encoding="utf-8",
    )
    python_path.chmod(0o755)
    return repo_root, launcher_path, capture_path


def test_codex_desktop_evolution_guard_subcommand_invokes_guard_script(tmp_path: Path) -> None:
    repo_root, launcher_path, capture_path = _make_stub_repo(tmp_path, "codex-desktop-evolution")

    env = os.environ.copy()
    env.pop("OPENSPACE_CODEX_HOME", None)
    env.pop("PRIMARY_CODEX_HOME", None)
    subprocess.run(
        [str(launcher_path), "guard", "status"],
        check=True,
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
    )

    payload = json.loads(capture_path.read_text(encoding="utf-8"))
    assert payload["argv"][1].endswith("/scripts/codex_mcp_guard.py")
    assert payload["argv"][2:] == ["status"]


def test_codex_openspace_guard_subcommand_invokes_guard_script_without_env_file(tmp_path: Path) -> None:
    repo_root, launcher_path, capture_path = _make_stub_repo(tmp_path, "codex-openspace")

    subprocess.run(
        [str(launcher_path), "guard", "check"],
        check=True,
        cwd=repo_root,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )

    payload = json.loads(capture_path.read_text(encoding="utf-8"))
    assert payload["argv"][1].endswith("/scripts/codex_mcp_guard.py")
    assert payload["argv"][2:] == ["check"]
