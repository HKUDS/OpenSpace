"""Pure lifecycle decisions for one durable portfolio research session.

The proposer owns research decisions until it submits a build.  A build with
an automatic admission follow-up is then a closed execution chain: its B
result is delivered by the driver without another proposer call.  The
functions here inspect durable records only, so the same decision is made
after a process restart.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


TERMINAL_OUTCOMES = frozenset({
    "success", "rejected", "inconclusive", "failed", "unresolved", "mixed",
})
_ACTIVE = frozenset({"queued", "running"})


def _artifact_id(value: object) -> str | None:
    if isinstance(value, Mapping):
        value = value.get("artifact_id", value.get("id"))
    return value if isinstance(value, str) and value else None


def _admission_outcome(ticket: object, active_ticket_ids: set[str]) -> tuple[str, str]:
    status = str(getattr(ticket, "status", "accounting_unknown"))
    identity = str(getattr(ticket, "id", ""))
    if status in _ACTIVE and identity in active_ticket_ids:
        return "pending", "Admission is still running."
    if status in _ACTIVE:
        return "unresolved", "Admission has no active executor or recovery."
    if status == "accounting_unknown" and identity in active_ticket_ids:
        return "pending", "Admission recovery is still running."
    if status == "failed":
        result = getattr(ticket, "result", None)
        detail = result.get("reason", result.get("summary")) if isinstance(result, Mapping) else None
        return "failed", str(detail or "Admission failed.")
    if status == "accounting_unknown":
        return "unresolved", "Admission result could not be reconciled."
    if status != "succeeded":
        return "unresolved", f"Admission has unknown status {status!r}."
    result = getattr(ticket, "result", None)
    if not isinstance(result, Mapping):
        return "unresolved", "Admission completed without a usable verdict."
    fate = result.get("admission_fate", result.get("admission", result.get("verdict")))
    if (fate == "admitted" and result.get("accepted") is True
            and result.get("evidence_tier") == "adaptive_b"):
        return "success", str(result.get("reason") or result.get("summary") or "Candidate admitted.")
    if fate == "rejected":
        return "rejected", str(result.get("reason") or result.get("summary") or "Candidate rejected.")
    if fate == "inconclusive":
        return "inconclusive", str(result.get("reason") or result.get("summary") or "Admission was inconclusive.")
    return "unresolved", str(result.get("reason") or "Admission completed without a recognized verdict.")


def build_chain_results(
    store: object,
    build_ticket_ids: Iterable[str],
    *,
    active_ticket_ids: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Classify each session-owned build and its frozen admission follow-up."""

    active = set(active_ticket_ids)
    followups = {
        row["build_ticket"]: row
        for row in getattr(store, "followups")()
        if row.get("kind") == "admit"
    } if callable(getattr(store, "followups", None)) else {}
    chains: list[dict[str, Any]] = []
    for identity in dict.fromkeys(build_ticket_ids):
        try:
            build = getattr(store, "get_ticket")(identity)
        except KeyError:
            chains.append({
                "build_ticket": identity,
                "status": "unresolved",
                "stage": "build",
                "reason": "The durable build record is missing.",
            })
            continue
        build_status = str(getattr(build, "status", "accounting_unknown"))
        artifact = _artifact_id(getattr(build, "artifact_id", None))
        base = _artifact_id(getattr(build, "arguments", {}).get("base"))
        chain: dict[str, Any] = {
            "build_ticket": identity,
            "artifact_id": artifact,
            "root_artifact_id": base,
            "stage": "build",
        }
        if build_status in _ACTIVE and identity in active:
            chain.update(status="pending", reason="Build is still running.")
            chains.append(chain)
            continue
        if build_status in _ACTIVE:
            chain.update(status="unresolved", reason="Build has no active executor or recovery.")
            chains.append(chain)
            continue
        if build_status == "accounting_unknown" and identity in active:
            chain.update(status="pending", reason="Build recovery is still running.")
            chains.append(chain)
            continue
        if build_status == "failed":
            chain.update(status="failed", reason=str(getattr(build, "error", None) or "Build failed."))
            chains.append(chain)
            continue
        if build_status == "accounting_unknown":
            chain.update(status="unresolved", reason="Build result could not be reconciled.")
            chains.append(chain)
            continue
        if build_status != "succeeded" or artifact is None:
            chain.update(status="failed" if build_status == "succeeded" else "unresolved",
                         reason="Build did not produce a candidate." if build_status == "succeeded"
                         else f"Build has unknown status {build_status!r}.")
            chains.append(chain)
            continue
        followup = followups.get(identity)
        if followup is None:
            chain.update(status="candidate", reason="Candidate was built without automatic admission.")
            chains.append(chain)
            continue
        child_id = followup.get("ticket")
        chain["admission_ticket"] = child_id
        chain["stage"] = "admission"
        followup_status = str(followup.get("status", "unresolved"))
        if child_id is None:
            if followup_status in {"waiting", "queued", "running"}:
                chain.update(status="pending", reason="Admission is waiting to start.")
            elif followup_status == "skipped":
                chain.update(status="inconclusive", reason=str(followup.get("error") or "Admission was skipped."))
            else:
                chain.update(status="unresolved", reason="Admission could not be started.")
            chains.append(chain)
            continue
        try:
            admission = getattr(store, "get_ticket")(child_id)
        except KeyError:
            chain.update(status="unresolved", reason="The durable admission record is missing.")
            chains.append(chain)
            continue
        outcome, reason = _admission_outcome(admission, active)
        control = _artifact_id(getattr(admission, "arguments", {}).get("control"))
        if control is not None:
            chain["root_artifact_id"] = control
        result = getattr(admission, "result", None)
        if isinstance(result, Mapping):
            fate = result.get("admission_fate", result.get("admission", result.get("verdict")))
            if isinstance(fate, str):
                chain["admission_fate"] = fate
        chain.update(status=outcome, reason=reason)
        if outcome != "pending":
            chain["stage"] = "complete"
        chains.append(chain)
    return chains


