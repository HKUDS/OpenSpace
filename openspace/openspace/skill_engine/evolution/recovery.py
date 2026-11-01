"""Crash recovery and reconciliation for evolution commits."""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from openspace.skill_engine.patch import collect_skill_snapshot
from openspace.skill_engine.registry import write_skill_id
from openspace.skill_engine.skill_utils import validate_skill_dir
from openspace.utils.logging import Logger

from .audit import EvolutionActionRecord

logger = Logger.get_logger(__name__)

DEFAULT_STALE_JOB_TIMEOUT_S = 30 * 60.0
DEFAULT_STAGING_RETENTION_S = 7 * 24 * 60 * 60.0
_TERMINAL_ACTION_STATUSES = {
    "committed",
    "committed_reconciled",
    "failed",
    "failed_needs_review",
}


@dataclass(frozen=True, slots=True)
class EvolutionRecoveryResult:
    stale_jobs_recovered: int = 0
    actions_reconciled: int = 0
    actions_failed: int = 0
    actions_needing_review: int = 0
    staging_dirs_removed: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EvolutionRecovery:
    """Best-effort startup recovery across TriggerJobs, EvidenceStore, and skills."""

    def __init__(
        self,
        *,
        evidence_store: Any,
        skill_store: Any | None = None,
        registry: Any | None = None,
        trigger_engine: Any | None = None,
        trigger_store: Any | None = None,
        stale_job_timeout_s: float = DEFAULT_STALE_JOB_TIMEOUT_S,
        staging_retention_s: float = DEFAULT_STAGING_RETENTION_S,
    ) -> None:
        self.evidence_store = evidence_store
        self.skill_store = skill_store
        self.registry = registry
        self.trigger_engine = trigger_engine
        self.trigger_store = trigger_store or getattr(trigger_engine, "store", None)
        self.stale_job_timeout_s = max(0.0, float(stale_job_timeout_s))
        self.staging_retention_s = max(0.0, float(staging_retention_s))

    def run_startup_recovery(self) -> EvolutionRecoveryResult:
        """Run all startup recovery steps without raising to runtime init."""

        stale_jobs = 0
        reconciled = 0
        failed = 0
        needs_review = 0
        staging_removed = 0
        errors: list[str] = []

        try:
            stale_jobs = self._recover_stale_jobs()
        except Exception as exc:
            logger.debug("Evolution stale job recovery failed", exc_info=True)
            errors.append(f"stale_jobs: {exc}")

        try:
            action_result = self._reconcile_committing_actions()
            reconciled += action_result.actions_reconciled
            failed += action_result.actions_failed
            needs_review += action_result.actions_needing_review
            errors.extend(action_result.errors)
        except Exception as exc:
            logger.debug("Evolution action reconciliation failed", exc_info=True)
            errors.append(f"actions: {exc}")

        try:
            staging_removed = self._cleanup_stale_staging_dirs()
        except Exception as exc:
            logger.debug("Evolution staging cleanup failed", exc_info=True)
            errors.append(f"staging_cleanup: {exc}")

        return EvolutionRecoveryResult(
            stale_jobs_recovered=stale_jobs,
            actions_reconciled=reconciled,
            actions_failed=failed,
            actions_needing_review=needs_review,
            staging_dirs_removed=staging_removed,
            errors=errors,
        )

    def _recover_stale_jobs(self) -> int:
        recover = getattr(self.trigger_engine, "recover_stale_jobs", None)
        if callable(recover):
            return int(recover(timeout_s=self.stale_job_timeout_s) or 0)
        store_recover = getattr(self.trigger_store, "recover_stale_jobs", None)
        if callable(store_recover):
            return int(store_recover(timeout_s=self.stale_job_timeout_s) or 0)
        return 0

    def _reconcile_committing_actions(self) -> EvolutionRecoveryResult:
        if self.skill_store is None:
            return EvolutionRecoveryResult()
        list_actions = getattr(self.evidence_store, "list_actions", None)
        if not callable(list_actions):
            return EvolutionRecoveryResult()
        actions = list_actions(status="committing", limit=100)
        reconciled = 0
        failed = 0
        needs_review = 0
        errors: list[str] = []
        for action in actions:
            try:
                status = self._reconcile_action(action)
                if status == "committed_reconciled":
                    reconciled += 1
                elif status == "failed_needs_review":
                    needs_review += 1
                elif status == "failed":
                    failed += 1
            except Exception as exc:
                logger.debug(
                    "Evolution action reconciliation failed for %s",
                    getattr(action, "action_id", ""),
                    exc_info=True,
                )
                errors.append(f"{getattr(action, 'action_id', '')}: {exc}")
                self._record_failure(action, "recovery", "failed_retryable", str(exc))
        return EvolutionRecoveryResult(
            actions_reconciled=reconciled,
            actions_failed=failed,
            actions_needing_review=needs_review,
            errors=errors,
        )

    def _reconcile_action(self, action: EvolutionActionRecord) -> str:
        target_dir = _path_or_none(action.active_target_dir)
        proposed_dir = _resolve_proposed_dir(action)
        record = self._load_skill_record(action)
        if record is not None:
            mismatch_reason = self._record_reconcile_mismatch(
                action,
                record,
                target_dir,
                proposed_dir,
            )
            if mismatch_reason:
                self._finalize_failed(action, "failed_needs_review", mismatch_reason)
                return "failed_needs_review"
            self._finalize_committed(action, record)
            return "committed_reconciled"

        backup_dir = _path_or_none(action.backup_dir)
        target_unchanged = _dirs_equivalent(target_dir, backup_dir)
        missing_proposed = proposed_dir is None or not proposed_dir.is_dir()

        if target_unchanged or missing_proposed:
            reason = (
                "recovery: active files unchanged"
                if target_unchanged
                else "recovery: proposed staging content missing"
            )
            self._finalize_failed(action, "failed", reason)
            return "failed"

        if target_dir is not None and _dirs_equivalent(target_dir, proposed_dir):
            restored = self._restore_backup(action, target_dir, backup_dir)
            if restored:
                self._finalize_failed(
                    action,
                    "failed",
                    "recovery: active files matched unrecorded proposed staging; backup restored",
                )
                return "failed"
            self._finalize_failed(
                action,
                "failed_needs_review",
                "recovery: active files matched unrecorded proposed staging but backup restore failed",
            )
            return "failed_needs_review"

        self._finalize_failed(
            action,
            "failed_needs_review",
            "recovery: active target state is unknown and SkillStore record is missing",
        )
        return "failed_needs_review"

    def _load_skill_record(self, action: EvolutionActionRecord) -> Any | None:
        if self.skill_store is None:
            return None
        if action.skill_id:
            load_record = getattr(self.skill_store, "load_record", None)
            if callable(load_record):
                record = load_record(action.skill_id)
                if record is not None:
                    return record
        return self._find_skill_record_for_action(action)

    def _find_skill_record_for_action(self, action: EvolutionActionRecord) -> Any | None:
        records = self._load_all_skill_records()
        if not records:
            return None

        action_id = str(action.action_id or "")
        target_dir = _path_or_none(action.active_target_dir)
        staging_id = Path(action.staging_dir).name if action.staging_dir else ""
        evidence_refs = {str(ref) for ref in action.evidence_refs if ref}
        path_matched_records = [
            record for record in records if _record_path_matches_target(record, target_dir)
        ]

        if action_id:
            for record in records:
                if _record_lineage_value(record, "evolution_action_id") == action_id:
                    return record

        if staging_id:
            for record in path_matched_records:
                if staging_id in _record_search_blob(record):
                    return record

        if evidence_refs:
            for record in path_matched_records:
                provenance_refs = {
                    str(ref)
                    for ref in (_record_lineage_value(record, "provenance_refs") or [])
                    if ref
                }
                if evidence_refs.intersection(provenance_refs):
                    return record

        if action_id:
            for record in path_matched_records:
                if action_id in _record_search_blob(record):
                    return record

        proposed_dir = _resolve_proposed_dir(action)
        if proposed_dir is not None and proposed_dir.is_dir():
            proposed_hash = _snapshot_hash(proposed_dir)
            for record in path_matched_records:
                if _record_snapshot_hash(record) == proposed_hash:
                    return record

        return None

    def _load_all_skill_records(self) -> list[Any]:
        if self.skill_store is None:
            return []
        load_all = getattr(self.skill_store, "load_all", None)
        if not callable(load_all):
            return []
        records = load_all(active_only=False)
        if isinstance(records, dict):
            return list(records.values())
        return list(records or [])

    def _record_reconcile_mismatch(
        self,
        action: EvolutionActionRecord,
        record: Any,
        target_dir: Path | None,
        proposed_dir: Path | None,
    ) -> str | None:
        record_dir = _record_skill_dir(record)
        if (target_dir is None or not target_dir.is_dir()) and record_dir is not None:
            target_dir = record_dir
        if target_dir is None or not target_dir.is_dir():
            return "recovery: active target missing; cannot verify SkillStore record"
        if proposed_dir is None or not proposed_dir.is_dir():
            return "recovery: proposed staging content missing; cannot verify SkillStore record"
        if not _record_path_matches_target(record, target_dir):
            return "recovery: SkillStore record path does not match active target"
        proposed_hash = _snapshot_hash(proposed_dir)
        if _snapshot_hash(target_dir) != proposed_hash:
            return "recovery: active target does not match proposed staging content"
        record_hash = _record_snapshot_hash(record)
        if record_hash and record_hash != proposed_hash:
            return "recovery: SkillStore record snapshot does not match proposed staging content"
        return None

    def _finalize_committed(self, action: EvolutionActionRecord, record: Any) -> None:
        target_dir = _path_or_none(action.active_target_dir)
        record_dir = _record_skill_dir(record)
        if (target_dir is None or not target_dir.is_dir()) and record_dir is not None:
            target_dir = record_dir
        if target_dir is None:
            raise RuntimeError("active target missing")
        validation_error = validate_skill_dir(target_dir)
        if validation_error:
            raise RuntimeError(f"active target invalid: {validation_error}")
        write_skill_id(target_dir, record.skill_id, raise_on_error=True)
        self._refresh_registry(action, target_dir, record)
        self.evidence_store.finalize_action(
            action.action_id,
            status="committed_reconciled",
            skill_id=record.skill_id,
            changed_files=action.changed_files,
            backup_dir=action.backup_dir,
            raw_backrefs=action.evidence_refs,
        )

    def _refresh_registry(
        self,
        action: EvolutionActionRecord,
        target_dir: Path,
        record: Any,
    ) -> None:
        if self.registry is None:
            return
        load_skill_from_dir = getattr(self.registry, "load_skill_from_dir", None)
        if not callable(load_skill_from_dir):
            return
        meta = load_skill_from_dir(target_dir)
        if meta is None:
            raise RuntimeError(f"registry could not load skill from {target_dir}")
        if str(action.action_type).upper() == "FIX":
            old_skill_id = action.parent_skill_ids[0] if action.parent_skill_ids else record.skill_id
            update_skill = getattr(self.registry, "update_skill", None)
            if callable(update_skill):
                update_skill(old_skill_id, meta)
                return
        add_skill = getattr(self.registry, "add_skill", None)
        if callable(add_skill):
            add_skill(meta)

    def _restore_backup(
        self,
        action: EvolutionActionRecord,
        target_dir: Path | None,
        backup_dir: Path | None,
    ) -> bool:
        if str(action.action_type).upper() != "FIX":
            return False
        if target_dir is None or backup_dir is None or not backup_dir.is_dir():
            return False
        try:
            if target_dir.exists():
                shutil.rmtree(target_dir)
            shutil.copytree(backup_dir, target_dir)
            return True
        except Exception as exc:
            self._record_failure(action, "backup_restore", "failed_needs_review", str(exc))
            return False

    def _finalize_failed(
        self,
        action: EvolutionActionRecord,
        status: str,
        reason: str,
    ) -> None:
        self._record_failure(action, "recovery", status, reason)
        self.evidence_store.finalize_action(
            action.action_id,
            status=status,
            skill_id=action.skill_id,
            changed_files=action.changed_files,
            backup_dir=action.backup_dir,
            failure_reason=reason,
            raw_backrefs=action.evidence_refs,
        )

    def _record_failure(
        self,
        action: EvolutionActionRecord,
        phase: str,
        status: str,
        reason: str,
    ) -> None:
        recorder = getattr(self.evidence_store, "record_action_failure", None)
        if not callable(recorder):
            return
        try:
            recorder(action.action_id, phase=phase, status=status, error=reason)
        except Exception:
            logger.debug("Failed to record evolution recovery failure", exc_info=True)

    def _cleanup_stale_staging_dirs(self) -> int:
        list_actions = getattr(self.evidence_store, "list_actions", None)
        if not callable(list_actions):
            return 0
        actions = list_actions(limit=1000)
        active_staging = {
            str(Path(action.staging_dir).expanduser().resolve())
            for action in actions
            if action.staging_dir and action.commit_status not in _TERMINAL_ACTION_STATUSES
        }
        now = time.time()
        removed = 0
        for action in actions:
            if action.commit_status not in _TERMINAL_ACTION_STATUSES:
                continue
            if not action.staging_dir:
                continue
            staging = Path(action.staging_dir).expanduser().resolve()
            if str(staging) in active_staging or not staging.is_dir():
                continue
            try:
                if now - staging.stat().st_mtime < self.staging_retention_s:
                    continue
                shutil.rmtree(staging)
                removed += 1
            except Exception:
                logger.debug("Failed to clean evolution staging dir %s", staging, exc_info=True)
        return removed


