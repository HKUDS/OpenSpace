from __future__ import annotations

import argparse
import hmac
import json
import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[3]

from flask import Flask, abort, jsonify, request, send_from_directory, url_for

from openspace.recording.action_recorder import analyze_agent_actions, load_agent_actions
from openspace.recording.utils import load_recording_session
from openspace.skill_engine import SkillStore
from openspace.skill_engine.evidence import (
    EvidenceStore,
    resolve_evidence_db_path as resolve_evidence_store_db_path,
    resolve_skill_store_db_path,
)
from openspace.skill_engine.evolution import (
    EvidenceRefAccessError,
    EvolutionAuditService,
)
from openspace.skill_engine.triggers import TriggerStore
from openspace.skill_engine.types import SkillRecord

API_PREFIX = "/api/v1"
PACKAGE_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIST_DIR = PROJECT_ROOT / "apps" / "dashboard" / "dist"
PACKAGED_DASHBOARD_STATIC_DIR = PACKAGE_ROOT / "packaged" / "dashboard"
DASHBOARD_TOKEN_ENV = "OPENSPACE_DASHBOARD_TOKEN"
WORKFLOW_ROOTS = [
    PROJECT_ROOT / "logs" / "recordings",
    PROJECT_ROOT / "logs" / "trajectories",
    PROJECT_ROOT / "benchmarks" / "gdpval" / "results",
    PROJECT_ROOT / "benchmarks" / "terminal_bench" / "runs",
]

PIPELINE_STAGES = [
    {
        "id": "initialize",
        "title": "Initialize",
        "description": "Load LLM, grounding backends, recording, registry, analyzer, and evolver.",
    },
    {
        "id": "select-skills",
        "title": "Skill Selection",
        "description": "Select candidate skills and write selection metadata before execution.",
    },
    {
        "id": "phase-1-skill",
        "title": "Skill Phase",
        "description": "Run the task with injected skill context whenever matching skills exist.",
    },
    {
        "id": "phase-2-fallback",
        "title": "Tool Fallback",
        "description": "Fallback to tool-only execution when the skill-guided phase fails or no skills match.",
    },
    {
        "id": "analysis",
        "title": "Execution Analysis",
        "description": "Persist metadata, trajectory, and post-run execution judgments.",
    },
    {
        "id": "evolution",
        "title": "Skill Evolution",
        "description": "Trigger fix / derived / captured evolution and periodic quality checks.",
    },
]


def _is_loopback_host(host: str | None) -> bool:
    normalized = (host or "").strip().lower()
    return normalized in {"127.0.0.1", "localhost", "::1"}


def _configured_dashboard_token() -> str:
    return os.environ.get(DASHBOARD_TOKEN_ENV, "").strip()


def _request_dashboard_token() -> str:
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[len("Bearer "):].strip()
    return request.headers.get("X-OpenSpace-Dashboard-Token", "").strip()


