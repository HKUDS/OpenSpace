"""Historical evidence backfill for sessions, recordings, and skill storage."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from openspace.services.session.storage import SessionStorage
from openspace.skill_engine.evidence.session_adapter import (
    SessionEvidenceAdapter,
    _metadata_agent_id,
    _metadata_generation,
    _metadata_parent_task_id,
    _metadata_task_id,
    _payload_from_storage_entry,
)
from openspace.skill_engine.evidence.skill_adapter import SkillEvidenceAdapter
from openspace.skill_engine.evidence.store import EvidenceStore
from openspace.skill_engine.evidence.tool_adapter import ToolEvidenceAdapter
from openspace.skill_engine.evidence.types import EvidenceEvent, ResourceRef
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class BackfillResult:
    source: str
    scanned: int
    created_refs: int
    skipped: int
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EvidenceBackfill:
    """Best-effort audit backfill with idempotent EvidenceStore writes."""

    def __init__(
        self,
        evidence_store: EvidenceStore,
        *,
        skill_store: Any | None = None,
        session_storage_config_home: str | Path | None = None,
        cwd: str | Path | None = None,
        quality_source: Any | None = None,
    ) -> None:
        self.evidence_store = evidence_store
        self.skill_store = skill_store
        self.session_storage_config_home = (
            Path(session_storage_config_home).expanduser().resolve()
            if session_storage_config_home is not None
            else None
        )
        self.cwd = Path(cwd).expanduser().resolve() if cwd is not None else None
        self.quality_source = quality_source
        self._session_adapter = SessionEvidenceAdapter(evidence_store)
        self._skill_adapter = SkillEvidenceAdapter(evidence_store)
        self._tool_adapter = ToolEvidenceAdapter(evidence_store)

    def backfill_session(self, session_id: str) -> BackfillResult:
        scanned = 0
        created_refs = 0
        skipped = 0
        errors: list[str] = []
        try:
            storage = SessionStorage.for_session(
                str(session_id),
                cwd=self.cwd,
                config_home=self.session_storage_config_home,
                create=False,
            )
            loaded = storage.load()
        except Exception as exc:
            return BackfillResult(
                source=f"session:{session_id}",
                scanned=0,
                created_refs=0,
                skipped=0,
                errors=[str(exc)],
            )

        transcript_path = loaded.transcript_path
        if not transcript_path.is_file():
            return BackfillResult(
                source=f"session:{session_id}",
                scanned=0,
                created_refs=0,
                skipped=1,
                errors=[f"missing transcript: {transcript_path}"],
            )

        scoped_task_id: str | None = None
        scoped_parent_task_id: str | None = None
        scoped_agent_id: str | None = "primary"
        generation = int(loaded.current_generation or 0)
        message_index = 0

        for line_number, entry, error in _iter_jsonl_with_errors(transcript_path):
            if error:
                errors.append(f"{transcript_path}:{line_number}: {error}")
                continue
            if not isinstance(entry, Mapping):
                skipped += 1
                continue
            scanned += 1
            entry_type = str(entry.get("type") or entry.get("entry_type") or "")
            data = entry.get("data") if isinstance(entry.get("data"), Mapping) else {}
            if entry_type == "session-metadata":
                scoped_task_id = (
                    _metadata_task_id(data)
                    or scoped_task_id
                )
                scoped_parent_task_id = (
                    _metadata_parent_task_id(data)
                    or scoped_parent_task_id
                )
                scoped_agent_id = (
                    _metadata_agent_id(data)
                    or scoped_agent_id
                    or "primary"
                )
                generation = _metadata_generation(data, generation)
                skipped += 1
                continue
            if entry_type == "transcript-rewrite":
                generation = _safe_int(data.get("new_generation"), generation)
            payload = _payload_from_storage_entry(
                entry,
                session_id=loaded.session_id,
                session_dir=str(loaded.session_dir),
                transcript_path=str(loaded.transcript_path),
                tool_results_dir=str(storage.tool_results_dir),
                file_history_dir=str(storage.file_history_dir),
                transcript_generation=generation,
                task_id=scoped_task_id,
                parent_task_id=scoped_parent_task_id,
                agent_id=scoped_agent_id,
                message_index=message_index,
            )
            if payload is None:
                skipped += 1
                continue
            if entry_type == "message":
                message_index += 1
            event = self._session_adapter.build_event(entry_type, payload)
            if event is None:
                skipped += 1
                continue
            created_refs += self._ingest_counting(event, errors)

        return BackfillResult(
            source=f"session:{session_id}",
            scanned=scanned,
            created_refs=created_refs,
            skipped=skipped,
            errors=errors,
        )

    def backfill_recording(self, recording_dir: str | Path) -> BackfillResult:
        root = Path(recording_dir).expanduser().resolve()
        scanned = 0
        created_refs = 0
        skipped = 0
        errors: list[str] = []
        if not root.is_dir():
            return BackfillResult(
                source=f"recording:{root}",
                scanned=0,
                created_refs=0,
                skipped=0,
                errors=[f"recording dir not found: {root}"],
            )

        metadata, metadata_error = _read_json_object(root / "metadata.json")
        if metadata_error:
            errors.append(f"{root / 'metadata.json'}: {metadata_error}")
        session_id = _none_or_str(metadata.get("session_id"))
        task_id = (
            _none_or_str(metadata.get("task_id"))
            or _none_or_str(metadata.get("task_name"))
            or root.name
        )
        agent_id = _none_or_str(metadata.get("agent_name")) or "recording"

        for kind, path in _recording_artifacts(root):
            scanned += 1
            if not path.exists():
                skipped += 1
                continue
            event = self._recording_artifact_event(
                kind,
                path,
                root=root,
                session_id=session_id,
                task_id=task_id,
                agent_id=agent_id,
                metadata=metadata,
            )
            created_refs += self._ingest_counting(event, errors)

        for jsonl_name in ("conversations.jsonl", "traj.jsonl", "agent_actions.jsonl"):
            path = root / jsonl_name
            if not path.is_file():
                continue
            for line_number, entry, error in _iter_jsonl_with_errors(path):
                if error:
                    errors.append(f"{path}:{line_number}: {error}")
                    continue
                if not isinstance(entry, Mapping):
                    skipped += 1
                    continue
                scanned += 1
                if jsonl_name == "traj.jsonl":
                    event = self._recording_tool_event(
                        entry,
                        root=root,
                        session_id=session_id,
                        task_id=task_id,
                        agent_id=agent_id,
                    )
                else:
                    event = self._recording_line_event(
                        jsonl_name,
                        line_number,
                        entry,
                        root=root,
                        session_id=session_id,
                        task_id=task_id,
                        agent_id=agent_id,
                    )
                if event is None:
                    skipped += 1
                    continue
                created_refs += self._ingest_counting(event, errors)

        return BackfillResult(
            source=f"recording:{root}",
            scanned=scanned,
            created_refs=created_refs,
            skipped=skipped,
            errors=errors,
        )

    def backfill_skill_store(self) -> BackfillResult:
        if self.skill_store is None:
            return BackfillResult(
                source="skill_store",
                scanned=0,
                created_refs=0,
                skipped=0,
                errors=["skill_store is not configured"],
            )
        scanned = 0
        created_refs = 0
        skipped = 0
        errors: list[str] = []

        load_all = getattr(self.skill_store, "load_all", None)
        if callable(load_all):
            try:
                for record in (load_all(active_only=False) or {}).values():
                    scanned += 1
                    payload = _skill_record_payload(record)
                    event = self._skill_adapter.build_event("skill_record", payload)
                    if event is None:
                        skipped += 1
                        continue
                    created_refs += self._ingest_counting(event, errors)
            except Exception as exc:
                errors.append(f"skill_records: {exc}")

        load_skill_events = getattr(self.skill_store, "load_skill_events", None)
        if callable(load_skill_events):
            try:
                for row in load_skill_events(limit=10_000) or []:
                    scanned += 1
                    event = self._skill_adapter.build_event("skill_event", dict(row))
                    if event is None:
                        skipped += 1
                        continue
                    created_refs += self._ingest_counting(event, errors)
            except Exception as exc:
                errors.append(f"skill_events: {exc}")

        load_all_analyses = getattr(self.skill_store, "load_all_analyses", None)
        if callable(load_all_analyses):
            try:
                for analysis in load_all_analyses(limit=10_000) or []:
                    scanned += 1
                    event = self._execution_analysis_event(analysis)
                    created_refs += self._ingest_counting(event, errors)
            except Exception as exc:
                errors.append(f"execution_analyses: {exc}")

        for row in self._skill_tool_dep_rows(errors):
            scanned += 1
            event = self._tool_dependency_event(row)
            created_refs += self._ingest_counting(event, errors)

        for payload in self._quality_payloads(errors):
            scanned += 1
            event = self._tool_adapter.build_event("tool_quality_record", payload)
            if event is None:
                skipped += 1
                continue
            created_refs += self._ingest_counting(event, errors)

        return BackfillResult(
            source="skill_store",
            scanned=scanned,
            created_refs=created_refs,
            skipped=skipped,
            errors=errors,
        )

    def _recording_artifact_event(
        self,
        kind: str,
        path: Path,
        *,
        root: Path,
        session_id: str | None,
        task_id: str | None,
        agent_id: str | None,
        metadata: Mapping[str, Any],
    ) -> EvidenceEvent:
        created_at = _utc_now()
        rel = str(path.relative_to(root))
        ref = ResourceRef(
            ref_id=f"recording_ref:{task_id or 'none'}:{kind}:{_digest(str(path))[:16]}",
            ref_type="recording_ref",
            uri=str(path),
            session_id=session_id,
            task_id=task_id,
            agent_id=agent_id,
            producer="recording_backfill",
            created_at=created_at,
            reliability="fallback",
            role="supporting",
            hash=_file_hash(path) if path.is_file() else None,
            preview=f"recording {kind} {rel}",
            metadata={
                "recording_dir": str(root),
                "artifact_kind": kind,
                "relative_path": rel,
                "task_name": metadata.get("task_name"),
                "source": "recording_backfill",
            },
        )
        return EvidenceEvent.create(
            event_id=f"evt_recording_artifact_{_digest(ref.ref_id)}",
            event_type="recording_backfill_artifact",
            producer="recording_backfill",
            created_at=created_at,
            session_id=session_id,
            task_id=task_id,
            agent_id=agent_id,
            idempotency_key=f"recording:artifact:{ref.ref_id}",
            supporting_refs=[ref],
            metadata={"artifact_kind": kind, "relative_path": rel},
        )

    def _recording_line_event(
        self,
        jsonl_name: str,
        line_number: int,
        entry: Mapping[str, Any],
        *,
        root: Path,
        session_id: str | None,
        task_id: str | None,
        agent_id: str | None,
    ) -> EvidenceEvent:
        created_at = _utc_now()
        digest = _digest({"file": jsonl_name, "line": line_number, "entry": entry})
        ref = ResourceRef(
            ref_id=f"recording_ref:{task_id or 'none'}:{jsonl_name}:{digest[:16]}",
            ref_type="recording_ref",
            uri=f"{root / jsonl_name}#L{line_number}",
            session_id=session_id,
            task_id=task_id,
            agent_id=agent_id,
            producer="recording_backfill",
            created_at=created_at,
            reliability="fallback",
            role="supporting",
            preview=str(entry.get("type") or entry.get("event_type") or jsonl_name)[:200],
            metadata={
                "recording_dir": str(root),
                "jsonl_file": jsonl_name,
                "line_number": line_number,
                "entry_type": entry.get("type") or entry.get("event_type"),
                "source": "recording_backfill",
            },
        )
        return EvidenceEvent.create(
            event_id=f"evt_recording_line_{digest}",
            event_type="recording_backfill_line",
            producer="recording_backfill",
            created_at=created_at,
            session_id=session_id,
            task_id=task_id,
            agent_id=agent_id,
            idempotency_key=f"recording:line:{ref.ref_id}",
            supporting_refs=[ref],
            metadata={"jsonl_file": jsonl_name, "line_number": line_number},
        )

    def _recording_tool_event(
        self,
        entry: Mapping[str, Any],
        *,
        root: Path,
        session_id: str | None,
        task_id: str | None,
        agent_id: str | None,
    ) -> EvidenceEvent | None:
        tool_name = _none_or_str(entry.get("tool"))
        if not tool_name:
            return None
        step = _none_or_str(entry.get("step")) or _digest(entry)[:12]
        backend = _none_or_str(entry.get("backend")) or "unknown"
        server = _none_or_str(entry.get("server")) or "default"
        tool_use_id = _none_or_str(entry.get("tool_use_id")) or f"recording-step-{step}"
        ref_id = f"tool_event:{session_id or 'none'}:{task_id or 'none'}:{agent_id or 'recording'}:{tool_use_id}"
        if self.evidence_store.get_ref(ref_id) is not None:
            return None
        created_at = _none_or_str(entry.get("timestamp")) or _utc_now()
        result = entry.get("result") if isinstance(entry.get("result"), Mapping) else {}
        status = _none_or_str(result.get("status")) or _none_or_str(entry.get("status")) or "unknown"
        ref = ResourceRef(
            ref_id=ref_id,
            ref_type="tool_event",
            uri=f"{root / 'traj.jsonl'}#step={step}",
            session_id=session_id,
            task_id=task_id,
            agent_id=agent_id,
            producer="recording_backfill",
            created_at=created_at,
            reliability="fallback",
            role="supporting",
            preview=f"{tool_name} {status}",
            metadata={
                "tool_use_id": tool_use_id,
                "tool_key": f"{backend}:{server}:{tool_name}",
                "tool_name": tool_name,
                "backend": backend,
                "server_name": server,
                "status": status,
                "step": step,
                "command": entry.get("command"),
                "parameters": entry.get("parameters") if isinstance(entry.get("parameters"), Mapping) else {},
                "recording_dir": str(root),
                "source": "recording_backfill",
            },
        )
        return EvidenceEvent.create(
            event_id=f"evt_recording_tool_{_digest(ref_id)}",
            event_type="recording_backfill_tool_event",
            producer="recording_backfill",
            created_at=created_at,
            session_id=session_id,
            task_id=task_id,
            agent_id=agent_id,
            idempotency_key=f"recording:tool_event:{ref_id}",
            supporting_refs=[ref],
            metadata={"tool_name": tool_name, "status": status},
        )

    def _execution_analysis_event(self, analysis: Any) -> EvidenceEvent:
        created_at = _none_or_str(getattr(analysis, "analyzed_at", None)) or _utc_now()
        task_id = _none_or_str(getattr(analysis, "task_id", None))
        payload = _analysis_payload(analysis)
        ref_id = f"execution_analysis:{task_id or _digest(payload)[:16]}"
        ref = ResourceRef(
            ref_id=ref_id,
            ref_type="execution_analysis",
            task_id=task_id,
            producer="skill_store_backfill",
            created_at=created_at,
            reliability="persisted",
            role="supporting",
            preview=str(getattr(analysis, "execution_note", "") or "")[:500],
            metadata=payload,
            raw_backrefs=[
                f"skill_record:{skill_id}"
                for skill_id in getattr(analysis, "skill_ids", []) or []
            ],
        )
        return EvidenceEvent.create(
            event_id=f"evt_skill_store_analysis_{_digest(ref_id)}",
            event_type="skill_store_execution_analysis",
            producer="skill_store_backfill",
            created_at=created_at,
            task_id=task_id,
            idempotency_key=f"skill_store:execution_analysis:{ref_id}",
            supporting_refs=[ref],
            metadata={"task_id": task_id},
        )

    def _tool_dependency_event(self, row: Mapping[str, Any]) -> EvidenceEvent:
        created_at = _utc_now()
        skill_id = _none_or_str(row.get("skill_id")) or "unknown"
        tool_key = _none_or_str(row.get("tool_key")) or "unknown"
        ref = ResourceRef(
            ref_id=f"tool_quality_record:skill_dep:{skill_id}:{_digest(tool_key)[:16]}",
            ref_type="tool_quality_record",
            producer="skill_store_backfill",
            created_at=created_at,
            reliability="summary_only",
            role="supporting",
            preview=f"{skill_id} depends on {tool_key}",
            metadata={
                "skill_id": skill_id,
                "tool_key": tool_key,
                "critical": bool(row.get("critical")),
                "source": "skill_tool_deps",
            },
            raw_backrefs=[f"skill_record:{skill_id}"],
        )
        return EvidenceEvent.create(
            event_id=f"evt_skill_tool_dep_{_digest(ref.ref_id)}",
            event_type="skill_store_tool_dependency",
            producer="skill_store_backfill",
            created_at=created_at,
            idempotency_key=f"skill_store:tool_dependency:{ref.ref_id}",
            supporting_refs=[ref],
            metadata={"skill_id": skill_id, "tool_key": tool_key},
        )

    def _skill_tool_dep_rows(self, errors: list[str]) -> list[dict[str, Any]]:
        reader = getattr(self.skill_store, "_reader", None)
        if not callable(reader):
            return []
        try:
            with reader() as conn:
                rows = conn.execute(
                    "SELECT skill_id, tool_key, critical FROM skill_tool_deps"
                ).fetchall()
                return [dict(row) for row in rows]
        except Exception as exc:
            errors.append(f"skill_tool_deps: {exc}")
            return []

    def _quality_payloads(self, errors: list[str]) -> list[dict[str, Any]]:
        if self.quality_source is None:
            return []
        try:
            from openspace.skill_engine.evidence.tool_adapter import _quality_payloads_from_source

            return _quality_payloads_from_source(self.quality_source, limit=20)
        except Exception as exc:
            errors.append(f"tool_quality: {exc}")
            return []

    def _ingest_counting(self, event: EvidenceEvent, errors: list[str]) -> int:
        ref_ids = [ref.ref_id for ref in event.all_refs() if ref.ref_id]
        before = {ref_id for ref_id in ref_ids if self.evidence_store.get_ref(ref_id) is not None}
        try:
            self.evidence_store.ingest_event(event)
        except Exception as exc:
            errors.append(f"{event.event_id}: {exc}")
            return 0
        return sum(
            1
            for ref_id in ref_ids
            if ref_id not in before and self.evidence_store.get_ref(ref_id) is not None
        )


def backfill_session(
    session_id: str,
    *,
    evidence_store: EvidenceStore,
    session_storage_config_home: str | Path | None = None,
    cwd: str | Path | None = None,
) -> BackfillResult:
    return EvidenceBackfill(
        evidence_store,
        session_storage_config_home=session_storage_config_home,
        cwd=cwd,
    ).backfill_session(session_id)


def backfill_recording(
    recording_dir: str | Path,
    *,
    evidence_store: EvidenceStore,
) -> BackfillResult:
    return EvidenceBackfill(evidence_store).backfill_recording(recording_dir)


def backfill_skill_store(
    *,
    evidence_store: EvidenceStore,
    skill_store: Any,
    quality_source: Any | None = None,
) -> BackfillResult:
    return EvidenceBackfill(
        evidence_store,
        skill_store=skill_store,
        quality_source=quality_source,
    ).backfill_skill_store()


def _recording_artifacts(root: Path) -> Iterable[tuple[str, Path]]:
    for name in ("metadata.json", "conversations.jsonl", "traj.jsonl", "summary.json", "agent_actions.jsonl"):
        yield name.rsplit(".", 1)[0], root / name
    for dirname in ("screenshots", "multimodal", "plans"):
        base = root / dirname
        if not base.exists():
            yield dirname, base
            continue
        for path in sorted(item for item in base.rglob("*") if item.is_file()):
            yield dirname, path


def _skill_record_payload(record: Any) -> dict[str, Any]:
    lineage = getattr(record, "lineage", None)
    created_at = _iso_or_str(getattr(record, "last_updated", None)) or _utc_now()
    return {
        "skill_id": getattr(record, "skill_id", ""),
        "name": getattr(record, "name", ""),
        "description": getattr(record, "description", ""),
        "path": getattr(record, "path", ""),
        "is_active": bool(getattr(record, "is_active", False)),
        "category": _enum_value(getattr(record, "category", "")),
        "tags": list(getattr(record, "tags", []) or []),
        "visibility": _enum_value(getattr(record, "visibility", "")),
        "creator_id": getattr(record, "creator_id", ""),
        "lineage_origin": _enum_value(getattr(lineage, "origin", "")),
        "lineage_generation": getattr(lineage, "generation", 0),
        "lineage_parent_skill_ids": list(getattr(lineage, "parent_skill_ids", []) or []),
        "lineage_source_task_id": getattr(lineage, "source_task_id", None),
        "lineage_change_summary": getattr(lineage, "change_summary", ""),
        "lineage_evolution_action_id": getattr(lineage, "evolution_action_id", None),
        "lineage_provenance_refs": list(getattr(lineage, "provenance_refs", []) or []),
        "lineage_created_at": _iso_or_str(getattr(lineage, "created_at", None)),
        "lineage_created_by": getattr(lineage, "created_by", ""),
        "tool_dependencies": list(getattr(record, "tool_dependencies", []) or []),
        "critical_tools": list(getattr(record, "critical_tools", []) or []),
        "total_selections": getattr(record, "total_selections", 0),
        "total_invocations": getattr(record, "total_invocations", 0),
        "total_applied": getattr(record, "total_applied", 0),
        "total_completions": getattr(record, "total_completions", 0),
        "total_fallbacks": getattr(record, "total_fallbacks", 0),
        "first_seen": _iso_or_str(getattr(record, "first_seen", None)),
        "last_updated": _iso_or_str(getattr(record, "last_updated", None)),
        "lifecycle_event": "backfilled",
        "source": "skill_store_backfill",
        "created_at": created_at,
    }


def _analysis_payload(analysis: Any) -> dict[str, Any]:
    to_dict = getattr(analysis, "to_dict", None)
    if callable(to_dict):
        try:
            payload = to_dict()
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass
    return {
        "task_id": getattr(analysis, "task_id", ""),
        "task_completed": bool(getattr(analysis, "task_completed", False)),
        "execution_note": getattr(analysis, "execution_note", ""),
        "tool_issues": list(getattr(analysis, "tool_issues", []) or []),
        "analyzed_by": getattr(analysis, "analyzed_by", ""),
        "analyzed_at": _iso_or_str(getattr(analysis, "analyzed_at", None)),
    }


def _iter_jsonl_with_errors(path: Path) -> Iterable[tuple[int, dict[str, Any] | None, str | None]]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    loaded = json.loads(line)
                except json.JSONDecodeError as exc:
                    yield line_number, None, f"invalid json: {exc.msg}"
                    continue
                if isinstance(loaded, dict):
                    yield line_number, loaded, None
                else:
                    yield line_number, None, "json line is not an object"
    except OSError as exc:
        yield 0, None, str(exc)


def _read_json_object(path: Path) -> tuple[dict[str, Any], str | None]:
    if not path.is_file():
        return {}, None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {}, str(exc)
    return (loaded if isinstance(loaded, dict) else {}), None


def _file_hash(path: Path) -> str | None:
    try:
        if path.is_file():
            return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        return None
    return None


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _iso_or_str(value: Any) -> str:
    if value is None:
        return ""
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            return str(isoformat())
        except Exception:
            return str(value)
    return str(value)


def _none_or_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _digest(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:24]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "BackfillResult",
    "EvidenceBackfill",
    "backfill_recording",
    "backfill_session",
    "backfill_skill_store",
]