def _resolve_proposed_dir(action: EvolutionActionRecord) -> Path | None:
    staging = _path_or_none(action.staging_dir)
    target = _path_or_none(action.active_target_dir)
    if staging is None:
        return None
    proposed_root = staging / "proposed"
    if not proposed_root.is_dir():
        return None
    if target is not None:
        preferred = proposed_root / target.name
        if preferred.is_dir():
            return preferred
    dirs = [child for child in proposed_root.iterdir() if child.is_dir()]
    if len(dirs) == 1:
        return dirs[0]
    return None


def _dirs_equivalent(left: Path | None, right: Path | None) -> bool:
    if left is None or right is None:
        return False
    if not left.exists() and not right.exists():
        return True
    if not left.is_dir() or not right.is_dir():
        return False
    return _snapshot_hash(left) == _snapshot_hash(right)


def _snapshot_hash(path: Path) -> str:
    snapshot = collect_skill_snapshot(path)
    return _snapshot_mapping_hash(snapshot)


def _snapshot_mapping_hash(snapshot: dict[str, str]) -> str:
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _record_snapshot_hash(record: Any) -> str | None:
    snapshot = _record_lineage_value(record, "content_snapshot")
    if not isinstance(snapshot, dict) or not snapshot:
        return None
    normalized = {str(key): str(value) for key, value in snapshot.items()}
    return _snapshot_mapping_hash(normalized)


