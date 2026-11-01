"""Per-file history snapshots for edit/write undo support.

OpenSpace stores backups under the active ``SessionStorage`` directory.
Snapshots point at immutable backup files, and a ``None`` backup file name
means the target file did not exist at that version.
"""

from __future__ import annotations

import asyncio
import dataclasses
import difflib
import hashlib
import os
import shutil
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openspace.services.conversation.messages import get_message_uuid
from openspace.utils.logging import Logger

if TYPE_CHECKING:
    from openspace.services.session.storage import SessionStorage

logger = Logger.get_logger(__name__)

MAX_SNAPSHOTS = 100
FILE_HISTORY_DIRNAME = "file-history"
BACKUPS_DIRNAME = "backups"


@dataclass(slots=True)
class FileHistoryBackup:
    backup_file_name: str | None
    version: int
    backup_time: str
    size: int | None = None
    sha256: str | None = None
    mode: int | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "FileHistoryBackup":
        return cls(
            backup_file_name=_none_or_str(data.get("backup_file_name")),
            version=int(data.get("version") or 0),
            backup_time=str(data.get("backup_time") or _utc_now()),
            size=_none_or_int(data.get("size")),
            sha256=_none_or_str(data.get("sha256")),
            mode=_none_or_int(data.get("mode")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "backup_file_name": self.backup_file_name,
            "version": self.version,
            "backup_time": self.backup_time,
            "size": self.size,
            "sha256": self.sha256,
            "mode": self.mode,
        }


@dataclass(slots=True)
class FileSnapshot:
    snapshot_id: str
    message_uuid: str | None
    timestamp: str
    tracked_file_backups: dict[str, FileHistoryBackup] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "FileSnapshot":
        raw_backups = data.get("tracked_file_backups")
        backups: dict[str, FileHistoryBackup] = {}
        if isinstance(raw_backups, Mapping):
            for path, backup in raw_backups.items():
                if isinstance(backup, Mapping):
                    backups[str(path)] = FileHistoryBackup.from_mapping(backup)
        message_uuid = _none_or_str(
            data.get("message_uuid")
            or data.get("message_id")
        )
        snapshot_id = _none_or_str(data.get("snapshot_id")) or message_uuid or uuid.uuid4().hex
        return cls(
            snapshot_id=snapshot_id,
            message_uuid=message_uuid,
            timestamp=str(data.get("timestamp") or _utc_now()),
            tracked_file_backups=backups,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "message_uuid": self.message_uuid,
            "timestamp": self.timestamp,
            "tracked_file_backups": {
                path: backup.to_dict()
                for path, backup in self.tracked_file_backups.items()
            },
        }


@dataclass(slots=True)
class FileDiff:
    filepath: str
    snapshot_id: str
    diff: str
    insertions: int
    deletions: int
    changed: bool
    backup_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(slots=True)
class RevertResult:
    filepath: str
    snapshot_id: str
    changed: bool
    backup_path: str | None = None
    deleted: bool = False
    insertions: int = 0
    deletions: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class FileHistory:
    """Per-file backup state scoped to one OpenSpace session."""

    def __init__(
        self,
        *,
        session_storage: "SessionStorage | None" = None,
        session_dir: str | Path | None = None,
        cwd: str | Path | None = None,
        enabled: bool = True,
    ) -> None:
        self.session_storage = session_storage
        if session_storage is not None:
            self.session_dir = Path(session_storage.session_dir)
            self.cwd = str(Path(session_storage.cwd).expanduser().resolve())
        else:
            self.session_dir = Path(session_dir or Path.cwd()).expanduser().resolve()
            self.cwd = str(Path(cwd or Path.cwd()).expanduser().resolve())
        self.file_history_dir = self.session_dir / FILE_HISTORY_DIRNAME
        self.backups_dir = self.file_history_dir / BACKUPS_DIRNAME
        self.enabled = enabled
        self.snapshots: list[FileSnapshot] = []
        self.tracked_files: set[str] = set()
        self.snapshot_sequence = 0
        self._lock = asyncio.Lock()

    async def record_snapshot(
        self,
        filepath: str | Path,
        *,
        message_uuid: str | None = None,
        task_id: str | None = None,
        parent_task_id: str | None = None,
        agent_id: str | None = None,
    ) -> FileSnapshot | None:
        """Save the target file's pre-write bytes if this turn needs a backup."""

        if not self.enabled:
            return None
        path = self._resolve_path(filepath)
        tracking_path = self._tracking_path(path)

        async with self._lock:
            current = self._current_snapshot(message_uuid)
            if current is not None and tracking_path in current.tracked_file_backups:
                return current

            latest = self.snapshots[-1] if self.snapshots else None
            inherited = (
                dict(latest.tracked_file_backups)
                if latest is not None
                else {}
            )
            latest_backup = inherited.get(tracking_path)
            if latest_backup is not None and not self._origin_changed(path, latest_backup):
                if current is None and latest is not None:
                    current = FileSnapshot(
                        snapshot_id=message_uuid or uuid.uuid4().hex,
                        message_uuid=message_uuid,
                        timestamp=_utc_now(),
                        tracked_file_backups=inherited,
                    )
                    self.snapshots.append(current)
                    self._trim_snapshots()
                    self.snapshot_sequence += 1
                    await self._record_transcript(
                        current,
                        is_snapshot_update=False,
                        task_id=task_id,
                        parent_task_id=parent_task_id,
                        agent_id=agent_id,
                    )
                    return current
                return latest

            version = (latest_backup.version + 1) if latest_backup is not None else 1
            backup = self._create_backup(path, tracking_path, version)

            if current is None:
                current = FileSnapshot(
                    snapshot_id=message_uuid or uuid.uuid4().hex,
                    message_uuid=message_uuid,
                    timestamp=_utc_now(),
                    tracked_file_backups=inherited,
                )
                self.snapshots.append(current)
                is_snapshot_update = False
            else:
                is_snapshot_update = True

            current.tracked_file_backups[tracking_path] = backup
            self.tracked_files.add(tracking_path)
            self._trim_snapshots()
            self.snapshot_sequence += 1
            await self._record_transcript(
                current,
                is_snapshot_update=is_snapshot_update,
                task_id=task_id,
                parent_task_id=parent_task_id,
                agent_id=agent_id,
            )
            return current

    async def make_snapshot(
        self,
        *,
        message_uuid: str | None = None,
        task_id: str | None = None,
        parent_task_id: str | None = None,
        agent_id: str | None = None,
    ) -> FileSnapshot | None:
        """Create a full tracked-file snapshot for all tracked paths."""

        if not self.enabled:
            return None
        async with self._lock:
            latest = self.snapshots[-1] if self.snapshots else None
            backups = dict(latest.tracked_file_backups) if latest is not None else {}
            changed = False
            for tracking_path in list(self.tracked_files):
                path = self._expand_tracking_path(tracking_path)
                latest_backup = backups.get(tracking_path)
                if latest_backup is not None and not self._origin_changed(path, latest_backup):
                    continue
                version = (latest_backup.version + 1) if latest_backup is not None else 1
                backups[tracking_path] = self._create_backup(path, tracking_path, version)
                changed = True

            if not backups:
                return None

            snapshot = FileSnapshot(
                snapshot_id=message_uuid or uuid.uuid4().hex,
                message_uuid=message_uuid,
                timestamp=_utc_now(),
                tracked_file_backups=backups,
            )
            self.snapshots.append(snapshot)
            self._trim_snapshots()
            self.snapshot_sequence += 1
            await self._record_transcript(
                snapshot,
                is_snapshot_update=False,
                task_id=task_id,
                parent_task_id=parent_task_id,
                agent_id=agent_id,
            )
            return snapshot if changed or backups else None

    async def revert_to_snapshot(
        self,
        filepath: str | Path,
        snapshot_id: str,
    ) -> RevertResult:
        path = self._resolve_path(filepath)
        tracking_path = self._tracking_path(path)
        snapshot = self._find_snapshot(snapshot_id)
        backup = self._backup_for_snapshot(snapshot, tracking_path)
        if backup is None:
            raise KeyError(f"No backup for {path} in snapshot {snapshot_id}")

        diff = await self.get_file_diff(path, snapshot.snapshot_id)
        if backup.backup_file_name is None:
            if path.exists():
                path.unlink()
                changed = True
            else:
                changed = False
            return RevertResult(
                filepath=str(path),
                snapshot_id=snapshot.snapshot_id,
                changed=changed,
                deleted=changed,
                insertions=diff.insertions,
                deletions=diff.deletions,
            )

        backup_path = self._backup_path(backup.backup_file_name)
        if not backup_path.exists():
            raise FileNotFoundError(f"Backup file not found: {backup_path}")
        changed = self._origin_changed(path, backup)
        if changed:
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(backup_path, path)
            if backup.mode is not None:
                os.chmod(path, backup.mode)
        return RevertResult(
            filepath=str(path),
            snapshot_id=snapshot.snapshot_id,
            changed=changed,
            backup_path=str(backup_path),
            insertions=diff.insertions,
            deletions=diff.deletions,
        )

    async def get_file_diff(
        self,
        filepath: str | Path,
        snapshot_id: str,
    ) -> FileDiff:
        path = self._resolve_path(filepath)
        tracking_path = self._tracking_path(path)
        snapshot = self._find_snapshot(snapshot_id)
        backup = self._backup_for_snapshot(snapshot, tracking_path)
        if backup is None:
            raise KeyError(f"No backup for {path} in snapshot {snapshot_id}")

        current_text = _read_text_or_empty(path)
        backup_path: Path | None = None
        backup_text = ""
        if backup.backup_file_name is not None:
            backup_path = self._backup_path(backup.backup_file_name)
            backup_text = _read_text_or_empty(backup_path)
        current_lines = current_text.splitlines(keepends=True)
        backup_lines = backup_text.splitlines(keepends=True)
        diff_lines = list(
            difflib.unified_diff(
                current_lines,
                backup_lines,
                fromfile=str(path),
                tofile=str(backup_path) if backup_path else "/dev/null",
            )
        )
        insertions, deletions = _line_change_stats(current_lines, backup_lines)
        changed = bool(insertions or deletions)
        if backup.backup_file_name is None and path.exists():
            changed = True
        return FileDiff(
            filepath=str(path),
            snapshot_id=snapshot.snapshot_id,
            diff="".join(diff_lines),
            insertions=insertions,
            deletions=deletions,
            changed=changed,
            backup_path=str(backup_path) if backup_path else None,
        )

    def list_snapshots(self, filepath: str | Path | None = None) -> list[FileSnapshot]:
        if filepath is None:
            return list(self.snapshots)
        tracking_path = self._tracking_path(self._resolve_path(filepath))
        return [
            snapshot
            for snapshot in self.snapshots
            if tracking_path in snapshot.tracked_file_backups
        ]

    def restore_state(self, snapshots: Sequence[Mapping[str, Any]]) -> None:
        restored: list[FileSnapshot] = []
        tracked: set[str] = set()
        for item in snapshots:
            raw = item.get("snapshot") if isinstance(item.get("snapshot"), Mapping) else item
            if not isinstance(raw, Mapping):
                continue
            snapshot = FileSnapshot.from_mapping(raw)
            restored.append(snapshot)
            tracked.update(snapshot.tracked_file_backups.keys())
        self.snapshots = restored[-MAX_SNAPSHOTS:]
        self.tracked_files = tracked
        self.snapshot_sequence = len(restored)

    def _current_snapshot(self, message_uuid: str | None) -> FileSnapshot | None:
        if not self.snapshots:
            return None
        latest = self.snapshots[-1]
        if message_uuid is not None and latest.message_uuid == message_uuid:
            return latest
        if message_uuid is None and latest.message_uuid is None:
            return latest
        return None

    def _find_snapshot(self, snapshot_id: str) -> FileSnapshot:
        for snapshot in reversed(self.snapshots):
            if snapshot.snapshot_id == snapshot_id or snapshot.message_uuid == snapshot_id:
                return snapshot
        raise KeyError(f"Snapshot not found: {snapshot_id}")

    def _backup_for_snapshot(
        self,
        snapshot: FileSnapshot,
        tracking_path: str,
    ) -> FileHistoryBackup | None:
        backup = snapshot.tracked_file_backups.get(tracking_path)
        if backup is not None:
            return backup
        for candidate in self.snapshots:
            first = candidate.tracked_file_backups.get(tracking_path)
            if first is not None and first.version == 1:
                return first
        return None

    def _create_backup(
        self,
        path: Path,
        tracking_path: str,
        version: int,
    ) -> FileHistoryBackup:
        if not path.exists():
            return FileHistoryBackup(
                backup_file_name=None,
                version=version,
                backup_time=_utc_now(),
            )
        stat_result = path.stat()
        digest = _sha256_file(path)
        backup_file_name = _backup_file_name(tracking_path, version)
        backup_path = self._backup_path(backup_file_name)
        while backup_path.exists() and _sha256_file(backup_path) != digest:
            version += 1
            backup_file_name = _backup_file_name(tracking_path, version)
            backup_path = self._backup_path(backup_file_name)

        self.backups_dir.mkdir(parents=True, exist_ok=True)
        if not backup_path.exists():
            shutil.copyfile(path, backup_path)
            os.chmod(backup_path, stat_result.st_mode)

        return FileHistoryBackup(
            backup_file_name=backup_file_name,
            version=version,
            backup_time=_utc_now(),
            size=stat_result.st_size,
            sha256=digest,
            mode=stat_result.st_mode,
        )

    def _origin_changed(self, path: Path, backup: FileHistoryBackup) -> bool:
        if backup.backup_file_name is None:
            return path.exists()
        backup_path = self._backup_path(backup.backup_file_name)
        if not path.exists() or not backup_path.exists():
            return True
        try:
            original_stat = path.stat()
            backup_stat = backup_path.stat()
        except OSError:
            return True
        if original_stat.st_mode != backup_stat.st_mode:
            return True
        if original_stat.st_size != backup_stat.st_size:
            return True
        if original_stat.st_mtime_ns < backup_stat.st_mtime_ns:
            return False
        return _sha256_file(path) != _sha256_file(backup_path)

    def _resolve_path(self, filepath: str | Path) -> Path:
        path = Path(filepath).expanduser()
        if not path.is_absolute():
            path = Path(self.cwd) / path
        return path.resolve(strict=False)

    def _tracking_path(self, path: Path) -> str:
        try:
            return str(path.relative_to(Path(self.cwd)))
        except ValueError:
            return str(path)

    def _expand_tracking_path(self, tracking_path: str) -> Path:
        path = Path(tracking_path)
        if path.is_absolute():
            return path
        return (Path(self.cwd) / path).resolve(strict=False)

    def _backup_path(self, backup_file_name: str) -> Path:
        return self.backups_dir / backup_file_name

    async def _record_transcript(
        self,
        snapshot: FileSnapshot,
        *,
        is_snapshot_update: bool,
        task_id: str | None = None,
        parent_task_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        if self.session_storage is None:
            return
        try:
            await self.session_storage.record_file_history_snapshot(
                snapshot.message_uuid or snapshot.snapshot_id,
                snapshot.to_dict(),
                is_snapshot_update=is_snapshot_update,
                task_id=task_id,
                parent_task_id=parent_task_id,
                agent_id=agent_id,
            )
        except Exception:
            logger.debug("Failed to record file history snapshot", exc_info=True)

    def _trim_snapshots(self) -> None:
        if len(self.snapshots) <= MAX_SNAPSHOTS:
            return
        self.snapshots = self.snapshots[-MAX_SNAPSHOTS:]
        tracked: set[str] = set()
        for snapshot in self.snapshots:
            tracked.update(snapshot.tracked_file_backups.keys())
        self.tracked_files = tracked


async def record_snapshot(
    filepath: str | Path,
    *,
    context: Any | None = None,
    message_uuid: str | None = None,
) -> FileSnapshot | None:
    history = _history_from_context(context)
    if history is None:
        logger.debug("File history unavailable; skipping snapshot for %s", filepath)
        return None
    resolved_message_uuid = message_uuid or _infer_message_uuid(context)
    return await history.record_snapshot(
        filepath,
        message_uuid=resolved_message_uuid,
        task_id=_context_value(context, "task_id"),
        parent_task_id=_context_value(context, "parent_task_id"),
        agent_id=_context_value(context, "agent_id"),
    )


async def revert_to_snapshot(
    filepath: str | Path,
    snapshot_id: str,
    *,
    context: Any | None = None,
) -> RevertResult:
    history = _history_from_context(context)
    if history is None:
        raise RuntimeError("File history unavailable")
    result = await history.revert_to_snapshot(filepath, snapshot_id)
    read_state = getattr(context, "read_file_state", None)
    if isinstance(read_state, dict):
        read_state.pop(str(Path(result.filepath).resolve(strict=False)), None)
    return result


async def get_file_diff(
    filepath: str | Path,
    snapshot_id: str,
    *,
    context: Any | None = None,
) -> FileDiff:
    history = _history_from_context(context)
    if history is None:
        raise RuntimeError("File history unavailable")
    return await history.get_file_diff(filepath, snapshot_id)


def list_snapshots(
    filepath: str | Path | None = None,
    *,
    context: Any | None = None,
) -> list[FileSnapshot]:
    history = _history_from_context(context)
    if history is None:
        return []
    return history.list_snapshots(filepath)


async def copy_file_history_for_resume(
    source_storage: "SessionStorage",
    target_storage: "SessionStorage",
    snapshots: Sequence[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Copy immutable backup files and snapshot entries into a forked session."""

    raw_snapshots = list(snapshots) if snapshots is not None else list(
        source_storage.load().file_history_snapshots
    )
    if not raw_snapshots:
        return []

    source_history = FileHistory(session_storage=source_storage)
    target_history = FileHistory(session_storage=target_storage)
    target_history.backups_dir.mkdir(parents=True, exist_ok=True)

    copied_entries: list[dict[str, Any]] = []
    seen_backup_files: set[str] = set()
    for item in raw_snapshots:
        raw_snapshot = item.get("snapshot") if isinstance(item.get("snapshot"), Mapping) else item
        if not isinstance(raw_snapshot, Mapping):
            continue
        snapshot = FileSnapshot.from_mapping(raw_snapshot)
        for backup in snapshot.tracked_file_backups.values():
            if backup.backup_file_name is None:
                continue
            if backup.backup_file_name in seen_backup_files:
                continue
            seen_backup_files.add(backup.backup_file_name)
            source_path = source_history.backups_dir / backup.backup_file_name
            target_path = target_history.backups_dir / backup.backup_file_name
            if not source_path.exists() or target_path.exists():
                continue
            try:
                os.link(source_path, target_path)
            except OSError:
                shutil.copyfile(source_path, target_path)
                if backup.mode is not None:
                    os.chmod(target_path, backup.mode)

        is_snapshot_update = bool(item.get("is_snapshot_update", False))
        original_task_id = _context_value(item, "original_task_id") or _context_value(
            item,
            "task_id",
        )
        original_parent_task_id = _context_value(
            item,
            "original_parent_task_id",
        ) or _context_value(item, "parent_task_id")
        original_agent_id = _context_value(item, "original_agent_id") or _context_value(
            item,
            "agent_id",
        )
        original_session_id = _context_value(
            item,
            "original_session_id",
        ) or _context_value(source_storage, "session_id")
        await target_storage.record_file_history_snapshot(
            snapshot.message_uuid or snapshot.snapshot_id,
            snapshot.to_dict(),
            is_snapshot_update=is_snapshot_update,
            original_session_id=original_session_id,
            original_task_id=original_task_id,
            original_parent_task_id=original_parent_task_id,
            original_agent_id=original_agent_id,
        )
        copied_entries.append(
            {
                "message_uuid": snapshot.message_uuid or snapshot.snapshot_id,
                "snapshot": snapshot.to_dict(),
                "is_snapshot_update": is_snapshot_update,
                "original_session_id": original_session_id,
                "original_task_id": original_task_id,
                "original_parent_task_id": original_parent_task_id,
                "original_agent_id": original_agent_id,
            }
        )

    return copied_entries


def _history_from_context(context: Any | None) -> FileHistory | None:
    if context is None:
        return None
    history = getattr(context, "file_history", None)
    if isinstance(history, FileHistory):
        return history
    storage = getattr(context, "session_storage", None)
    if storage is None:
        return None
    history = FileHistory(session_storage=storage, cwd=getattr(context, "cwd", None))
    try:
        loaded = storage.load()
        history.restore_state(getattr(loaded, "file_history_snapshots", []) or [])
    except Exception:
        logger.debug("Failed to restore file history state from session", exc_info=True)
    try:
        context.file_history = history
    except Exception:
        pass
    return history


def _infer_message_uuid(context: Any | None) -> str | None:
    messages = getattr(context, "messages", None)
    if not isinstance(messages, Sequence):
        return None
    for message in reversed(messages):
        if isinstance(message, Mapping):
            msg_uuid = get_message_uuid(message)
            if msg_uuid:
                return msg_uuid
    return None


def _context_value(context: Any | None, key: str) -> str | None:
    if context is None:
        return None
    if isinstance(context, Mapping):
        raw = context.get(key)
    else:
        raw = getattr(context, key, None)
    if raw is None:
        return None
    text = str(raw)
    return text if text else None


def _backup_file_name(tracking_path: str, version: int) -> str:
    digest = hashlib.sha256(tracking_path.encode("utf-8")).hexdigest()[:16]
    return f"{digest}@v{version}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_text_or_empty(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _line_change_stats(
    current_lines: list[str],
    backup_lines: list[str],
) -> tuple[int, int]:
    matcher = difflib.SequenceMatcher(a=current_lines, b=backup_lines)
    insertions = 0
    deletions = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag in {"replace", "delete"}:
            deletions += i2 - i1
        if tag in {"replace", "insert"}:
            insertions += j2 - j1
    return insertions, deletions


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _none_or_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _none_or_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "BACKUPS_DIRNAME",
    "FILE_HISTORY_DIRNAME",
    "MAX_SNAPSHOTS",
    "FileDiff",
    "FileHistory",
    "FileHistoryBackup",
    "FileSnapshot",
    "RevertResult",
    "copy_file_history_for_resume",
    "get_file_diff",
    "list_snapshots",
    "record_snapshot",
    "revert_to_snapshot",
]