def create_app(
    *,
    store: SkillStore | None = None,
    db_path: str | Path | None = None,
    evidence_store: EvidenceStore | None = None,
    evidence_db_path: str | Path | None = None,
    evolution_storage_root: str | Path | None = None,
    bind_host: str = "127.0.0.1",
) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["OPENSPACE_BIND_HOST"] = bind_host
    resolved_skill_db_path = _resolve_skill_store_db_path(
        db_path=db_path,
        evolution_storage_root=evolution_storage_root,
    )
    skill_store = store or SkillStore(resolved_skill_db_path)
    resolved_evidence_db_path = _resolve_evidence_db_path(
        evidence_db_path=evidence_db_path,
        db_path=db_path,
        evolution_storage_root=evolution_storage_root,
        skill_store=skill_store,
    )
    audit_evidence_store = evidence_store or EvidenceStore(
        resolved_evidence_db_path,
        allowed_read_roots=_dashboard_evidence_allowed_read_roots(
            evidence_db_path=resolved_evidence_db_path,
            db_path=db_path,
            evolution_storage_root=evolution_storage_root,
        ),
    )
    migration_trigger_store = TriggerStore(evidence_store=audit_evidence_store)
    migration_trigger_store.close()
    audit_service = EvolutionAuditService(
        audit_evidence_store,
        skill_store,
    )

    def get_store() -> SkillStore:
        return skill_store

    def get_audit() -> EvolutionAuditService:
        return audit_service

    @app.before_request
    def enforce_dashboard_auth():
        """Require a bearer token for API access when bound outside loopback.

        Static frontend assets remain reachable so the SPA can load; the API
        (skills, evidence, workflows) is what must not be exposed anonymously.
        """
        if not request.path.startswith(API_PREFIX):
            return None

        token = _configured_dashboard_token()
        configured_host = app.config.get("OPENSPACE_BIND_HOST", "127.0.0.1")
        if _is_loopback_host(configured_host) and not token:
            return None

        if not token:
            return jsonify({
                "status": "error",
                "message": (
                    f"{DASHBOARD_TOKEN_ENV} is required when the dashboard "
                    "API is bound outside loopback."
                ),
            }), 403

        if hmac.compare_digest(_request_dashboard_token(), token):
            return None

        return jsonify({
            "status": "error",
            "message": "Missing or invalid dashboard bearer token.",
        }), 401

    @app.route(f"{API_PREFIX}/health", methods=["GET"])
    def health() -> Any:
        workflows = _discover_workflow_dirs()
        store = get_store()
        return jsonify(
            {
                "status": "ok",
                "project_root": str(PROJECT_ROOT),
                "db_path": str(store.db_path),
                "evidence_db_path": str(audit_evidence_store.db_path),
                "db_exists": store.db_path.exists(),
                "evidence_db_exists": audit_evidence_store.db_path.exists(),
                "frontend_dist_exists": resolve_dashboard_static_dir() is not None,
                "workflow_roots": [str(path) for path in WORKFLOW_ROOTS],
                "workflow_count": len(workflows),
            }
        )

    @app.route(f"{API_PREFIX}/overview", methods=["GET"])
    def overview() -> Any:
        store = get_store()
        skills = list(store.load_all(active_only=False).values())
        workflows = [_build_workflow_summary(path) for path in _discover_workflow_dirs()]
        top_skills = _sort_skills(skills, sort_key="score")[:5]
        recent_skills = _sort_skills(skills, sort_key="updated")[:5]
        average_score = round(
            sum(_skill_score(record) for record in skills) / len(skills), 1
        ) if skills else 0.0
        average_workflow_success = round(
            (sum((item.get("success_rate") or 0.0) for item in workflows) / len(workflows)) * 100,
            1,
        ) if workflows else 0.0

        return jsonify(
            {
                "health": {
                    "status": "ok",
                    "db_path": str(store.db_path),
                    "evidence_db_path": str(audit_evidence_store.db_path),
                    "workflow_count": len(workflows),
                    "frontend_dist_exists": resolve_dashboard_static_dir() is not None,
                },
                "pipeline": PIPELINE_STAGES,
                "skills": {
                    "summary": _build_skill_stats(store, skills),
                    "average_score": average_score,
                    "top": [_serialize_skill(item) for item in top_skills],
                    "recent": [_serialize_skill(item) for item in recent_skills],
                },
                "workflows": {
                    "total": len(workflows),
                    "average_success_rate": average_workflow_success,
                    "recent": workflows[:5],
                },
            }
        )

    @app.route(f"{API_PREFIX}/skills", methods=["GET"])
    def list_skills() -> Any:
        store = get_store()
        active_only = _bool_arg("active_only", True)
        limit = _int_arg("limit", 100)
        sort_key = (_str_arg("sort", "score") or "score").lower()
        skills = list(store.load_all(active_only=active_only).values())
        query = (_str_arg("query", "") or "").strip().lower()
        if query:
            skills = [
                record
                for record in skills
                if query in record.name.lower()
                or query in record.skill_id.lower()
                or query in record.description.lower()
                or any(query in tag.lower() for tag in record.tags)
            ]
        items = [_serialize_skill(item) for item in _sort_skills(skills, sort_key=sort_key)[:limit]]
        return jsonify({"items": items, "count": len(items), "active_only": active_only})

    @app.route(f"{API_PREFIX}/skills/stats", methods=["GET"])
    def skill_stats() -> Any:
        store = get_store()
        skills = list(store.load_all(active_only=False).values())
        return jsonify(_build_skill_stats(store, skills))

    @app.route(f"{API_PREFIX}/skills/<skill_id>", methods=["GET"])
    def skill_detail(skill_id: str) -> Any:
        store = get_store()
        record = store.load_record(skill_id)
        if not record:
            abort(404, description=f"Unknown skill_id: {skill_id}")

        detail = _serialize_skill(record, include_recent_analyses=True)
        detail["recent_analyses"] = [analysis.to_dict() for analysis in store.load_analyses(skill_id=skill_id, limit=10)]
        detail["source"] = _load_skill_source(record)
        return jsonify(detail)

    @app.route(f"{API_PREFIX}/skills/<skill_id>/lineage", methods=["GET"])
    def skill_lineage(skill_id: str) -> Any:
        store = get_store()
        if not store.load_record(skill_id):
            abort(404, description=f"Unknown skill_id: {skill_id}")
        return jsonify(_build_lineage_payload(skill_id, store))

    @app.route(f"{API_PREFIX}/skills/<skill_id>/source", methods=["GET"])
    def skill_source(skill_id: str) -> Any:
        store = get_store()
        record = store.load_record(skill_id)
        if not record:
            abort(404, description=f"Unknown skill_id: {skill_id}")
        return jsonify(_load_skill_source(record))

    @app.route(f"{API_PREFIX}/evolution/jobs", methods=["GET"])
    def evolution_jobs() -> Any:
        status = _str_arg("status", "")
        limit = _int_arg("limit", 100)
        items = get_audit().list_jobs(status=status or None, limit=limit)
        return jsonify({"items": items, "count": len(items)})

    @app.route(f"{API_PREFIX}/evolution/jobs/<job_id>", methods=["GET"])
    def evolution_job(job_id: str) -> Any:
        payload = get_audit().get_job(job_id)
        if payload is None:
            abort(404, description=f"Unknown evolution job: {job_id}")
        return jsonify(payload)

    @app.route(f"{API_PREFIX}/evolution/packets/<packet_id>", methods=["GET"])
    def evolution_packet(packet_id: str) -> Any:
        payload = get_audit().get_packet(packet_id)
        if payload is None:
            abort(404, description=f"Unknown evidence packet: {packet_id}")
        return jsonify(payload)

    @app.route(f"{API_PREFIX}/evolution/decisions/<decision_id>", methods=["GET"])
    def evolution_decision(decision_id: str) -> Any:
        payload = get_audit().get_decision(decision_id)
        if payload is None:
            abort(404, description=f"Unknown evolution decision: {decision_id}")
        return jsonify(payload)

    @app.route(f"{API_PREFIX}/evolution/candidates", methods=["GET"])
    def evolution_candidates() -> Any:
        status = _str_arg("status", "pending")
        limit = _int_arg("limit", 100)
        items = get_audit().list_candidates(status=status, limit=limit)
        return jsonify({"items": items, "count": len(items), "status": status})

    @app.route(f"{API_PREFIX}/evolution/review-items", methods=["GET"])
    def evolution_review_items() -> Any:
        limit = _int_arg("limit", 100)
        items = get_audit().list_review_items(limit=limit)
        return jsonify({"items": items, "count": len(items)})

    @app.route(f"{API_PREFIX}/quality-signals", methods=["GET"])
    def quality_signals() -> Any:
        limit = _int_arg("limit", 100)
        subject_type = _str_arg("subject_type", "") or None
        subject_id = _str_arg("subject_id", "") or None
        actionability = _str_arg("actionability", "") or None
        not_triggerable = _bool_arg("not_triggerable", False)
        items = get_audit().list_quality_signals(
            subject_type=subject_type,
            subject_id=subject_id,
            actionability=actionability,
            not_triggerable=not_triggerable,
            limit=limit,
        )
        return jsonify({"items": items, "count": len(items)})

    @app.route(f"{API_PREFIX}/quality-signals/jobs", methods=["GET"])
    def quality_signal_jobs() -> Any:
        limit = _int_arg("limit", 100)
        items = get_audit().list_quality_signal_jobs(limit=limit)
        return jsonify({"items": items, "count": len(items)})

    @app.route(f"{API_PREFIX}/evolution/candidates/<candidate_id>", methods=["GET"])
    def evolution_candidate(candidate_id: str) -> Any:
        payload = get_audit().get_candidate(candidate_id)
        if payload is None:
            abort(404, description=f"Unknown evolution candidate: {candidate_id}")
        return jsonify(payload)

    @app.route(f"{API_PREFIX}/evolution/candidates/<candidate_id>/reject", methods=["POST"])
    def reject_evolution_candidate(candidate_id: str) -> Any:
        body = request.get_json(silent=True) or {}
        reason = str(body.get("reason") or "").strip()
        if not reason:
            reason = "manual reject"
        try:
            payload = get_audit().reject_candidate(candidate_id, reason)
        except KeyError:
            abort(404, description=f"Unknown evolution candidate: {candidate_id}")
        return jsonify(payload)

    @app.route(f"{API_PREFIX}/evolution/actions/<action_id>", methods=["GET"])
    def evolution_action(action_id: str) -> Any:
        payload = get_audit().get_action(action_id)
        if payload is None:
            abort(404, description=f"Unknown evolution action: {action_id}")
        return jsonify(payload)

    @app.route(f"{API_PREFIX}/evidence/refs/<path:ref_id>/preview", methods=["GET"])
    def evidence_ref_preview(ref_id: str) -> Any:
        max_chars = _int_arg("max_chars", 2000)
        try:
            payload = get_audit().read_ref(ref_id, max_chars=max_chars)
        except KeyError:
            abort(404, description=f"Unknown evidence ref: {ref_id}")
        except EvidenceRefAccessError as exc:
            return jsonify({"error": exc.reason, "ref_id": ref_id}), exc.status_code
        return jsonify(payload)

    @app.route(f"{API_PREFIX}/evidence/refs/<path:ref_id>", methods=["GET"])
    def evidence_ref(ref_id: str) -> Any:
        include_preview = _bool_arg("include_preview", True)
        payload = get_audit().get_ref(ref_id, include_preview=include_preview)
        if payload is None:
            abort(404, description=f"Unknown evidence ref: {ref_id}")
        return jsonify(payload)

    @app.route(f"{API_PREFIX}/workflows", methods=["GET"])
    def list_workflows() -> Any:
        items = [_build_workflow_summary(path) for path in _discover_workflow_dirs()]
        return jsonify({"items": items, "count": len(items)})

    @app.route(f"{API_PREFIX}/workflows/<workflow_id>", methods=["GET"])
    def workflow_detail(workflow_id: str) -> Any:
        workflow_dir = _get_workflow_dir(workflow_id)
        if not workflow_dir:
            abort(404, description=f"Unknown workflow: {workflow_id}")

        session = load_recording_session(str(workflow_dir))
        actions = load_agent_actions(str(workflow_dir))
        conversation = _load_conversation_records(workflow_dir)
        metadata = session.get("metadata") or {}
        trajectory = session.get("trajectory") or []
        plans = session.get("plans") or []
        decisions = session.get("decisions") or []
        action_stats = analyze_agent_actions(actions)

        enriched_trajectory = []
        for step in trajectory:
            step_copy = dict(step)
            screenshot_rel = step_copy.get("screenshot")
            if screenshot_rel:
                step_copy["screenshot_url"] = url_for(
                    "workflow_artifact",
                    workflow_id=workflow_id,
                    artifact_path=screenshot_rel,
                )
            enriched_trajectory.append(step_copy)

        timeline = _build_timeline(actions, enriched_trajectory)
        artifacts = _build_workflow_artifacts(workflow_dir, workflow_id, metadata)
        trace = _build_workflow_trace(
            workflow_id=workflow_id,
            workflow_dir=workflow_dir,
            metadata=metadata,
            conversation=conversation,
            actions=actions,
            trajectory=enriched_trajectory,
            artifacts=artifacts,
        )

        return jsonify(
            {
                **_build_workflow_summary(workflow_dir),
                "metadata": metadata,
                "statistics": session.get("statistics") or {},
                "trajectory": enriched_trajectory,
                "plans": plans,
                "decisions": decisions,
                "agent_actions": actions,
                "agent_statistics": action_stats,
                "timeline": timeline,
                "artifacts": artifacts,
                "trace": trace,
            }
        )

    @app.route(f"{API_PREFIX}/workflows/<workflow_id>/artifacts/<path:artifact_path>", methods=["GET"])
    def workflow_artifact(workflow_id: str, artifact_path: str) -> Any:
        workflow_dir = _get_workflow_dir(workflow_id)
        if not workflow_dir:
            abort(404, description=f"Unknown workflow: {workflow_id}")

        target = (workflow_dir / artifact_path).resolve()
        root = workflow_dir.resolve()
        if root not in target.parents and target != root:
            abort(404)
        if not target.exists() or not target.is_file():
            abort(404)
        return send_from_directory(str(target.parent), target.name)

    @app.route("/", defaults={"path": ""})
    @app.route("/<path:path>")
    def serve_frontend(path: str) -> Any:
        if path.startswith("api/"):
            abort(404)

        static_dir = resolve_dashboard_static_dir()
        if static_dir is not None:
            requested = static_dir / path if path else static_dir / "index.html"
            if path and requested.exists() and requested.is_file():
                return send_from_directory(str(static_dir), path)
            return send_from_directory(str(static_dir), "index.html")

        return jsonify(
            {
                "message": "OpenSpace dashboard API is running.",
                "frontend": _dashboard_static_fallback_message(),
            }
        )

    return app


