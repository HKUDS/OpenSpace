from __future__ import annotations

import json
from pathlib import Path

from openspace.codex_session_scenarios import (
    cleanup_session_artifacts,
    collect_session_family,
    parse_exec_output,
    snapshot_daemons,
)


def _write_session(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "session_meta", "payload": payload}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def test_parse_exec_output_extracts_thread_and_opening_status() -> None:
    output = """
2026-04-13T00:00:00Z WARN unrelated noise
{"type":"thread.started","thread_id":"thread-parent"}
{"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"先做一次 OpenSpace 预检，再开始当前任务。\\n\\nOpenSpace session: ready\\nOpenSpace machine: ready"}}
{"type":"item.completed","item":{"id":"item_2","type":"agent_message","text":"cold-start done"}}
""".strip()

    result = parse_exec_output(output)

    assert result.thread_id == "thread-parent"
    assert result.opening is not None
    assert result.opening.session_status == "ready"
    assert result.opening.machine_status == "ready"
    assert result.opening.fallback_present is False
    assert result.agent_messages[-1] == "cold-start done"


def test_collect_session_family_discovers_descendants(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    parent = sessions_root / "2026/04/13/parent.jsonl"
    child = sessions_root / "2026/04/13/child.jsonl"
    grandchild = sessions_root / "2026/04/13/grandchild.jsonl"
    unrelated = sessions_root / "2026/04/13/unrelated.jsonl"

    _write_session(parent, {"id": "parent-thread"})
    _write_session(
        child,
        {
            "id": "child-thread",
            "source": {
                "subagent": {
                    "thread_spawn": {
                        "parent_thread_id": "parent-thread",
                    }
                }
            },
        },
    )
    _write_session(
        grandchild,
        {
            "id": "grandchild-thread",
            "source": {
                "subagent": {
                    "thread_spawn": {
                        "parent_thread_id": "child-thread",
                    }
                }
            },
        },
    )
    _write_session(unrelated, {"id": "other-thread"})

    family = collect_session_family("parent-thread", sessions_root)

    assert family.thread_ids == {"parent-thread", "child-thread", "grandchild-thread"}
    assert family.session_files == {parent, child, grandchild}


def test_cleanup_session_artifacts_removes_files_and_index_rows(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    session_file = sessions_root / "2026/04/13/parent.jsonl"
    _write_session(session_file, {"id": "parent-thread"})

    session_index = tmp_path / "session_index.jsonl"
    session_index.write_text(
        "\n".join(
            [
                json.dumps({"thread_id": "parent-thread", "session_file": str(session_file)}),
                json.dumps({"thread_id": "keep-thread", "session_file": "/tmp/keep.jsonl"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    cleanup_session_artifacts(
        thread_ids={"parent-thread"},
        session_files={session_file},
        session_index_path=session_index,
    )

    assert not session_file.exists()
    remaining = session_index.read_text(encoding="utf-8")
    assert "parent-thread" not in remaining
    assert "keep-thread" in remaining


def test_snapshot_daemons_filters_by_workspace(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    matching = state_dir / "main-match.json"
    other = state_dir / "main-other.json"

    matching.write_text(
        json.dumps(
            {
                "server_kind": "main",
                "instance_key": "match",
                "pid": 111,
                "port": 9001,
                "workspace": "/tmp/workspace-a",
                "ready": True,
            }
        ),
        encoding="utf-8",
    )
    other.write_text(
        json.dumps(
            {
                "server_kind": "main",
                "instance_key": "other",
                "pid": 222,
                "port": 9002,
                "workspace": "/tmp/workspace-b",
                "ready": True,
            }
        ),
        encoding="utf-8",
    )

    snapshot = snapshot_daemons(state_dir, "/tmp/workspace-a")

    assert set(snapshot) == {"main"}
    assert snapshot["main"]["pid"] == 111
