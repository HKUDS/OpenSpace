"""Local trust gate for skill uploads."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from openspace.cloud.local_mapping import read_local_skill_id


class SkillUploadTrustError(RuntimeError):
    """Raised before cloud access when local skill trust is insufficient."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        skill_id: str | None = None,
        trust_state: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.skill_id = skill_id
        self.trust_state = trust_state

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": "error",
            "code": self.code,
            "message": str(self),
            "required_trust_state": "trusted",
        }
        if self.skill_id:
            payload["local_skill_id"] = self.skill_id
        if self.trust_state:
            payload["actual_trust_state"] = self.trust_state
        return payload


def require_trusted_skill_for_upload(
    skill_dir: str | Path,
    *,
    skill_store: Any | None,
) -> Any:
    """Return the matching trusted record or fail before cloud access."""

    root = Path(skill_dir).expanduser().resolve()
    local_skill_id = read_local_skill_id(root)
    if skill_store is None:
        raise SkillUploadTrustError(
            "SKILL_TRUST_UNKNOWN",
            "SkillStore is unavailable; upload requires a trusted local skill record.",
            skill_id=local_skill_id,
        )

    record = None
    if local_skill_id:
        load_record = getattr(skill_store, "load_record", None)
        if callable(load_record):
            record = load_record(local_skill_id)
        if record is None:
            raise SkillUploadTrustError(
                "SKILL_TRUST_UNKNOWN",
                "The local skill ID is not registered in the active SkillStore.",
                skill_id=local_skill_id,
            )
    else:
        load_record_by_path = getattr(skill_store, "load_record_by_path", None)
        if callable(load_record_by_path):
            record = load_record_by_path(str(root))
        if record is None:
            raise SkillUploadTrustError(
                "SKILL_TRUST_UNKNOWN",
                "The skill directory is not registered in the active SkillStore.",
            )

    record_skill_id = str(getattr(record, "skill_id", None) or local_skill_id or "")
    record_root = _record_skill_root(record)
    if record_root is None or record_root != root:
        raise SkillUploadTrustError(
            "SKILL_RECORD_PATH_MISMATCH",
            "The trusted SkillStore record does not match the requested skill directory.",
            skill_id=record_skill_id or None,
            trust_state=_trust_state(record),
        )

    trust_state = _trust_state(record)
    if trust_state != "trusted":
        raise SkillUploadTrustError(
            "SKILL_NOT_TRUSTED",
            "Skill upload is blocked until the local skill reaches trusted state.",
            skill_id=record_skill_id or None,
            trust_state=trust_state or "unknown",
        )
    return record


def require_trusted_skill_for_upload_db(
    skill_dir: str | Path,
    *,
    db_path: str | Path,
) -> Any:
    """Apply the trust gate using a read-only standalone SkillStore DB."""

    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        return require_trusted_skill_for_upload(skill_dir, skill_store=None)

    connection = sqlite3.connect(
        f"{path.as_uri()}?mode=ro",
        uri=True,
        timeout=30.0,
    )
    connection.row_factory = sqlite3.Row
    try:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(skill_records)")
        }
        if not columns:
            return require_trusted_skill_for_upload(skill_dir, skill_store=None)
        trust_projection = (
            "trust_state" if "trust_state" in columns else "'trusted' AS trust_state"
        )
        adapter = _ReadOnlyUploadSkillStore(connection, trust_projection)
        return require_trusted_skill_for_upload(skill_dir, skill_store=adapter)
    finally:
        connection.close()


def resolve_upload_skill_store_db(
    skill_dir: str | Path,
    *,
    explicit_db_path: str | Path | None = None,
    cwd: str | Path | None = None,
) -> Path:
    """Resolve the local SkillStore used by the standalone upload CLI."""

    explicit = explicit_db_path or os.environ.get("OPENSPACE_SKILL_STORE_DB_PATH")
    if explicit:
        return Path(explicit).expanduser().resolve()

    storage_root = os.environ.get("OPENSPACE_EVOLUTION_STORAGE_ROOT")
    if storage_root:
        return (
            Path(storage_root).expanduser().resolve()
            / ".openspace"
            / "openspace.db"
        )

    root = Path(skill_dir).expanduser().resolve()
    search_roots = [root, *root.parents]
    current = Path(cwd or Path.cwd()).expanduser().resolve()
    search_roots.extend([current, *current.parents])
    seen: set[Path] = set()
    for parent in search_roots:
        if parent in seen:
            continue
        seen.add(parent)
        candidate = parent / ".openspace" / "openspace.db"
        if candidate.is_file():
            return candidate
    return current / ".openspace" / "openspace.db"


def _record_skill_root(record: Any) -> Path | None:
    value = getattr(record, "path", None)
    if not value:
        return None
    path = Path(value).expanduser().resolve()
    if path.name == "SKILL.md" or path.suffix:
        return path.parent
    return path


def _trust_state(record: Any) -> str:
    value = getattr(record, "trust_state", None)
    value = getattr(value, "value", value)
    return str(value or "").strip().lower()


class _ReadOnlyUploadSkillStore:
    def __init__(self, connection: sqlite3.Connection, trust_projection: str) -> None:
        self._connection = connection
        self._trust_projection = trust_projection

    def load_record(self, skill_id: str) -> Any | None:
        row = self._connection.execute(
            f"SELECT skill_id, path, {self._trust_projection} "
            "FROM skill_records WHERE skill_id=? LIMIT 1",
            (skill_id,),
        ).fetchone()
        return _upload_record(row)

    def load_record_by_path(self, skill_dir: str) -> Any | None:
        normalized = str(Path(skill_dir).expanduser().resolve()).rstrip("/")
        row = self._connection.execute(
            f"SELECT skill_id, path, {self._trust_projection} "
            "FROM skill_records WHERE path LIKE ? AND is_active=1 "
            "ORDER BY last_updated DESC LIMIT 1",
            (f"{normalized}%",),
        ).fetchone()
        return _upload_record(row)


def _upload_record(row: sqlite3.Row | None) -> Any | None:
    if row is None:
        return None
    return SimpleNamespace(
        skill_id=str(row["skill_id"] or ""),
        path=str(row["path"] or ""),
        trust_state=str(row["trust_state"] or ""),
    )
