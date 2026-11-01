"""Group repeated Visible-A measurements without pooling programs or profiles."""
from __future__ import annotations

from collections import Counter
import re
from agentpilot.domain import canonical_json

CONTRACT = "same-task-repetition-context-v1"


def task_groups(manifests, artifacts):
    groups = {}
    for manifest in manifests:
        for case in manifest.case_indexes:
            artifact = artifacts.get(case.trace_ref_id)
            prefix = manifest.program_id + ":"
            cell = case.case_key[len(prefix):] if case.case_key.startswith(prefix) else None
            # Published AIME/TB2 cell coordinates retain task/replicate even
            # when infrastructure failed before a trace could be captured.
            coordinate = re.fullmatch(r"visible-a/(.+)/(\d+)", cell or "")
            task = artifact.task_id if artifact else coordinate[1] if coordinate else None
            replicate = artifact.replicate if artifact else int(coordinate[2]) if coordinate else None
            key = (manifest.program_id, manifest.commit_sha, manifest.task_set_id,
                   manifest.measurement_profile_id, task or case.case_key)
            group = groups.setdefault(key, {
                "task_id": task, "program_id": manifest.program_id,
                "commit_sha": manifest.commit_sha,
                "task_set_id": manifest.task_set_id,
                "measurement_profile_id": manifest.measurement_profile_id,
                "attempts_by_cell": {},
            })
            status = case.outcome if case.measurement_status == "valid" else "invalid"
            item = {"replicate": replicate, "cell_id": cell, "case_ref": case.case_ref,
                    "result": status, "measurement_status": case.measurement_status,
                    "trace_available": artifact is not None,
                    "failure_label": case.trusted_failure_label}
            previous = group["attempts_by_cell"].get(cell or case.case_key)
            if previous is not None and previous != item:
                raise ValueError("Conflicting repetition records for the same measurement cell")
            group["attempts_by_cell"][cell or case.case_key] = item
    result = []
    for group in groups.values():
        attempts = sorted(group.pop("attempts_by_cell").values(),
                          key=lambda row: (row["replicate"] is None, row["replicate"] or 0, row["case_ref"]))
        counts = Counter(row["result"] for row in attempts)
        group.update(attempts=attempts, pattern=[row["result"] for row in attempts],
                     counts={key: counts[key] for key in ("pass", "fail", "invalid")},
                     valid_attempts=counts["pass"] + counts["fail"],
                     recorded_attempts=len(attempts))
        result.append(group)
    return sorted(result, key=lambda row: (row["counts"]["fail"] == 0, row["task_id"] or "", row["measurement_profile_id"]))


def matching_group(groups, case_ref):
    return next((group for group in groups
                 if any(row["case_ref"] == case_ref for row in group["attempts"])), None)


def passing_examples(group, *, focus_ref, count):
    if not group:
        return []
    # Replicate order is deterministic, independent of length or apparent
    # explanation quality; do not cherry-pick a short or polished success.
    return [row["case_ref"] for row in group["attempts"]
            if row["result"] == "pass" and row["trace_available"]
            and row["case_ref"] != focus_ref][:count]


def group_navigation(groups, focus_ref, max_bytes=16384):
    """Bound the initial directory, retaining full statistics and read addresses."""
    focus = matching_group(groups, focus_ref)
    ordered = ([focus] if focus else []) + [g for g in groups if g is not focus]
    shown = []
    for group in ordered:
        attempts = group["attempts"][:16]
        focused = next((r for r in group["attempts"] if r["case_ref"] == focus_ref), None)
        if focused and focused not in attempts:
            attempts = [*attempts[:15], focused]
        row = {**group, "attempts": attempts, "pattern": [r["result"] for r in attempts],
               "omitted_attempts": len(group["attempts"]) - len(attempts)}
        if len(canonical_json([*shown, row]).encode()) > max_bytes:
            break
        shown.append(row)
    return shown
