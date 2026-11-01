#!/usr/bin/env python3
"""Summarize OpenSpace Terminal-Bench run directories."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any


_RUNS_DIR = Path("benchmarks/terminal_bench/runs")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _tail_text(path: Path, limit: int = 12000) -> str:
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    return text[-limit:]


def _reward(result: dict[str, Any]) -> float | None:
    rewards = (result.get("verifier_result") or {}).get("rewards") or {}
    value = rewards.get("reward")
    return value if isinstance(value, (int, float)) else None


def _metadata(result: dict[str, Any]) -> dict[str, Any]:
    metadata = (result.get("agent_result") or {}).get("metadata") or {}
    return metadata if isinstance(metadata, dict) else {}


def _exception_type(result: dict[str, Any]) -> str | None:
    exception = result.get("exception_info")
    if not isinstance(exception, dict):
        return None
    return str(exception.get("exception_type") or exception.get("type") or "Exception")


def _combined_tail(trial_dir: Path) -> str:
    parts = [
        _tail_text(trial_dir / "agent" / "openspace-stdout.txt"),
        _tail_text(trial_dir / "agent" / "openspace-stderr.txt"),
        _tail_text(trial_dir / "exception.txt"),
        _tail_text(trial_dir / "verifier" / "test-stdout.txt"),
    ]
    return "\n".join(part for part in parts if part)


def _failure_reason(trial_dir: Path, result: dict[str, Any]) -> str:
    reward = _reward(result)
    metadata = _metadata(result)
    exception_type = _exception_type(result)
    text = _combined_tail(trial_dir)
    lowered = text.lower()

    if exception_type:
        if "max_output_tokens" in lowered or "max_output_tokens" in exception_type.lower():
            return "MAX_OUTPUT"
        if metadata.get("return_code") == 124 or "return code 124" in lowered:
            return "RUN_TIMEOUT"
        if "agenttimeouterror" in lowered:
            return "AGENT_TIMEOUT"
        return exception_type
    if reward == 1.0:
        return "PASS"
    if reward == 0.0:
        return "VERIFY_FAIL"
    return "NO_VERIFIER"


def _sqlite_counts(db_path: Path) -> dict[str, int]:
    if not db_path.exists():
        return {}
    tables = (
        "evidence_packets",
        "trigger_jobs",
        "admission_results",
        "evolution_candidates",
        "evolution_actions",
        "evolution_action_failures",
        "validation_results",
        "behavior_eval_results",
    )
    counts: dict[str, int] = {}
    try:
        with sqlite3.connect(db_path) as conn:
            for table in tables:
                try:
                    counts[table] = int(
                        conn.execute(f"select count(*) from {table}").fetchone()[0]
                    )
                except sqlite3.Error:
                    continue
            try:
                rows = conn.execute(
                    "select status, count(*) from trigger_jobs group by status"
                ).fetchall()
                for status, count in rows:
                    counts[f"trigger_jobs:{status}"] = int(count)
            except sqlite3.Error:
                pass
    except sqlite3.Error:
        return counts
    return counts


def _json_list(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except Exception:
            return [value] if value else []
        value = decoded
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item)]
    return []


def _short_text(value: Any, limit: int = 220) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _sqlite_details(db_path: Path) -> dict[str, Any]:
    if not db_path.exists():
        return {}
    details: dict[str, Any] = {}
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            try:
                details["triggers"] = [
                    {
                        "type": row["trigger_type"],
                        "reason": row["reason"],
                        "tags": _json_list(row["reason_tags_json"]),
                        "status": row["status"],
                        "attempts": row["attempts"],
                        "error": row["error"],
                        "result_ref": row["result_ref"],
                    }
                    for row in conn.execute(
                        "select trigger_type, reason, reason_tags_json, status, "
                        "attempts, error, result_ref from trigger_jobs "
                        "order by created_at"
                    )
                ]
            except sqlite3.Error:
                pass
            try:
                details["decisions"] = [
                    {
                        "action": row["proposed_action"],
                        "policy": row["candidate_policy"],
                        "tags": _json_list(row["reason_tags_json"]),
                        "noop": row["noop_reason"],
                        "risks": _json_list(row["risks_json"]),
                        "summary": _short_text(row["reason_summary"]),
                    }
                    for row in conn.execute(
                        "select proposed_action, candidate_policy, reason_tags_json, "
                        "noop_reason, risks_json, reason_summary from decision_rationales "
                        "order by created_at"
                    )
                ]
            except sqlite3.Error:
                pass
            try:
                details["admissions"] = [
                    {
                        "outcome": row["outcome"],
                        "hard_failures": _json_list(row["hard_failures_json"]),
                        "warnings": _json_list(row["warnings_json"]),
                    }
                    for row in conn.execute(
                        "select outcome, hard_failures_json, warnings_json "
                        "from admission_results order by created_at"
                    )
                ]
            except sqlite3.Error:
                pass
            try:
                details["validations"] = [
                    {
                        "outcome": row["outcome"],
                        "failures": _json_list(row["deterministic_failures_json"]),
                        "warnings": _json_list(row["semantic_warnings_json"]),
                        "changed_files": _json_list(row["changed_files_json"]),
                    }
                    for row in conn.execute(
                        "select outcome, deterministic_failures_json, "
                        "semantic_warnings_json, changed_files_json "
                        "from validation_results order by checked_at"
                    )
                ]
            except sqlite3.Error:
                pass
            try:
                details["behavior_evals"] = [
                    {
                        "outcome": row["outcome"],
                        "failures": _json_list(row["failures_json"]),
                        "warnings": _json_list(row["warnings_json"]),
                    }
                    for row in conn.execute(
                        "select outcome, failures_json, warnings_json "
                        "from behavior_eval_results order by checked_at"
                    )
                ]
            except sqlite3.Error:
                pass
            try:
                details["actions"] = [
                    {
                        "type": row["action_type"],
                        "status": row["commit_status"],
                        "skill_id": row["skill_id"],
                        "failure": row["failure_reason"],
                        "changed_files": _json_list(row["changed_files_json"]),
                    }
                    for row in conn.execute(
                        "select action_type, commit_status, skill_id, failure_reason, "
                        "changed_files_json from evolution_actions order by created_at"
                    )
                ]
            except sqlite3.Error:
                pass
    except sqlite3.Error:
        return details
    return {key: value for key, value in details.items() if value}


def _skill_count(trial_dir: Path) -> int:
    skill_dir = trial_dir / "agent" / "evolved-skills"
    if not skill_dir.exists():
        return 0
    return sum(1 for _ in skill_dir.rglob("SKILL.md"))


def _summarize_trial(trial_dir: Path, *, details: bool = False) -> dict[str, Any]:
    result_path = trial_dir / "result.json"
    result = _load_json(result_path)
    task_name = str(result.get("task_name") or trial_dir.name).split("/")[-1]
    metadata = _metadata(result)
    counts = _sqlite_counts(trial_dir / "agent" / "openspace-evidence.db")
    row = {
        "task": task_name,
        "trial": trial_dir.name,
        "reward": _reward(result),
        "reason": _failure_reason(trial_dir, result),
        "return_code": metadata.get("return_code"),
        "internal_failure": metadata.get("openspace_internal_failure"),
        "iterations": ((result.get("agent_result") or {}).get("rollout_details") or {}).get(
            "iterations"
        ),
        "recording": (trial_dir / "agent" / "recordings").exists(),
        "evidence_packets": counts.get("evidence_packets", 0),
        "trigger_jobs": counts.get("trigger_jobs", 0),
        "trigger_running": counts.get("trigger_jobs:running", 0),
        "trigger_completed": counts.get("trigger_jobs:completed", 0),
        "admissions": counts.get("admission_results", 0),
        "candidates": counts.get("evolution_candidates", 0),
        "actions": counts.get("evolution_actions", 0),
        "action_failures": counts.get("evolution_action_failures", 0),
        "evolved_skills": _skill_count(trial_dir),
        "seed_enabled": metadata.get("replay_seed_enabled"),
        "seed_db": metadata.get("replay_seed_evidence_uploaded"),
        "seed_skills": metadata.get("replay_seed_evolved_skills_uploaded"),
        "seed_runtime_db": metadata.get("replay_seed_runtime_db_uploaded"),
        "path": str(trial_dir),
    }
    if details:
        row["details"] = _sqlite_details(trial_dir / "agent" / "openspace-evidence.db")
    return row


def _resolve_run(path_or_name: str) -> Path:
    path = Path(path_or_name).expanduser()
    if path.exists():
        return path
    candidate = _RUNS_DIR / path_or_name
    if candidate.exists():
        return candidate
    raise FileNotFoundError(f"Run directory not found: {path_or_name}")


def summarize_run(run_dir: Path, *, details: bool = False) -> list[dict[str, Any]]:
    trials = sorted(path.parent for path in run_dir.glob("*/result.json"))
    return [_summarize_trial(trial, details=details) for trial in trials]


def print_summary(
    run_dir: Path,
    rows: list[dict[str, Any]],
    *,
    show_paths: bool,
    show_details: bool,
) -> None:
    solved = sum(1 for row in rows if row["reward"] == 1.0)
    errored = sum(1 for row in rows if row["reason"] not in {"PASS", "VERIFY_FAIL"})
    reasons = Counter(str(row["reason"]) for row in rows)
    print(f"\n{run_dir}")
    print(f"solved={solved}/{len(rows)} errors={errored} reasons={dict(reasons)}")
    print(
        "task                         reward reason        rc   int  ev  trig run cand act skills seed path"
    )
    for row in rows:
        seed = "-"
        if row["seed_enabled"] is not None:
            seed = (
                f"db={int(bool(row['seed_db']))},"
                f"rt={int(bool(row['seed_runtime_db']))},"
                f"sk={int(bool(row['seed_skills']))}"
            )
        path = row["path"] if show_paths else row["trial"]
        print(
            f"{row['task'][:28]:28s} "
            f"{str(row['reward']):6s} "
            f"{row['reason'][:12]:12s} "
            f"{str(row['return_code']):4s} "
            f"{str(row['internal_failure']):4s} "
            f"{row['evidence_packets']:3d} "
            f"{row['trigger_jobs']:4d} "
            f"{row['trigger_running']:3d} "
            f"{row['candidates']:4d} "
            f"{row['actions']:3d} "
            f"{row['evolved_skills']:6d} "
            f"{seed:10s} "
            f"{path}"
        )
        if show_details and row.get("details"):
            _print_details(row["details"])


def _print_details(details: dict[str, Any]) -> None:
    for label in (
        "triggers",
        "decisions",
        "admissions",
        "validations",
        "behavior_evals",
        "actions",
    ):
        items = details.get(label)
        if not items:
            continue
        print(f"    {label}:")
        for item in items:
            rendered = ", ".join(
                f"{key}={_short_text(value, 140)}"
                for key, value in item.items()
                if value not in (None, "", [], {})
            )
            print(f"      - {rendered}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="+", help="Run directory path or name under runs/")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    parser.add_argument("--show-paths", action="store_true")
    parser.add_argument("--details", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload: dict[str, list[dict[str, Any]]] = {}
    for name in args.runs:
        run_dir = _resolve_run(name)
        rows = summarize_run(run_dir, details=args.details)
        payload[str(run_dir)] = rows
        if not args.json:
            print_summary(
                run_dir,
                rows,
                show_paths=args.show_paths,
                show_details=args.details,
            )
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