def dashboard_static_dir_candidates() -> List[Path]:
    if running_from_source_checkout():
        return [FRONTEND_DIST_DIR]
    return [PACKAGED_DASHBOARD_STATIC_DIR]


def running_from_source_checkout() -> bool:
    return (PROJECT_ROOT / "pyproject.toml").is_file() and (
        PROJECT_ROOT / "apps" / "dashboard"
    ).is_dir()


def resolve_dashboard_static_dir() -> Optional[Path]:
    for candidate in dashboard_static_dir_candidates():
        if candidate.is_dir() and (candidate / "index.html").is_file():
            return candidate
    return None


def _dashboard_static_fallback_message() -> str:
    searched = ", ".join(str(path) for path in dashboard_static_dir_candidates())
    if running_from_source_checkout():
        return (
            "No dashboard frontend dist found. Build the source dashboard with "
            "`npm --prefix apps/dashboard run build`. "
            f"Searched: {searched}"
        )
    return (
        "No packaged dashboard frontend found. Reinstall OpenSpace from a "
        "package built with `npm --prefix apps/dashboard run build:packaged`. "
        f"Searched: {searched}"
    )


def _bool_arg(name: str, default: bool) -> bool:
    from flask import request

    raw = request.args.get(name)
    if raw is None:
        return default
    return raw.lower() not in {"0", "false", "no", "off"}


def _int_arg(name: str, default: int) -> int:
    from flask import request

    raw = request.args.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _str_arg(name: str, default: str) -> str:
    from flask import request

    return request.args.get(name, default)


def _resolve_evidence_db_path(
    *,
    evidence_db_path: str | Path | None,
    db_path: str | Path | None,
    evolution_storage_root: str | Path | None = None,
    skill_store: SkillStore,
) -> Path:
    if evidence_db_path is not None:
        return Path(evidence_db_path).expanduser().resolve()
    explicit = os.environ.get("OPENSPACE_EVOLUTION_EVIDENCE_DB_PATH")
    if explicit:
        return Path(explicit).expanduser().resolve()
    storage_root = evolution_storage_root or os.environ.get("OPENSPACE_EVOLUTION_STORAGE_ROOT")
    return resolve_evidence_store_db_path(
        storage_root=storage_root,
        skill_store=skill_store,
    )


def _resolve_skill_store_db_path(
    *,
    db_path: str | Path | None,
    evolution_storage_root: str | Path | None = None,
) -> Path | None:
    if db_path is not None:
        return Path(db_path).expanduser().resolve()
    explicit = os.environ.get("OPENSPACE_SKILL_STORE_DB_PATH")
    if explicit:
        return Path(explicit).expanduser().resolve()
    storage_root = evolution_storage_root or os.environ.get("OPENSPACE_EVOLUTION_STORAGE_ROOT")
    return resolve_skill_store_db_path(
        storage_root=storage_root,
        workspace_dir=PROJECT_ROOT,
    )


def _dashboard_evidence_allowed_read_roots(
    *,
    evidence_db_path: Path,
    db_path: str | Path | None,
    evolution_storage_root: str | Path | None,
) -> tuple[Path, ...]:
    roots: list[Path] = []
    _append_root(roots, evidence_db_path.parent)
    if db_path is not None:
        _append_root(roots, Path(db_path).expanduser().resolve().parent)
    storage_root = evolution_storage_root or os.environ.get("OPENSPACE_EVOLUTION_STORAGE_ROOT")
    if storage_root:
        _append_root(roots, storage_root)
        _append_root(roots, Path(storage_root).expanduser().resolve() / ".openspace" / "evolution")
    env_roots = os.environ.get("OPENSPACE_EVOLUTION_ALLOWED_READ_ROOTS", "")
    for item in env_roots.split(os.pathsep):
        _append_root(roots, item)
    return tuple(roots)


def _append_root(roots: list[Path], root: str | Path | None) -> None:
    if not root:
        return
    try:
        path = Path(root).expanduser()
        if path.is_file():
            path = path.parent
        resolved = path.resolve()
    except (OSError, TypeError, ValueError):
        return
    if resolved not in roots:
        roots.append(resolved)


def _skill_score(record: SkillRecord) -> float:
    return round(record.effective_rate * 100, 1)


def _skill_has_activity(record: SkillRecord) -> bool:
    return any(
        value > 0
        for value in (
            record.total_uses,
            record.total_applied,
            record.total_completions,
            record.total_fallbacks,
        )
    ) or bool(record.recent_analyses)


def _serialize_skill(record: SkillRecord, *, include_recent_analyses: bool = False) -> Dict[str, Any]:
    payload = record.to_dict()
    if not include_recent_analyses:
        payload.pop("recent_analyses", None)

    path = payload.get("path", "")
    lineage = payload.get("lineage") or {}
    payload.update(
        {
            "skill_dir": str(Path(path).parent) if path else "",
            "origin": lineage.get("origin", ""),
            "generation": lineage.get("generation", 0),
            "parent_skill_ids": lineage.get("parent_skill_ids", []),
            "applied_rate": round(record.applied_rate, 4),
            "completion_rate": round(record.completion_rate, 4),
            "effective_rate": round(record.effective_rate, 4),
            "fallback_rate": round(record.fallback_rate, 4),
            "score": _skill_score(record),
            "latest_evolution_action_id": lineage.get("evolution_action_id"),
            "evolution_provenance_refs": lineage.get("provenance_refs", []),
        }
    )
    return payload


def _naive_dt(dt: datetime) -> datetime:
    """Strip tzinfo so naive/aware datetimes can be compared safely."""
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def _sort_skills(records: Iterable[SkillRecord], *, sort_key: str) -> List[SkillRecord]:
    if sort_key == "updated":
        return sorted(records, key=lambda item: _naive_dt(item.last_updated), reverse=True)
    if sort_key == "name":
        return sorted(records, key=lambda item: item.name.lower())
    return sorted(
        records,
        key=lambda item: (_skill_score(item), item.total_uses, _naive_dt(item.last_updated).timestamp()),
        reverse=True,
    )


def _build_skill_stats(store: SkillStore, skills: List[SkillRecord]) -> Dict[str, Any]:
    stats = store.get_stats(active_only=False)
    avg_score = round(sum(_skill_score(item) for item in skills) / len(skills), 1) if skills else 0.0
    skills_with_recent_analysis = sum(1 for item in skills if item.recent_analyses)
    return {
        **stats,
        "average_score": avg_score,
        "skills_with_activity": sum(1 for item in skills if _skill_has_activity(item)),
        "skills_with_recent_analysis": skills_with_recent_analysis,
        "top_by_effective_rate": [_serialize_skill(item) for item in _sort_skills(skills, sort_key="score")[:5]],
    }


def _load_skill_source(record: SkillRecord) -> Dict[str, Any]:
    skill_path = Path(record.path)
    if not skill_path.exists() or not skill_path.is_file():
        return {"exists": False, "path": record.path, "content": None}
    try:
        return {
            "exists": True,
            "path": str(skill_path),
            "content": skill_path.read_text(encoding="utf-8"),
        }
    except OSError:
        return {"exists": False, "path": str(skill_path), "content": None}


def _build_lineage_payload(skill_id: str, store: SkillStore) -> Dict[str, Any]:
    records = store.load_all(active_only=False)
    if skill_id not in records:
        return {"skill_id": skill_id, "nodes": [], "edges": [], "total_nodes": 0}

    children_by_parent: Dict[str, set[str]] = {}
    for item in records.values():
        for parent_id in item.lineage.parent_skill_ids:
            children_by_parent.setdefault(parent_id, set()).add(item.skill_id)

    related_ids = {skill_id}
    frontier = [skill_id]
    while frontier:
        current = frontier.pop()
        record = records.get(current)
        if not record:
            continue
        for parent_id in record.lineage.parent_skill_ids:
            if parent_id not in related_ids:
                related_ids.add(parent_id)
                frontier.append(parent_id)
        for child_id in children_by_parent.get(current, set()):
            if child_id not in related_ids:
                related_ids.add(child_id)
                frontier.append(child_id)

    nodes = []
    edges = []
    for related_id in sorted(related_ids):
        record = records.get(related_id)
        if not record:
            continue
        nodes.append(
            {
                "skill_id": record.skill_id,
                "name": record.name,
                "description": record.description,
                "origin": record.lineage.origin.value,
                "generation": record.lineage.generation,
                "created_at": record.lineage.created_at.isoformat(),
                "visibility": record.visibility.value,
                "is_active": record.is_active,
                "enabled": record.enabled,
                "trust_state": record.trust_state.value,
                "trust_successes": record.trust_successes,
                "trust_failures": record.trust_failures,
                "tags": list(record.tags),
                "score": _skill_score(record),
                "effective_rate": round(record.effective_rate, 4),
                "total_selections": record.total_selections,
            }
        )
        for parent_id in record.lineage.parent_skill_ids:
            if parent_id in related_ids:
                edges.append({"source": parent_id, "target": record.skill_id})

    return {
        "skill_id": skill_id,
        "nodes": nodes,
        "edges": edges,
        "total_nodes": len(nodes),
    }


