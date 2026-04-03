from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent

from flask import Flask, abort, jsonify, send_from_directory, url_for

from openspace.recording.action_recorder import analyze_agent_actions, load_agent_actions
from openspace.recording.utils import load_recording_session
from openspace.skill_engine import SkillStore
from openspace.skill_engine.types import SkillRecord

API_PREFIX = "/api/v1"
FRONTEND_DIST_DIR = PROJECT_ROOT / "frontend" / "dist"
WORKFLOW_ROOTS = [
    PROJECT_ROOT / "logs" / "recordings",
    PROJECT_ROOT / "logs" / "trajectories",
    PROJECT_ROOT / "gdpval_bench" / "results",
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

_STORE: SkillStore | None = None


def create_app() -> Flask:
    app = Flask(__name__, static_folder=None)

    @app.route(f"{API_PREFIX}/health", methods=["GET"])
    def health() -> Any:
        workflows = _discover_workflow_dirs()
        store = _get_store()
        return jsonify(
            {
                "status": "ok",
                "project_root": str(PROJECT_ROOT),
                "db_path": str(store.db_path),
                "db_exists": store.db_path.exists(),
                "frontend_dist_exists": FRONTEND_DIST_DIR.exists(),
                "workflow_roots": [str(path) for path in WORKFLOW_ROOTS],
                "workflow_count": len(workflows),
            }
        )

    @app.route(f"{API_PREFIX}/overview", methods=["GET"])
    def overview() -> Any:
        store = _get_store()
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
                    "workflow_count": len(workflows),
                    "frontend_dist_exists": FRONTEND_DIST_DIR.exists(),
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
        store = _get_store()
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
        store = _get_store()
        skills = list(store.load_all(active_only=False).values())
        return jsonify(_build_skill_stats(store, skills))

    @app.route(f"{API_PREFIX}/skills/<skill_id>", methods=["GET"])
    def skill_detail(skill_id: str) -> Any:
        store = _get_store()
        record = store.load_record(skill_id)
        if not record:
            abort(404, description=f"Unknown skill_id: {skill_id}")

        detail = _serialize_skill(record, include_recent_analyses=True)
        detail["lineage_graph"] = _build_lineage_payload(skill_id, store)
        detail["recent_analyses"] = [analysis.to_dict() for analysis in store.load_analyses(skill_id=skill_id, limit=10)]
        detail["source"] = _load_skill_source(record)
        return jsonify(detail)

    @app.route(f"{API_PREFIX}/skills/<skill_id>/lineage", methods=["GET"])
    def skill_lineage(skill_id: str) -> Any:
        store = _get_store()
        if not store.load_record(skill_id):
            abort(404, description=f"Unknown skill_id: {skill_id}")
        return jsonify(_build_lineage_payload(skill_id, store))

    @app.route(f"{API_PREFIX}/skills/<skill_id>/source", methods=["GET"])
    def skill_source(skill_id: str) -> Any:
        store = _get_store()
        record = store.load_record(skill_id)
        if not record:
            abort(404, description=f"Unknown skill_id: {skill_id}")
        return jsonify(_load_skill_source(record))

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

        if FRONTEND_DIST_DIR.exists():
            requested = FRONTEND_DIST_DIR / path if path else FRONTEND_DIST_DIR / "index.html"
            if path and requested.exists() and requested.is_file():
                return send_from_directory(str(FRONTEND_DIST_DIR), path)
            return send_from_directory(str(FRONTEND_DIST_DIR), "index.html")

        return jsonify(
            {
                "message": "OpenSpace dashboard API is running.",
                "frontend": "Build frontend/ first or run the Vite dev server.",
            }
        )

    return app


def _get_store() -> SkillStore:
    global _STORE
    if _STORE is None:
        _STORE = SkillStore()
    return _STORE


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


def _skill_score(record: SkillRecord) -> float:
    return round(record.effective_rate * 100, 1)


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
        key=lambda item: (_skill_score(item), item.total_selections, _naive_dt(item.last_updated).timestamp()),
        reverse=True,
    )


def _build_skill_stats(store: SkillStore, skills: List[SkillRecord]) -> Dict[str, Any]:
    stats = store.get_stats(active_only=False)
    avg_score = round(sum(_skill_score(item) for item in skills) / len(skills), 1) if skills else 0.0
    skills_with_recent_analysis = sum(1 for item in skills if item.recent_analyses)
    return {
        **stats,
        "average_score": avg_score,
        "skills_with_activity": sum(1 for item in skills if item.total_selections > 0),
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
    # Instruction fallback chain: top-level → retrieved_tools.instruction → skill_selection.task
    instruction = (
        metadata.get("instruction")
        or (metadata.get("retrieved_tools") or {}).get("instruction")
        or (metadata.get("skill_selection") or {}).get("task")
        or ""
    )

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
        total_steps = statistics.get("total_steps", 0)
        if total_steps > 0:
            status = "success"
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
        "task_id": metadata.get("task_id") or metadata.get("task_name") or workflow_dir.name,
        "task_name": metadata.get("task_name") or metadata.get("task_id") or workflow_dir.name,
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
    parser.add_argument("--debug", action="store_true", help="Enable Flask debug mode")
    args = parser.parse_args()

    app = create_app()

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