def _record_path_matches_target(record: Any, target_dir: Path | None) -> bool:
    if target_dir is None:
        return False
    skill_dir = _record_skill_dir(record)
    if skill_dir is None:
        return False
    return skill_dir == target_dir


def _record_skill_dir(record: Any) -> Path | None:
    raw_path = str(getattr(record, "path", "") or "")
    if not raw_path:
        return None
    try:
        path = Path(raw_path).expanduser().resolve()
    except Exception:
        return None
    return path.parent if path.name == "SKILL.md" else path


def _record_lineage_value(record: Any, name: str) -> Any:
    lineage = getattr(record, "lineage", None)
    if lineage is None:
        return None
    if isinstance(lineage, dict):
        return lineage.get(name)
    return getattr(lineage, name, None)


def _record_search_blob(record: Any) -> str:
    parts = [
        getattr(record, "skill_id", ""),
        getattr(record, "name", ""),
        getattr(record, "path", ""),
        _record_lineage_value(record, "source_task_id"),
        _record_lineage_value(record, "change_summary"),
        _record_lineage_value(record, "evolution_action_id"),
    ]
    provenance = _record_lineage_value(record, "provenance_refs") or []
    parts.extend(provenance)
    return "\n".join(str(part or "") for part in parts)


def _path_or_none(value: str | None) -> Path | None:
    if not value:
        return None
    try:
        return Path(value).expanduser().resolve()
    except Exception:
        return None


__all__ = [
    "DEFAULT_STAGING_RETENTION_S",
    "DEFAULT_STALE_JOB_TIMEOUT_S",
    "EvolutionRecovery",
    "EvolutionRecoveryResult",
]