def _workflow_id(workflow_dir: Path) -> str:
    """Stable short ID for a workflow directory, unique across roots.

    Uses a hash suffix derived from the resolved path to avoid collisions
    when directory names contain the separator character.
    """
    import hashlib
    resolved = str(workflow_dir.resolve())
    path_hash = hashlib.sha256(resolved.encode()).hexdigest()[:8]
    return f"{workflow_dir.name}_{path_hash}"


def _workflow_source_root(workflow_dir: Path) -> Optional[Path]:
    resolved = workflow_dir.resolve()
    for root in WORKFLOW_ROOTS:
        root_resolved = root.resolve()
        if resolved == root_resolved or root_resolved in resolved.parents:
            return root
    return None


def _workflow_log_folder(workflow_dir: Path, root: Path | None) -> Path:
    if root is None:
        return workflow_dir
    try:
        relative = workflow_dir.resolve().relative_to(root.resolve())
    except ValueError:
        return workflow_dir
    if len(relative.parts) <= 1:
        return root
    return root / relative.parts[0]


def _project_relative_label(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


def _workflow_log_metadata(workflow_dir: Path) -> Dict[str, Any]:
    root = _workflow_source_root(workflow_dir)
    folder = _workflow_log_folder(workflow_dir, root)
    return {
        "log_root": str(root) if root else None,
        "log_root_label": _project_relative_label(root),
        "log_folder": str(folder),
        "log_folder_label": _project_relative_label(folder),
        "log_relative_path": _project_relative_label(workflow_dir),
    }


def _terminal_bench_task_parts(workflow_dir: Path) -> Dict[str, str | None]:
    terminal_root = PROJECT_ROOT / "benchmarks" / "terminal_bench" / "runs"
    try:
        relative = workflow_dir.resolve().relative_to(terminal_root.resolve())
    except ValueError:
        return {"run_name": None, "task_run_id": None, "task_slug": None}

    parts = relative.parts
    if len(parts) < 4:
        return {"run_name": parts[0] if parts else None, "task_run_id": None, "task_slug": None}

    task_run_id = parts[1]
    task_slug = task_run_id.split("__", 1)[0] if task_run_id else None
    return {
        "run_name": parts[0],
        "task_run_id": task_run_id,
        "task_slug": task_slug,
    }


def _metadata_instruction(metadata: Dict[str, Any]) -> str:
    return str(
        metadata.get("instruction")
        or (metadata.get("retrieved_tools") or {}).get("instruction")
        or (metadata.get("skill_selection") or {}).get("task")
        or ""
    )


def _extract_task_prompt(instruction: str) -> str:
    text = str(instruction or "").strip()
    if not text:
        return ""
    match = re.search(r"(?ims)^\s*Task:\s*(.*)$", text)
    if match:
        task = match.group(1).strip()
        if task:
            return task
    inline_match = re.search(r"(?ims)\bTask:\s*(.*)$", text)
    if inline_match:
        task = inline_match.group(1).strip()
        if task:
            return task
    return text


def _looks_like_recording_task_id(value: Any) -> bool:
    return bool(re.fullmatch(r"task_[0-9a-fA-F]{8,}(?:_\d{8}_\d{6})?", str(value or "")))


def _workflow_task_identity(workflow_dir: Path, metadata: Dict[str, Any]) -> Dict[str, Any]:
    recording_task_id = str(metadata.get("task_id") or metadata.get("task_name") or workflow_dir.name)
    raw_task_name = str(metadata.get("task_name") or "")
    full_instruction = _metadata_instruction(metadata)
    task_prompt = _extract_task_prompt(full_instruction)
    bench = _terminal_bench_task_parts(workflow_dir)
    benchmark_task_id = bench.get("task_slug")
    benchmark_task_run_id = bench.get("task_run_id")

    if benchmark_task_id:
        task_name = benchmark_task_id
    elif raw_task_name and not _looks_like_recording_task_id(raw_task_name):
        task_name = raw_task_name
    elif task_prompt:
        task_name = _compact_text(task_prompt, 96)
    else:
        task_name = recording_task_id

    return {
        "task_id": recording_task_id,
        "task_name": task_name,
        "instruction": task_prompt or full_instruction,
        "full_instruction": full_instruction,
        "recording_task_id": recording_task_id,
        "benchmark_task_id": benchmark_task_id,
        "benchmark_task_run_id": benchmark_task_run_id,
        "benchmark_run_name": bench.get("run_name"),
        "instruction_source": "metadata.instruction.task_block" if task_prompt != full_instruction else "metadata.instruction",
    }


def _discover_workflow_dirs() -> List[Path]:
    discovered: Dict[str, Path] = {}
    for root in WORKFLOW_ROOTS:
        if not root.exists():
            continue
        _scan_workflow_tree(root, discovered)
    return sorted(discovered.values(), key=lambda item: item.stat().st_mtime, reverse=True)


def _scan_workflow_tree(directory: Path, discovered: Dict[str, Path], *, _depth: int = 0, _max_depth: int = 6) -> None:
    if _depth > _max_depth:
        return
    try:
        children = list(directory.iterdir())
    except OSError:
        return
    for child in children:
        if not child.is_dir():
            continue
        if (child / "metadata.json").exists() or (child / "traj.jsonl").exists():
            discovered.setdefault(str(child.resolve()), child)
        else:
            _scan_workflow_tree(child, discovered, _depth=_depth + 1, _max_depth=_max_depth)


def _get_workflow_dir(workflow_id: str) -> Optional[Path]:
    for path in _discover_workflow_dirs():
        if _workflow_id(path) == workflow_id:
            return path
    return None


def _build_workflow_summary(workflow_dir: Path) -> Dict[str, Any]:
    session = load_recording_session(str(workflow_dir))
    metadata = session.get("metadata") or {}
    statistics = session.get("statistics") or {}
    actions = load_agent_actions(str(workflow_dir))
    screenshots_dir = workflow_dir / "screenshots"
    screenshot_count = len(list(screenshots_dir.glob("*.png"))) if screenshots_dir.exists() else 0

    video_candidates = [workflow_dir / "screen_recording.mp4", workflow_dir / "recording.mp4"]
    video_url = None
    for candidate in video_candidates:
        if candidate.exists():
            rel = candidate.relative_to(workflow_dir).as_posix()
            video_url = url_for("workflow_artifact", workflow_id=_workflow_id(workflow_dir), artifact_path=rel)
            break

    outcome = metadata.get("execution_outcome") or {}
    task_identity = _workflow_task_identity(workflow_dir, metadata)
    instruction = task_identity["instruction"]

    # Resolve start/end times with trajectory fallback
    start_time = metadata.get("start_time")
    end_time = metadata.get("end_time")
    trajectory = session.get("trajectory") or []

    # If end_time is missing, infer from last trajectory step
    if not end_time and trajectory:
        last_ts = trajectory[-1].get("timestamp")
        if last_ts:
            end_time = last_ts

    # Compute execution_time: prefer outcome, fallback to timestamp diff
    execution_time = outcome.get("execution_time", 0)
    if not execution_time and start_time and end_time:
        try:
            t0 = datetime.fromisoformat(start_time)
            t1 = datetime.fromisoformat(end_time)
            execution_time = round((t1 - t0).total_seconds(), 2)
        except (ValueError, TypeError):
            pass

    # Resolve status: prefer outcome, fallback heuristic
    status = outcome.get("status", "")
    if not status:
        total_steps = int(statistics.get("total_steps") or 0)
        success_count = int(statistics.get("success_count") or 0)
        if total_steps > 0 and success_count >= total_steps:
            status = "success"
        elif total_steps > 0 and success_count > 0:
            status = "partial"
        elif total_steps > 0:
            status = "error"
        elif trajectory:
            status = "completed"
        else:
            status = "unknown"

    # Resolve iterations: prefer outcome, fallback to conversation count
    iterations = outcome.get("iterations", 0)
    if not iterations and trajectory:
        iterations = len(trajectory)

    return {
        "id": _workflow_id(workflow_dir),
        "path": str(workflow_dir),
        **_workflow_log_metadata(workflow_dir),
        "task_id": task_identity["task_id"],
        "task_name": task_identity["task_name"],
        "recording_task_id": task_identity["recording_task_id"],
        "benchmark_task_id": task_identity["benchmark_task_id"],
        "benchmark_task_run_id": task_identity["benchmark_task_run_id"],
        "benchmark_run_name": task_identity["benchmark_run_name"],
        "instruction_source": task_identity["instruction_source"],
        "instruction": instruction,
        "status": status,
        "iterations": iterations,
        "execution_time": execution_time,
        "start_time": start_time,
        "end_time": end_time,
        "total_steps": statistics.get("total_steps", 0),
        "success_count": statistics.get("success_count", 0),
        "success_rate": statistics.get("success_rate", 0.0),
        "backend_counts": statistics.get("backends", {}),
        "tool_counts": statistics.get("tools", {}),
        "agent_action_count": len(actions),
        "has_video": bool(video_url),
        "video_url": video_url,
        "screenshot_count": screenshot_count,
        "selected_skills": (metadata.get("skill_selection") or {}).get("selected", []),
    }


def _build_timeline(actions: List[Dict[str, Any]], trajectory: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for action in actions:
        events.append(
            {
                "timestamp": action.get("timestamp", ""),
                "type": "agent_action",
                "step": action.get("step"),
                "label": action.get("action_type", "agent_action"),
                "agent_name": action.get("agent_name", ""),
                "agent_type": action.get("agent_type", ""),
                "details": action,
            }
        )
    for step in trajectory:
        events.append(
            {
                "timestamp": step.get("timestamp", ""),
                "type": "tool_execution",
                "step": step.get("step"),
                "label": step.get("tool", "tool_execution"),
                "backend": step.get("backend", ""),
                "status": (step.get("result") or {}).get("status", "unknown"),
                "details": step,
            }
        )
    events.sort(key=lambda item: (item.get("timestamp", ""), item.get("step") or 0))
    return events


def _load_conversation_records(workflow_dir: Path) -> List[Dict[str, Any]]:
    path = workflow_dir / "conversations.jsonl"
    if not path.exists():
        return []
    records: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    value = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    value["_line_no"] = line_no
                    records.append(value)
    except OSError:
        return []
    return records


def _build_workflow_trace(
    *,
    workflow_id: str,
    workflow_dir: Path,
    metadata: Dict[str, Any],
    conversation: List[Dict[str, Any]],
    actions: List[Dict[str, Any]],
    trajectory: List[Dict[str, Any]],
    artifacts: Dict[str, Any],
) -> Dict[str, Any]:
    events: List[Dict[str, Any]] = []
    emitted_tool_steps: set[int] = set()

    def add_event(
        *,
        harness: str,
        source: str,
        title: str,
        summary: str = "",
        timestamp: str | None = None,
        iteration: int | None = None,
        based_on: List[str] | None = None,
        decision: str = "",
        impact: str = "",
        status: str | None = None,
        agent_name: str | None = None,
        tool_name: str | None = None,
        backend: str | None = None,
        inputs: List[Dict[str, Any]] | None = None,
        outputs: List[Dict[str, Any]] | None = None,
        metadata_payload: Dict[str, Any] | None = None,
        raw: Dict[str, Any] | None = None,
    ) -> None:
        sequence = len(events) + 1
        clean_inputs = _dedupe_trace_items(inputs or [])
        clean_outputs = _dedupe_trace_items(outputs or [], existing=clean_inputs)
        events.append(
            {
                "event_id": f"trace-{sequence:04d}",
                "sequence": sequence,
                "timestamp": timestamp or "",
                "iteration": iteration,
                "harness": harness,
                "source": source,
                "title": title,
                "summary": _compact_text(summary, 520),
                "based_on": [
                    _compact_text(item, 220)
                    for item in (based_on or [])
                    if _compact_text(item, 220)
                ],
                "decision": _compact_text(decision, 520),
                "impact": _compact_text(impact, 520),
                "status": status,
                "agent_name": agent_name,
                "tool_name": tool_name,
                "backend": backend,
                "inputs": clean_inputs,
                "outputs": clean_outputs,
                "metadata": metadata_payload or {},
                "raw": raw or {},
            }
        )

    start_time = str(metadata.get("start_time") or "")
    task_identity = _workflow_task_identity(workflow_dir, metadata)
    instruction = str(task_identity["instruction"] or "")
    full_instruction = str(task_identity["full_instruction"] or "")
    if instruction:
        add_event(
            harness="input",
            source="metadata.json",
            title=f"Task: {task_identity['task_name']}",
            summary=str(instruction),
            timestamp=start_time,
            based_on=[
                "recording_task_id: " + str(task_identity["recording_task_id"]),
                "benchmark_task_id: " + str(task_identity.get("benchmark_task_id") or "unknown"),
            ],
            decision="Accepted the task as the execution objective.",
            impact="Every later tool selection, skill selection, model prompt, and stop decision is scoped to this instruction.",
            inputs=[
                _trace_item("Task", task_identity["task_name"]),
                _trace_item("Recording id", task_identity["recording_task_id"]),
                _trace_item("Task prompt", instruction),
            ],
            outputs=[],
            metadata_payload={
                "recording_task_id": task_identity["recording_task_id"],
                "benchmark_task_id": task_identity.get("benchmark_task_id"),
                "benchmark_task_run_id": task_identity.get("benchmark_task_run_id"),
                "instruction_source": task_identity["instruction_source"],
            },
            raw={"instruction": full_instruction, "task_prompt": instruction, "task_id": metadata.get("task_id")},
        )

    backends = metadata.get("backends")
    if isinstance(backends, list) and backends:
        add_event(
            harness="capability",
            source="metadata.json",
            title="Enabled harness backends",
            summary=", ".join(str(item) for item in backends),
            timestamp=start_time,
            decision=f"Enabled {len(backends)} backend channel(s).",
            impact="Only tools from these backend harnesses can be recorded as executable trajectory steps.",
            inputs=[_trace_item("Recording config", {"backends": backends})],
            outputs=[_trace_item("Enabled backends", backends)],
            raw={"backends": backends},
        )

    retrieved_tools = metadata.get("retrieved_tools")
    if isinstance(retrieved_tools, dict):
        tools = retrieved_tools.get("tools") if isinstance(retrieved_tools.get("tools"), list) else []
        tool_names = [_tool_record_name(item) for item in tools if _tool_record_name(item)]
        preselection = retrieved_tools.get("preselection_debug")
        retrieved_instruction = str(retrieved_tools.get("instruction") or "")
        based_on = [f"task: {task_identity['task_name']}"]
        if isinstance(preselection, dict):
            for key in ("search_mode", "total_candidates", "deferred_count", "non_deferred_count"):
                if key in preselection:
                    based_on.append(f"{key}: {preselection[key]}")
        add_event(
            harness="capability",
            source="metadata.retrieved_tools",
            title="Retrieved tool inventory",
            summary=", ".join(tool_names[:24]) if tool_names else "No retrieved tools recorded.",
            timestamp=start_time,
            based_on=based_on,
            decision=f"Retrieved {len(tools)} tool definition(s) for the model context.",
            impact="This establishes the concrete tool-use surface before model decisions are made.",
            inputs=[
                _trace_item("Preselection debug", preselection or {}),
            ],
            outputs=[_trace_item("Retrieved tool names", tool_names)],
            metadata_payload={
                "tool_count": len(tools),
                "retrieval_instruction_matches_task": (
                    _same_trace_text(retrieved_instruction, full_instruction)
                    or _same_trace_text(
                        retrieved_instruction,
                        str(task_identity.get("instruction") or ""),
                    )
                ),
            },
            raw=retrieved_tools,
        )

    skill_selection = metadata.get("skill_selection")
    if isinstance(skill_selection, dict):
        selected = _strings(skill_selection.get("selected"))
        available = _strings(skill_selection.get("available_skills"))
        method = str(skill_selection.get("method") or "unknown")
        add_event(
            harness="skill",
            source="metadata.skill_selection",
            title="Skill selection",
            summary=", ".join(selected) if selected else "No skills selected.",
            timestamp=start_time,
            based_on=[
                f"method: {method}",
                f"available skills: {len(available)}",
                _compact_text(skill_selection.get("prompt") or "", 220),
            ],
            decision=f"Selected {len(selected)} skill(s)." if selected else "Continued without selected skills.",
            impact="Selected skills can inject task-specific guidance, while unselected skills stay out of the prompt.",
            inputs=[
                _trace_item("Available skills", available),
                _trace_item("Selection prompt", skill_selection.get("prompt") or ""),
                _trace_item("Selection method", method),
            ],
            outputs=[_trace_item("Selected skills", selected)],
            metadata_payload={"method": method, "selected_count": len(selected), "available_count": len(available)},
            raw=skill_selection,
        )

    trajectory_by_call_id: Dict[str, List[Dict[str, Any]]] = {}
    for step in trajectory:
        evidence = ((step.get("result") or {}).get("evidence") or {})
        call_id = evidence.get("tool_call_id") if isinstance(evidence, dict) else None
        if call_id:
            trajectory_by_call_id.setdefault(str(call_id), []).append(step)

    for record in conversation:
        record_type = str(record.get("type") or "record")
        timestamp = str(record.get("timestamp") or start_time)
        agent_name = str(record.get("agent_name") or "")
        if record_type == "setup":
            messages = record.get("messages") if isinstance(record.get("messages"), list) else []
            tools = record.get("tools") if isinstance(record.get("tools"), list) else []
            prompt_roles = [
                str(message.get("role") or "message") if isinstance(message, dict) else "message"
                for message in messages
            ]
            add_event(
                harness="input",
                source="conversations.jsonl",
                title=f"{agent_name or 'Agent'} prompt setup",
                summary=f"{len(messages)} prompt message(s), {len(tools)} tool schema(s).",
                timestamp=timestamp,
                agent_name=agent_name or None,
                based_on=["system prompts", "user instruction", "available tool schemas"],
                decision="Constructed the initial model-call context.",
                impact="This is the base context the first model decision is conditioned on.",
                inputs=[
                    _trace_item("Prompt message roles", prompt_roles),
                    _trace_item("Tool schema count", len(tools)),
                ],
                outputs=[_trace_item("Prompt context", {"messages": len(messages), "tools": len(tools)})],
                metadata_payload={"line": record.get("_line_no"), "message_count": len(messages), "tool_count": len(tools)},
                raw=record,
            )
            for index, message in enumerate(messages, start=1):
                harness = _message_harness(message)
                role = str(message.get("role") or "message") if isinstance(message, dict) else "message"
                message_text = _message_text(message)
                if role == "user" and _trace_text_overlaps(message_text, full_instruction):
                    continue
                add_event(
                    harness=harness,
                    source="conversation.setup.messages",
                    title=f"Setup {role} message",
                    summary=message_text,
                    timestamp=timestamp,
                    agent_name=agent_name or None,
                    based_on=[f"setup message #{index}", _meta_label(message)],
                    decision="Included this context in the model prompt.",
                    impact="Influences the first model response and any later reconstructed conversation context.",
                    inputs=[
                        _trace_item("Role", role),
                        _trace_item("Message metadata", (message.get("_meta") if isinstance(message, dict) else {}) or {}),
                    ],
                    outputs=[_trace_item("Prompt contribution", message_text)],
                    metadata_payload={"line": record.get("_line_no"), "message_index": index, "role": role},
                    raw=message if isinstance(message, dict) else {"value": message},
                )
            if tools:
                add_event(
                    harness="capability",
                    source="conversation.setup.tools",
                    title="Tool schema exposed to model",
                    summary=", ".join(_tool_schema_label(item) for item in tools[:32]),
                    timestamp=timestamp,
                    agent_name=agent_name or None,
                    based_on=[f"{len(tools)} recorded schema(s)"],
                    decision="Exposed callable tool schemas to the model.",
                    impact="Tool-call decisions can only reference tools present in this schema set.",
                    inputs=[_trace_item("Schema count", len(tools))],
                    outputs=[_trace_item("Callable tool names", [_tool_schema_label(item) for item in tools])],
                    metadata_payload={"line": record.get("_line_no"), "tool_count": len(tools)},
                    raw={"tools": tools},
                )
            continue

        if record_type != "iteration":
            add_event(
                harness="state",
                source="conversations.jsonl",
                title=f"Conversation {record_type}",
                summary=_compact_json(record, 420),
                timestamp=timestamp,
                agent_name=agent_name or None,
                decision="Recorded conversation-side state.",
                impact="This state may affect replay and trace reconstruction.",
                inputs=[_trace_item("Conversation record", record)],
                outputs=[_trace_item("State record", {"type": record_type, "line": record.get("_line_no")})],
                raw=record,
            )
            continue

        iteration = _safe_int(record.get("iteration"))
        response_metadata = record.get("response_metadata") if isinstance(record.get("response_metadata"), dict) else {}
        delta_messages = record.get("delta_messages") if isinstance(record.get("delta_messages"), list) else []
        add_event(
            harness="state",
            source="conversation.iteration",
            title=f"Iteration {iteration or '?'} boundary",
            summary=_compact_json(response_metadata, 420),
            timestamp=timestamp,
            iteration=iteration,
            agent_name=agent_name or None,
            based_on=["conversation context before this iteration"],
            decision=_iteration_decision(response_metadata),
            impact="Groups the model response, tool calls, tool results, and any injected follow-up context for this turn.",
            inputs=[
                _trace_item("Response metadata", response_metadata),
                _trace_item("Delta message count", len(delta_messages)),
            ],
            outputs=[_trace_item("Iteration group", {"iteration": iteration, "delta_messages": len(delta_messages)})],
            metadata_payload={"line": record.get("_line_no"), **response_metadata},
            raw=record,
        )
        for message_index, message in enumerate(delta_messages, start=1):
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "")
            if role == "assistant":
                tool_calls = message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []
                content = _message_text(message)
                call_names = [_tool_call_name(item) for item in tool_calls]
                add_event(
                    harness="model",
                    source="conversation.delta.assistant",
                    title=f"Model response {iteration or '?'}",
                    summary=content or f"{len(tool_calls)} tool call(s), no assistant text.",
                    timestamp=timestamp,
                    iteration=iteration,
                    agent_name=agent_name or None,
                    based_on=[
                        "setup context and previous deltas",
                        f"response metadata: {_compact_json(response_metadata, 180)}",
                    ],
                    decision=(
                        "Requested tool(s): " + ", ".join(call_names)
                        if call_names
                        else "Produced assistant output without tool calls."
                    ),
                    impact=(
                        "Triggers tool execution before the next model iteration."
                        if call_names
                        else "May complete the task or trigger stop/budget hooks."
                    ),
                    inputs=[
                        _trace_item("Context basis", ["setup context", "previous deltas"]),
                        _trace_item("Response metadata", response_metadata),
                    ],
                    outputs=[
                        _trace_item("Assistant content", content),
                        _trace_item("Tool calls", tool_calls),
                    ],
                    metadata_payload={"message_index": message_index, "tool_calls_count": len(tool_calls)},
                    raw=message,
                )
                for tool_index, tool_call in enumerate(tool_calls, start=1):
                    name = _tool_call_name(tool_call)
                    call_id = _tool_call_id(tool_call)
                    arguments = _tool_call_arguments(tool_call)
                    add_event(
                        harness="tool_call",
                        source="assistant.tool_calls",
                        title=f"Tool call: {name}",
                        summary=_compact_json(arguments, 420),
                        timestamp=timestamp,
                        iteration=iteration,
                        agent_name=agent_name or None,
                        tool_name=name,
                        based_on=[content or "tool-call-only model response", f"tool_call_id: {call_id}" if call_id else ""],
                        decision=f"Call {name} with the recorded arguments.",
                        impact="Transfers control from model reasoning to the selected tool harness.",
                        inputs=[_trace_item("Model response text", content)] if content else [],
                        outputs=[
                            _trace_item("Tool", {"name": name, "id": call_id}),
                            _trace_item("Arguments", arguments),
                        ],
                        metadata_payload={"tool_call_id": call_id, "tool_index": tool_index},
                        raw=tool_call if isinstance(tool_call, dict) else {"value": tool_call},
                    )
                    if call_id:
                        for step in trajectory_by_call_id.get(str(call_id), []):
                            step_num = _safe_int(step.get("step"))
                            if step_num is not None:
                                emitted_tool_steps.add(step_num)
                            _add_tool_execution_event(
                                add_event,
                                step,
                                timestamp=str(step.get("timestamp") or timestamp),
                                iteration=iteration,
                                based_on_extra=[f"tool_call_id: {call_id}"],
                            )
                continue

            if role == "tool" or message.get("tool_call_id") or _tool_result_metadata(message):
                meta = _tool_result_metadata(message)
                tool_name = str(message.get("name") or meta.get("tool") or meta.get("tool_name") or "tool")
                status = str(meta.get("status") or "")
                add_event(
                    harness="tool_result",
                    source="conversation.delta.tool_result",
                    title=f"Tool result: {tool_name}",
                    summary=_message_text(message),
                    timestamp=timestamp,
                    iteration=iteration,
                    agent_name=agent_name or None,
                    tool_name=tool_name,
                    status=status or None,
                    based_on=[
                        f"tool_call_id: {message.get('tool_call_id') or meta.get('tool_call_id') or meta.get('tool_use_id')}",
                        _compact_json(meta, 220),
                    ],
                    decision="Returned an observation to the conversation.",
                    impact="This result becomes input for the next model decision and can change the plan.",
                    inputs=[
                        _trace_item("Tool identity", {"name": tool_name, "tool_call_id": message.get("tool_call_id") or meta.get("tool_call_id") or meta.get("tool_use_id")}),
                        _trace_item("Tool result metadata", meta),
                    ],
                    outputs=[_trace_item("Tool result content", _message_text(message))],
                    metadata_payload=meta,
                    raw=message,
                )
                continue

            harness = _message_harness(message)
            add_event(
                harness=harness,
                source="conversation.delta.message",
                title=f"{_human_title(harness)} context update",
                summary=_message_text(message),
                timestamp=timestamp,
                iteration=iteration,
                agent_name=agent_name or None,
                based_on=[_meta_label(message), f"role: {role or 'unknown'}"],
                decision="Injected a non-tool message into the conversation.",
                impact="This message is visible to later model calls and can redirect the next decision.",
                inputs=[_trace_item("Injected message", message)],
                outputs=[_trace_item("Conversation update", _message_text(message))],
                metadata_payload={"message_index": message_index, "role": role, "meta": message.get("_meta") or {}},
                raw=message,
            )

    for action in actions:
        if not isinstance(action, dict):
            continue
        related_steps = action.get("related_tool_steps")
        action_input = _action_input_for_trace(action.get("input"), instruction=full_instruction)
        add_event(
            harness="agent_action",
            source="agent_actions.jsonl",
            title=f"Agent action: {action.get('action_type') or 'action'}",
            summary=_compact_json(action.get("output") or action.get("reasoning") or action.get("input") or {}, 520),
            timestamp=str(action.get("timestamp") or start_time),
            agent_name=str(action.get("agent_name") or "") or None,
            based_on=_basis_from_mapping(action.get("input")),
            decision=_compact_json(action.get("output") or action.get("reasoning") or {}, 520),
            impact=(
                f"Linked tool steps: {', '.join(str(item) for item in related_steps)}"
                if isinstance(related_steps, list) and related_steps
                else "Records the high-level agent decision for later audit."
            ),
            inputs=[_trace_item("Action input", action_input)],
            outputs=[
                _trace_item("Reasoning", action.get("reasoning") or {}),
                _trace_item("Action output", action.get("output") or {}),
            ],
            metadata_payload={
                "action_type": action.get("action_type"),
                "correlation_id": action.get("correlation_id"),
                "related_tool_steps": related_steps or [],
            },
            raw=action,
        )

    for step in trajectory:
        step_num = _safe_int(step.get("step"))
        if step_num is not None and step_num in emitted_tool_steps:
            continue
        _add_tool_execution_event(
            add_event,
            step,
            timestamp=str(step.get("timestamp") or start_time),
            iteration=None,
            based_on_extra=[],
        )

    screenshot_count = len(artifacts.get("screenshots") or []) if isinstance(artifacts, dict) else 0
    if screenshot_count or (isinstance(artifacts, dict) and artifacts.get("video_url")):
        add_event(
            harness="artifact",
            source="workflow artifacts",
            title="Captured visual artifacts",
            summary=f"{screenshot_count} screenshot(s)" + (" and video" if artifacts.get("video_url") else ""),
            timestamp=str(metadata.get("end_time") or start_time),
            decision="Archived visual evidence for the run.",
            impact="Screenshots and video help verify UI or GUI-side effects outside text logs.",
            inputs=[_trace_item("Artifact manifest", artifacts)],
            outputs=[
                _trace_item("Screenshots", artifacts.get("screenshots") or []),
                _trace_item("Video URL", artifacts.get("video_url")),
            ],
            metadata_payload={"screenshot_count": screenshot_count, "has_video": bool(artifacts.get("video_url"))},
            raw=artifacts if isinstance(artifacts, dict) else {},
        )

    outcome = metadata.get("execution_outcome")
    if isinstance(outcome, dict):
        add_event(
            harness="outcome",
            source="metadata.execution_outcome",
            title="Execution outcome",
            summary=_compact_json(outcome, 420),
            timestamp=str(metadata.get("end_time") or start_time),
            status=str(outcome.get("status") or "") or None,
            based_on=[
                f"iterations: {outcome.get('iterations')}",
                f"execution_time: {outcome.get('execution_time')}",
            ],
            decision=f"Finished with status {outcome.get('status') or 'unknown'}.",
            impact="Closes the trace and provides the task-level result used by workflow summaries.",
            inputs=[
                _trace_item("Iterations", outcome.get("iterations")),
                _trace_item("Execution time", outcome.get("execution_time")),
            ],
            outputs=[_trace_item("Final status", outcome.get("status") or "unknown")],
            raw=outcome,
        )

    harness_counts = Counter(str(event.get("harness") or "unknown") for event in events)
    tools = sorted(
        {
            str(event.get("tool_name"))
            for event in events
            if event.get("tool_name")
        }
    )
    agents = sorted(
        {
            str(event.get("agent_name"))
            for event in events
            if event.get("agent_name")
        }
    )
    iterations = sorted(
        {
            int(event["iteration"])
            for event in events
            if isinstance(event.get("iteration"), int)
        }
    )
    return {
        "summary": {
            "total_events": len(events),
            "harness_counts": dict(harness_counts),
            "agents": agents,
            "tools": tools,
            "iterations": iterations,
            "has_conversation_log": bool(conversation),
            "has_agent_actions": bool(actions),
            "has_tool_trajectory": bool(trajectory),
            "source_files": {
                "metadata": str(workflow_dir / "metadata.json"),
                "conversation": str(workflow_dir / "conversations.jsonl"),
                "agent_actions": str(workflow_dir / "agent_actions.jsonl"),
                "trajectory": str(workflow_dir / "traj.jsonl"),
            },
            "workflow_id": workflow_id,
        },
        "events": events,
    }


def _add_tool_execution_event(
    add_event: Any,
    step: Dict[str, Any],
    *,
    timestamp: str,
    iteration: int | None,
    based_on_extra: List[str],
) -> None:
    result = step.get("result") if isinstance(step.get("result"), dict) else {}
    status = str(result.get("status") or "unknown")
    command = str(step.get("command") or step.get("tool") or "tool")
    tool_name = str(step.get("tool") or "tool")
    evidence = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
    output = (
        result.get("stderr")
        or result.get("output")
        or result.get("stdout")
        or result.get("content")
        or ""
    )
    based_on = [
        f"backend: {step.get('backend') or 'unknown'}",
        f"command: {_compact_text(command, 220)}",
        _compact_json(step.get("parameters") or {}, 220),
    ] + based_on_extra
    if evidence:
        based_on.append(_compact_json(evidence, 220))
    add_event(
        harness="tool_execution",
        source="traj.jsonl",
        title=f"Executed {tool_name}",
        summary=_compact_text(str(output) or command, 520),
        timestamp=timestamp,
        iteration=iteration,
        status=status,
        tool_name=tool_name,
        backend=str(step.get("backend") or "") or None,
        agent_name=str(step.get("agent_name") or "") or None,
        based_on=based_on,
        decision=f"Ran {tool_name} through the {step.get('backend') or 'unknown'} harness.",
        impact=(
            f"Result status: {status}. "
            + ("A screenshot was captured." if step.get("screenshot_url") else "The result was recorded for later model context or audit.")
        ),
        inputs=[
            _trace_item("Command", command),
            _trace_item("Parameters", step.get("parameters") or {}),
            _trace_item("Backend", step.get("backend") or "unknown"),
        ],
        outputs=[
            _trace_item("Status", status),
            _trace_item("Stdout", result.get("stdout") or ""),
            _trace_item("Stderr", result.get("stderr") or ""),
            _trace_item("Output", result.get("output") or result.get("content") or ""),
            _trace_item("Evidence", evidence),
        ],
        metadata_payload={
            "step": step.get("step"),
            "server": step.get("server"),
            "screenshot_url": step.get("screenshot_url"),
            "evidence": evidence,
        },
        raw=step,
    )


def _trace_item(label: str, value: Any, kind: str | None = None) -> Dict[str, Any]:
    inferred_kind = kind
    if inferred_kind is None:
        if isinstance(value, (dict, list, tuple)):
            inferred_kind = "json"
        elif isinstance(value, (int, float, bool)) or value is None:
            inferred_kind = "scalar"
        else:
            inferred_kind = "text"
    return {
        "label": label,
        "kind": inferred_kind,
        "preview": _compact_text(value, 520),
        "value": _trace_value(value),
    }


def _same_trace_text(left: Any, right: Any) -> bool:
    return _trace_value_key(left) == _trace_value_key(right)


def _trace_text_overlaps(left: Any, right: Any, *, min_chars: int = 160) -> bool:
    left_text = _trace_value_key(left)
    right_text = _trace_value_key(right)
    if not left_text or not right_text:
        return False
    if left_text == right_text:
        return True
    shortest = min(len(left_text), len(right_text))
    if shortest < min_chars:
        return False
    return left_text.startswith(right_text[:min_chars]) or right_text.startswith(left_text[:min_chars])


def _trace_value_key(value: Any) -> str:
    if value is None:
        return ""
    try:
        normalized = _trace_value(value, max_string=20000)
        if isinstance(normalized, str):
            text = normalized
        else:
            text = json.dumps(normalized, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        text = str(value)
    return " ".join(text.split()).strip().lower()


def _dedupe_trace_items(
    items: List[Dict[str, Any]],
    *,
    existing: List[Dict[str, Any]] | None = None,
) -> List[Dict[str, Any]]:
    seen = {
        _trace_value_key(item.get("value"))
        for item in (existing or [])
        if _trace_value_key(item.get("value"))
    }
    result: List[Dict[str, Any]] = []
    for item in items:
        if _trace_item_is_empty(item):
            continue
        key = _trace_value_key(item.get("value"))
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        result.append(item)
    return result


def _trace_item_is_empty(item: Dict[str, Any]) -> bool:
    value = item.get("value")
    return value is None or value == "" or value == [] or value == {}


def _action_input_for_trace(value: Any, *, instruction: str) -> Any:
    if not isinstance(value, dict):
        return value or {}
    cleaned: Dict[str, Any] = {}
    for key, item in value.items():
        if str(key) == "instruction" and _trace_text_overlaps(item, instruction):
            continue
        cleaned[str(key)] = item
    if cleaned:
        return cleaned
    return {"instruction": "same as task prompt"}


def _trace_value(value: Any, *, max_string: int = 20000, depth: int = 0) -> Any:
    if isinstance(value, str):
        if len(value) > max_string:
            return value[:max_string].rstrip() + f"... [truncated, total {len(value)} chars]"
        return value
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if depth >= 3:
        return _compact_json(value, max_string)
    if isinstance(value, dict):
        result: Dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 80:
                result["..."] = f"{len(value) - index} more keys"
                break
            result[str(key)] = _trace_value(item, max_string=max_string, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        items = list(value)
        truncated = [
            _trace_value(item, max_string=max_string, depth=depth + 1)
            for item in items[:120]
        ]
        if len(items) > 120:
            truncated.append(f"... {len(items) - 120} more items")
        return truncated
    return _compact_text(value, max_string)


def _compact_text(value: Any, max_chars: int = 280) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = _compact_json(value, max_chars=max_chars)
    text = " ".join(str(value).split())
    if len(text) > max_chars:
        return text[: max_chars - 1].rstrip() + "..."
    return text


def _compact_json(value: Any, max_chars: int = 280) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        text = str(value)
    return _compact_text(text, max_chars=max_chars)


def _strings(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item or "").strip()]


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _message_text(message: Dict[str, Any]) -> str:
    content = message.get("content") if isinstance(message, dict) else message
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [_content_block_text(item) for item in content]
        return "\n".join(part for part in parts if part)
    return _compact_json(content, 500)


def _content_block_text(item: Any) -> str:
    if isinstance(item, str):
        return item
    if not isinstance(item, dict):
        return _compact_json(item, 260)
    block_type = str(item.get("type") or "")
    if block_type == "text":
        return str(item.get("text") or "")
    if block_type == "tool_result":
        content = item.get("content")
        if isinstance(content, list):
            return "\n".join(_content_block_text(child) for child in content)
        return str(content or "")
    if block_type in {"image", "image_url", "document"}:
        source = item.get("source") or item.get("image_url") or {}
        if isinstance(source, dict) and source.get("path"):
            return f"[{block_type}: {source.get('path')}]"
        return f"[{block_type}]"
    return _compact_json(item, 260)


def _message_harness(message: Any) -> str:
    if not isinstance(message, dict):
        return "state"
    role = str(message.get("role") or "").lower()
    meta = message.get("_meta") if isinstance(message.get("_meta"), dict) else {}
    meta_type = str(meta.get("type") or "").lower()
    attachment = meta.get("attachment") if isinstance(meta.get("attachment"), dict) else {}
    attachment_type = str(attachment.get("type") or "").lower()
    haystack = " ".join([role, meta_type, attachment_type, str(message.get("name") or "").lower()])
    if "memory" in haystack:
        return "memory"
    if "skill" in haystack:
        return "skill"
    if role == "system":
        return "system"
    if role == "assistant":
        return "model"
    if role == "tool":
        return "tool_result"
    if role == "user":
        return "input"
    return "state"


def _meta_label(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    meta = message.get("_meta") if isinstance(message.get("_meta"), dict) else {}
    labels = []
    if meta.get("type"):
        labels.append(f"meta: {meta.get('type')}")
    attachment = meta.get("attachment") if isinstance(meta.get("attachment"), dict) else {}
    if attachment.get("type"):
        labels.append(f"attachment: {attachment.get('type')}")
    return ", ".join(labels)


def _tool_schema_label(item: Any) -> str:
    if not isinstance(item, dict):
        return str(item)
    name = str(item.get("name") or "tool")
    backend = str(item.get("backend") or "")
    return f"{name}@{backend}" if backend else name


def _tool_record_name(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("name") or item.get("tool") or "").strip()
    return str(item or "").strip()


def _tool_call_name(tool_call: Any) -> str:
    if not isinstance(tool_call, dict):
        return "tool"
    function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
    return str(function.get("name") or tool_call.get("name") or "tool")


def _tool_call_id(tool_call: Any) -> str:
    if not isinstance(tool_call, dict):
        return ""
    return str(tool_call.get("id") or tool_call.get("tool_call_id") or tool_call.get("tool_use_id") or "")


def _tool_call_arguments(tool_call: Any) -> Any:
    if not isinstance(tool_call, dict):
        return {}
    function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
    raw = function.get("arguments", tool_call.get("arguments", tool_call.get("input", {})))
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}
    return raw or {}


def _tool_result_metadata(message: Dict[str, Any]) -> Dict[str, Any]:
    meta = message.get("_meta") if isinstance(message.get("_meta"), dict) else {}
    result_meta = meta.get("tool_result_metadata")
    return dict(result_meta) if isinstance(result_meta, dict) else {}


def _iteration_decision(metadata: Dict[str, Any]) -> str:
    count = metadata.get("tool_calls_count")
    if count:
        return f"The model chose to request {count} tool call(s)."
    if metadata.get("has_tool_calls") is False:
        return "The model did not request tool execution in this iteration."
    return "Recorded iteration metadata."


def _basis_from_mapping(value: Any) -> List[str]:
    if not isinstance(value, dict):
        return []
    basis: List[str] = []
    for key, item in value.items():
        basis.append(f"{key}: {_compact_text(item, 180)}")
        if len(basis) >= 6:
            break
    return basis


def _human_title(value: str) -> str:
    normalized = value.replace("_", " ").replace("-", " ").strip()
    return normalized.title() if normalized else "State"


def _build_workflow_artifacts(workflow_dir: Path, workflow_id: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
    screenshots: List[Dict[str, Any]] = []
    screenshots_dir = workflow_dir / "screenshots"
    if screenshots_dir.exists():
        for image in sorted(screenshots_dir.glob("*.png")):
            rel = image.relative_to(workflow_dir).as_posix()
            screenshots.append(
                {
                    "name": image.name,
                    "path": rel,
                    "url": url_for("workflow_artifact", workflow_id=workflow_id, artifact_path=rel),
                }
            )

    init_screenshot = metadata.get("init_screenshot")
    init_screenshot_url = (
        url_for("workflow_artifact", workflow_id=workflow_id, artifact_path=init_screenshot)
        if isinstance(init_screenshot, str)
        else None
    )

    video_url = None
    for rel in ("screen_recording.mp4", "recording.mp4"):
        candidate = workflow_dir / rel
        if candidate.exists():
            video_url = url_for("workflow_artifact", workflow_id=workflow_id, artifact_path=rel)
            break

    return {
        "init_screenshot_url": init_screenshot_url,
        "screenshots": screenshots,
        "video_url": video_url,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenSpace dashboard API server")
    parser.add_argument("--host", default="127.0.0.1", help="Dashboard API host")
    parser.add_argument("--port", type=int, default=7788, help="Dashboard API port")
    parser.add_argument("--db-path", default=None, help="Dashboard skill store path")
    parser.add_argument(
        "--evidence-db-path",
        default=None,
        help="Dashboard evidence/audit store path; defaults to evidence.db next to --db-path",
    )
    parser.add_argument(
        "--evolution-storage-root",
        default=None,
        help="Workspace/evolution storage root containing .openspace/evidence.db",
    )
    parser.add_argument("--debug", action="store_true", help="Enable Flask debug mode")
    args = parser.parse_args()

    if not _is_loopback_host(args.host) and not _configured_dashboard_token():
        raise SystemExit(
            f"{DASHBOARD_TOKEN_ENV} must be set when binding the dashboard "
            f"to non-loopback host {args.host!r}."
        )

    app = create_app(
        db_path=args.db_path,
        evidence_db_path=args.evidence_db_path,
        evolution_storage_root=args.evolution_storage_root,
        bind_host=args.host,
    )

    from werkzeug.serving import run_simple
    run_simple(
        args.host,
        args.port,
        app,
        threaded=True,
        use_debugger=args.debug,
        use_reloader=args.debug,
    )


if __name__ == "__main__":
    main()