def inspect_session(
    store: object,
    *,
    owned_ticket_ids: Iterable[str],
    build_ticket_ids: Iterable[str],
    presented_ticket_statuses: Mapping[str, str] | None = None,
    active_ticket_ids: Iterable[str] = (),
    wait_for_results: bool = True,
) -> dict[str, Any]:
    """Return the next durable action without mutating the session or store."""

    owned = tuple(dict.fromkeys(owned_ticket_ids))
    builds = tuple(dict.fromkeys(build_ticket_ids))
    presented = dict(presented_ticket_statuses or {})
    active = set(active_ticket_ids)
    chains = build_chain_results(store, builds, active_ticket_ids=active)
    chained = {row["build_ticket"] for row in chains if "admission_ticket" in row}
    pending = {
        identity for row in chains if row["status"] == "pending"
        for identity in (row.get("build_ticket"), row.get("admission_ticket"))
        if isinstance(identity, str)
    }
    actionable: list[str] = []
    for identity in owned:
        try:
            ticket = getattr(store, "get_ticket")(identity)
        except KeyError:
            continue
        status = str(getattr(ticket, "status", "accounting_unknown"))
        if status in _ACTIVE and identity in active:
            pending.add(identity)
            continue
        if status == "accounting_unknown" and identity in active:
            pending.add(identity)
            continue
        kind = str(getattr(ticket, "kind", ""))
        if kind == "admit" or kind == "build" and identity in chained:
            continue
        signal = "unresolved" if status in _ACTIVE else status
        if signal in {"succeeded", "failed", "accounting_unknown", "unresolved"} and presented.get(identity) != signal:
            actionable.append(identity)
    terminal_chains = [row for row in chains if row["status"] in TERMINAL_OUTCOMES]
    automatic = bool(chains) and len(terminal_chains) == len(chains)
    if actionable:
        action = "wake_proposer"
    elif pending and (wait_for_results or builds):
        action = "wait"
    elif automatic:
        action = "deliver"
    else:
        action = "research"
    return {
        "action": action,
        "pending_ticket_ids": sorted(pending),
        "actionable_ticket_ids": actionable,
        "build_chains": chains,
    }


def final_outcome(build_chains: Iterable[Mapping[str, Any]]) -> str:
    """Aggregate chains without hiding an unresolved or conflicting result."""

    statuses = [str(row.get("status")) for row in build_chains]
    if not statuses:
        return "inconclusive"
    if any(status not in TERMINAL_OUTCOMES for status in statuses) or "unresolved" in statuses:
        return "unresolved"
    distinct = set(statuses)
    return statuses[0] if len(distinct) == 1 else "mixed"


def make_delivery(
    *,
    session_key: str,
    root_artifact_id: str | None,
    build_chains: Iterable[Mapping[str, Any]],
    summary: str = "",
    reason: str = "",
) -> dict[str, Any]:
    """Create the stable public terminal record saved in the session checkpoint."""

    chains = [dict(row) for row in build_chains]
    status = final_outcome(chains)
    accepted = next((row.get("artifact_id") for row in reversed(chains)
                     if row.get("status") == "success"), None)
    roots = [row.get("root_artifact_id") for row in chains if row.get("root_artifact_id")]
    return {
        "status": status,
        "session_key": session_key,
        "root_artifact_id": roots[-1] if roots else root_artifact_id,
        "artifact_id": accepted,
        "summary": summary,
        "reason": reason,
        "build_chains": chains,
    }


def accepted_artifact_id(delivery: Mapping[str, Any] | None) -> str | None:
    """Return the only safe root for another evolution round."""

    if not isinstance(delivery, Mapping):
        return None
    chains = delivery.get("build_chains")
    if not isinstance(chains, list) or not chains:
        return None
    if any(not isinstance(row, Mapping) or row.get("status") not in TERMINAL_OUTCOMES
           or row.get("status") == "unresolved" for row in chains):
        return None
    return _artifact_id(delivery.get("artifact_id"))


def can_start_new_round(delivery: Mapping[str, Any] | None) -> bool:
    """A new automatic round needs a durably admitted candidate."""

    return accepted_artifact_id(delivery) is not None


__all__ = [
    "TERMINAL_OUTCOMES",
    "accepted_artifact_id",
    "build_chain_results",
    "can_start_new_round",
    "final_outcome",
    "inspect_session",
    "make_delivery",
]
