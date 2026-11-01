"""Local persistence for cloud skill bindings and package metadata."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Iterable, Mapping

from openspace.config.constants import PROJECT_ROOT

SKILL_ID_FILENAME = ".skill_id"
CLOUD_SKILL_SIDECAR_FILENAME = ".cloud_skill.json"
CLOUD_SKILL_INFO_FILENAME = CLOUD_SKILL_SIDECAR_FILENAME
UPLOAD_META_FILENAME = ".upload_meta.json"

_HASH_EXCLUDED_FILENAMES = frozenset({
    SKILL_ID_FILENAME,
    CLOUD_SKILL_SIDECAR_FILENAME,
    UPLOAD_META_FILENAME,
})

_SYNC_STATES = frozenset({
    "clean",
    "dirty",
    "conflict",
    "uploaded",
    "metadata_only",
})
_CATEGORIES = frozenset({"workflow", "tool_guide", "reference"})
_REVIEW_STATES = frozenset({"auto", "needs_review", "reviewed", "conflict"})

_DDL = """
CREATE TABLE IF NOT EXISTS skill_cloud_bindings (
    local_skill_id TEXT PRIMARY KEY,
    cloud_skill_id TEXT UNIQUE,
    local_path TEXT NOT NULL DEFAULT '',
    package_id_at_pull TEXT,
    package_path_at_pull TEXT,
    package_snapshot_version_at_pull TEXT,
    current_package_id TEXT,
    current_package_path TEXT,
    source_cloud_skill_id TEXT,
    manifest_hash TEXT,
    local_content_hash TEXT,
    sync_state TEXT NOT NULL DEFAULT 'clean'
        CHECK(sync_state IN ('clean', 'dirty', 'conflict', 'uploaded', 'metadata_only')),
    last_pulled_at TEXT,
    last_pushed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_skill_cloud_bindings_cloud
    ON skill_cloud_bindings(cloud_skill_id);

CREATE TABLE IF NOT EXISTS skill_local_classifications (
    local_skill_id TEXT PRIMARY KEY,
    category TEXT NOT NULL
        CHECK(category IN ('workflow', 'tool_guide', 'reference')),
    local_category_path TEXT NOT NULL DEFAULT '',
    classification_confidence REAL,
    classification_rationale TEXT NOT NULL DEFAULT '',
    review_state TEXT NOT NULL DEFAULT 'auto'
        CHECK(review_state IN ('auto', 'needs_review', 'reviewed', 'conflict')),
    evidence_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS package_cache (
    package_id TEXT PRIMARY KEY,
    package_path TEXT NOT NULL DEFAULT '',
    projection_hash TEXT,
    serving_epoch TEXT,
    source_epoch TEXT,
    last_pulled_at TEXT
);

"""


class CloudLocalMappingError(RuntimeError):
    """Raised when local cloud mapping state cannot satisfy a request."""


class UnboundLocalSkillError(CloudLocalMappingError):
    """Raised when a local parent skill has no cloud binding."""

    code = "PARENT_CLOUD_BINDING_REQUIRED"

    def __init__(self, missing_parent_local_skill_ids: Iterable[str]):
        self.missing_parent_local_skill_ids = list(missing_parent_local_skill_ids)
        super().__init__(
            "v2 fix/derive upload requires every parent local skill to have a "
            "cloud_skill_id: "
            + ", ".join(self.missing_parent_local_skill_ids)
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": "error",
            "code": self.code,
            "message": str(self),
            "missing_parent_local_skill_ids": self.missing_parent_local_skill_ids,
            "policy_decision_required": True,
            "allowed_resolutions": [
                "upload_parent_first",
                "upload_as_imported_or_capture",
            ],
        }


@dataclass(frozen=True)
class SkillCloudBinding:
    local_skill_id: str
    cloud_skill_id: str | None = None
    local_path: str = ""
    package_id_at_pull: str | None = None
    package_path_at_pull: str | None = None
    package_snapshot_version_at_pull: str | None = None
    current_package_id: str | None = None
    current_package_path: str | None = None
    source_cloud_skill_id: str | None = None
    manifest_hash: str | None = None
    local_content_hash: str | None = None
    sync_state: str = "clean"
    last_pulled_at: str | None = None
    last_pushed_at: str | None = None


CloudSkillBinding = SkillCloudBinding


@dataclass(frozen=True)
class SkillLocalClassification:
    local_skill_id: str
    category: str
    local_category_path: str = ""
    classification_confidence: float | None = None
    classification_rationale: str = ""
    review_state: str = "auto"
    evidence: Any = None
    updated_at: str = ""


@dataclass(frozen=True)
class PackageCacheEntry:
    package_id: str
    package_path: str = ""
    projection_hash: str | None = None
    serving_epoch: str | None = None
    source_epoch: str | None = None
    last_pulled_at: str | None = None


@dataclass(frozen=True)
class PackagePathIndexEntry:
    snapshot_version: str
    package_path: str
    package_id: str
    normalized_package_path: str = ""
    root_sub_domain_package_id: str | None = None
    parent_package_id: str | None = None
    package_kind: str = ""
    can_select_as_upload_target: bool = False
    can_create_child_regular_package: bool | None = None
    select_disabled_reason: str | None = None
    fetched_from: str = ""
    fetched_at: str | None = None


class CloudLocalMappingStore:
    """SQLite-backed local mapping store for cloud package/skill metadata."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            db_dir = PROJECT_ROOT / ".openspace"
            db_dir.mkdir(parents=True, exist_ok=True)
            db_path = db_dir / "openspace.db"
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._mu = threading.Lock()
        self._closed = False
        self._conn = self._make_connection(read_only=False)
        self._init_db()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.close()
        except Exception:
            pass

    def __enter__(self) -> "CloudLocalMappingStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _make_connection(self, *, read_only: bool) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self._db_path),
            timeout=30.0,
            check_same_thread=False,
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        if read_only:
            conn.execute("PRAGMA query_only=ON")
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _reader(self) -> Generator[sqlite3.Connection, None, None]:
        self._ensure_open()
        conn = self._make_connection(read_only=True)
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._mu:
            self._conn.executescript(_DDL)
            self._ensure_package_path_index_v2_locked()
            self._conn.commit()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("CloudLocalMappingStore is closed")

    def _ensure_package_path_index_v2_locked(self) -> None:
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(package_path_index)").fetchall()
        }
        required = {
            "normalized_package_path",
            "root_sub_domain_package_id",
            "parent_package_id",
            "can_create_child_regular_package",
            "select_disabled_reason",
            "fetched_from",
            "fetched_at",
        }
        if required.issubset(columns):
            return
        self._conn.execute("DROP TABLE IF EXISTS package_path_index")
        self._conn.execute("DROP INDEX IF EXISTS idx_package_path_index_package_id")
        self._conn.execute("DROP INDEX IF EXISTS idx_package_path_index_selectable")
        self._conn.executescript(
            """
            CREATE TABLE package_path_index (
                snapshot_version TEXT NOT NULL,
                normalized_package_path TEXT NOT NULL,
                package_id TEXT NOT NULL,
                package_path TEXT NOT NULL,
                root_sub_domain_package_id TEXT,
                parent_package_id TEXT,
                package_kind TEXT NOT NULL DEFAULT '',
                can_select_as_upload_target INTEGER NOT NULL DEFAULT 0,
                can_create_child_regular_package INTEGER,
                select_disabled_reason TEXT,
                fetched_from TEXT NOT NULL DEFAULT '',
                fetched_at TEXT,
                PRIMARY KEY (snapshot_version, normalized_package_path, package_id)
            );
            CREATE INDEX IF NOT EXISTS idx_package_path_index_package_id_v2
                ON package_path_index(package_id);
            CREATE INDEX IF NOT EXISTS idx_package_path_index_selectable_v2
                ON package_path_index(snapshot_version, can_select_as_upload_target);
            CREATE INDEX IF NOT EXISTS idx_package_path_index_path_v2
                ON package_path_index(snapshot_version, normalized_package_path);
            """
        )

    def upsert_skill_cloud_binding(
        self,
        *,
        local_skill_id: str,
        cloud_skill_id: str | None = None,
        local_path: str = "",
        package_id_at_pull: str | None = None,
        package_path_at_pull: str | None = None,
        package_snapshot_version_at_pull: str | None = None,
        current_package_id: str | None = None,
        current_package_path: str | None = None,
        source_cloud_skill_id: str | None = None,
        manifest_hash: str | None = None,
        local_content_hash: str | None = None,
        sync_state: str = "clean",
        last_pulled_at: str | None = None,
        last_pushed_at: str | None = None,
    ) -> SkillCloudBinding:
        """Insert or update one local skill to cloud skill binding."""

        self._ensure_open()
        local_skill_id = _required_text(local_skill_id, "local_skill_id")
        cloud_skill_id = _optional_text(cloud_skill_id)
        if sync_state not in _SYNC_STATES:
            raise ValueError(f"sync_state must be one of {sorted(_SYNC_STATES)}")
        with self._mu:
            self._conn.execute(
                """
                INSERT INTO skill_cloud_bindings (
                    local_skill_id,
                    cloud_skill_id,
                    local_path,
                    package_id_at_pull,
                    package_path_at_pull,
                    package_snapshot_version_at_pull,
                    current_package_id,
                    current_package_path,
                    source_cloud_skill_id,
                    manifest_hash,
                    local_content_hash,
                    sync_state,
                    last_pulled_at,
                    last_pushed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(local_skill_id) DO UPDATE SET
                    cloud_skill_id=excluded.cloud_skill_id,
                    local_path=excluded.local_path,
                    package_id_at_pull=excluded.package_id_at_pull,
                    package_path_at_pull=excluded.package_path_at_pull,
                    package_snapshot_version_at_pull=excluded.package_snapshot_version_at_pull,
                    current_package_id=excluded.current_package_id,
                    current_package_path=excluded.current_package_path,
                    source_cloud_skill_id=excluded.source_cloud_skill_id,
                    manifest_hash=excluded.manifest_hash,
                    local_content_hash=excluded.local_content_hash,
                    sync_state=excluded.sync_state,
                    last_pulled_at=excluded.last_pulled_at,
                    last_pushed_at=excluded.last_pushed_at
                """,
                (
                    local_skill_id,
                    cloud_skill_id,
                    str(local_path or ""),
                    _optional_text(package_id_at_pull),
                    _optional_text(package_path_at_pull),
                    _optional_text(package_snapshot_version_at_pull),
                    _optional_text(current_package_id),
                    _optional_text(current_package_path),
                    _optional_text(source_cloud_skill_id),
                    _optional_text(manifest_hash),
                    _optional_text(local_content_hash),
                    sync_state,
                    _optional_text(last_pulled_at),
                    _optional_text(last_pushed_at),
                ),
            )
            self._conn.commit()
        binding = self.get_skill_cloud_binding_by_local(local_skill_id)
        if binding is None:
            raise CloudLocalMappingError(
                f"Failed to load saved binding for local skill {local_skill_id!r}"
            )
        return binding

    def upsert_binding(self, binding: SkillCloudBinding) -> SkillCloudBinding:
        return self.upsert_skill_cloud_binding(
            local_skill_id=binding.local_skill_id,
            cloud_skill_id=binding.cloud_skill_id,
            local_path=binding.local_path,
            package_id_at_pull=binding.package_id_at_pull,
            package_path_at_pull=binding.package_path_at_pull,
            package_snapshot_version_at_pull=binding.package_snapshot_version_at_pull,
            current_package_id=binding.current_package_id,
            current_package_path=binding.current_package_path,
            source_cloud_skill_id=binding.source_cloud_skill_id,
            manifest_hash=binding.manifest_hash,
            local_content_hash=binding.local_content_hash,
            sync_state=binding.sync_state,
            last_pulled_at=binding.last_pulled_at,
            last_pushed_at=binding.last_pushed_at,
        )

    def get_skill_cloud_binding_by_local(
        self,
        local_skill_id: str,
    ) -> SkillCloudBinding | None:
        """Return a cloud binding by local skill id."""

        with self._reader() as conn:
            row = conn.execute(
                "SELECT * FROM skill_cloud_bindings WHERE local_skill_id=?",
                (local_skill_id,),
            ).fetchone()
        return _row_to_skill_cloud_binding(row) if row else None

    def get_binding_by_local(self, local_skill_id: str) -> SkillCloudBinding | None:
        return self.get_skill_cloud_binding_by_local(local_skill_id)

    def get_skill_cloud_binding_by_cloud(
        self,
        cloud_skill_id: str,
    ) -> SkillCloudBinding | None:
        """Return the current local binding for a cloud skill id."""

        cloud_skill_id = _required_text(cloud_skill_id, "cloud_skill_id")
        with self._reader() as conn:
            row = conn.execute(
                "SELECT * FROM skill_cloud_bindings WHERE cloud_skill_id=?",
                (cloud_skill_id,),
            ).fetchone()
        return _row_to_skill_cloud_binding(row) if row else None

    def get_binding_by_cloud(self, cloud_skill_id: str) -> SkillCloudBinding | None:
        return self.get_skill_cloud_binding_by_cloud(cloud_skill_id)

    def resolve_parent_local_ids_to_cloud_ids(
        self,
        parent_local_skill_ids: Iterable[str],
    ) -> list[str]:
        """Resolve local parent skill ids to cloud ids for fix/derive upload."""

        cloud_ids: list[str] = []
        missing: list[str] = []
        for local_skill_id in parent_local_skill_ids:
            local_id = _required_text(local_skill_id, "parent_local_skill_id")
            binding = self.get_skill_cloud_binding_by_local(local_id)
            if binding is None or not binding.cloud_skill_id:
                missing.append(local_id)
                continue
            cloud_ids.append(binding.cloud_skill_id)
        if missing:
            raise UnboundLocalSkillError(missing)
        return cloud_ids

    def resolve_parent_cloud_skill_ids(
        self,
        parent_local_skill_ids: Iterable[str],
    ) -> list[str]:
        return self.resolve_parent_local_ids_to_cloud_ids(parent_local_skill_ids)

    def upsert_skill_local_classification(
        self,
        *,
        local_skill_id: str,
        category: str,
        updated_at: str,
        local_category_path: str = "",
        classification_confidence: float | None = None,
        classification_rationale: str = "",
        review_state: str = "auto",
        evidence: Any = None,
    ) -> SkillLocalClassification:
        """Insert or update local package/category classification metadata."""

        self._ensure_open()
        local_skill_id = _required_text(local_skill_id, "local_skill_id")
        if category not in _CATEGORIES:
            raise ValueError(f"category must be one of {sorted(_CATEGORIES)}")
        if review_state not in _REVIEW_STATES:
            raise ValueError(f"review_state must be one of {sorted(_REVIEW_STATES)}")
        updated_at = _required_text(updated_at, "updated_at")
        evidence_json = _dump_json(evidence if evidence is not None else {})
        with self._mu:
            self._conn.execute(
                """
                INSERT INTO skill_local_classifications (
                    local_skill_id,
                    category,
                    local_category_path,
                    classification_confidence,
                    classification_rationale,
                    review_state,
                    evidence_json,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(local_skill_id) DO UPDATE SET
                    category=excluded.category,
                    local_category_path=excluded.local_category_path,
                    classification_confidence=excluded.classification_confidence,
                    classification_rationale=excluded.classification_rationale,
                    review_state=excluded.review_state,
                    evidence_json=excluded.evidence_json,
                    updated_at=excluded.updated_at
                """,
                (
                    local_skill_id,
                    category,
                    str(local_category_path or ""),
                    classification_confidence,
                    str(classification_rationale or ""),
                    review_state,
                    evidence_json,
                    updated_at,
                ),
            )
            self._conn.commit()
        classification = self.get_skill_local_classification(local_skill_id)
        if classification is None:
            raise CloudLocalMappingError(
                f"Failed to load saved classification for {local_skill_id!r}"
            )
        return classification

    def get_skill_local_classification(
        self,
        local_skill_id: str,
    ) -> SkillLocalClassification | None:
        with self._reader() as conn:
            row = conn.execute(
                "SELECT * FROM skill_local_classifications WHERE local_skill_id=?",
                (local_skill_id,),
            ).fetchone()
        return _row_to_skill_local_classification(row) if row else None

    def list_skill_local_classifications(
        self,
        *,
        category: str | None = None,
        limit: int | None = None,
    ) -> list[SkillLocalClassification]:
        clauses: list[str] = []
        params: list[Any] = []
        if category:
            if category not in _CATEGORIES:
                raise ValueError(f"category must be one of {sorted(_CATEGORIES)}")
            clauses.append("category=?")
            params.append(category)
        limit_clause = ""
        if limit is not None:
            limit_clause = " LIMIT ?"
            params.append(max(int(limit), 1))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._reader() as conn:
            rows = conn.execute(
                "SELECT * FROM skill_local_classifications"
                f"{where} ORDER BY category, local_category_path, local_skill_id"
                f"{limit_clause}",
                params,
            ).fetchall()
        return [_row_to_skill_local_classification(row) for row in rows]

    def upsert_package_cache(
        self,
        *,
        package_id: str,
        package_path: str = "",
        projection_hash: str | None = None,
        serving_epoch: str | None = None,
        source_epoch: str | None = None,
        last_pulled_at: str | None = None,
    ) -> PackageCacheEntry:
        """Insert or update one pulled package cache record."""

        self._ensure_open()
        package_id = _required_text(package_id, "package_id")
        with self._mu:
            self._conn.execute(
                """
                INSERT INTO package_cache (
                    package_id,
                    package_path,
                    projection_hash,
                    serving_epoch,
                    source_epoch,
                    last_pulled_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(package_id) DO UPDATE SET
                    package_path=excluded.package_path,
                    projection_hash=excluded.projection_hash,
                    serving_epoch=excluded.serving_epoch,
                    source_epoch=excluded.source_epoch,
                    last_pulled_at=excluded.last_pulled_at
                """,
                (
                    package_id,
                    str(package_path or ""),
                    _optional_text(projection_hash),
                    _optional_text(serving_epoch),
                    _optional_text(source_epoch),
                    _optional_text(last_pulled_at),
                ),
            )
            self._conn.commit()
        package = self.get_package_cache(package_id)
        if package is None:
            raise CloudLocalMappingError(
                f"Failed to load saved package cache for {package_id!r}"
            )
        return package

    def get_package_cache(self, package_id: str) -> PackageCacheEntry | None:
        with self._reader() as conn:
            row = conn.execute(
                "SELECT * FROM package_cache WHERE package_id=?",
                (package_id,),
            ).fetchone()
        return _row_to_package_cache_entry(row) if row else None

    def upsert_package_path_index_entry(
        self,
        *,
        snapshot_version: str,
        package_path: str,
        package_id: str,
        root_sub_domain_package_id: str | None = None,
        parent_package_id: str | None = None,
        package_kind: str = "",
        can_select_as_upload_target: bool = False,
        can_create_child_regular_package: bool | None = None,
        select_disabled_reason: str | None = None,
        fetched_from: str = "",
        fetched_at: str | None = None,
    ) -> PackagePathIndexEntry:
        """Insert or update one package path index row."""

        self._ensure_open()
        snapshot_version = _required_text(snapshot_version, "snapshot_version")
        package_path = _required_text(package_path, "package_path")
        package_id = _required_text(package_id, "package_id")
        normalized_package_path = normalize_package_path_key(package_path)
        with self._mu:
            self._conn.execute(
                """
                INSERT INTO package_path_index (
                    snapshot_version,
                    normalized_package_path,
                    package_id,
                    package_path,
                    root_sub_domain_package_id,
                    parent_package_id,
                    package_kind,
                    can_select_as_upload_target,
                    can_create_child_regular_package,
                    select_disabled_reason,
                    fetched_from,
                    fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(snapshot_version, normalized_package_path, package_id) DO UPDATE SET
                    package_path=excluded.package_path,
                    root_sub_domain_package_id=excluded.root_sub_domain_package_id,
                    parent_package_id=excluded.parent_package_id,
                    package_kind=excluded.package_kind,
                    can_select_as_upload_target=excluded.can_select_as_upload_target,
                    can_create_child_regular_package=excluded.can_create_child_regular_package,
                    select_disabled_reason=excluded.select_disabled_reason,
                    fetched_from=excluded.fetched_from,
                    fetched_at=excluded.fetched_at
                """,
                (
                    snapshot_version,
                    normalized_package_path,
                    package_id,
                    package_path,
                    _optional_text(root_sub_domain_package_id),
                    _optional_text(parent_package_id),
                    str(package_kind or ""),
                    1 if can_select_as_upload_target else 0,
                    _optional_bool(can_create_child_regular_package),
                    _optional_text(select_disabled_reason),
                    str(fetched_from or ""),
                    _optional_text(fetched_at),
                ),
            )
            self._conn.commit()
        entry = self.get_package_path_index_entry(
            snapshot_version,
            package_path,
            package_id=package_id,
        )
        if entry is None:
            raise CloudLocalMappingError(
                f"Failed to load saved package path index for {package_path!r}"
            )
        return entry

    def get_package_path_index_entry(
        self,
        snapshot_version: str,
        package_path: str,
        *,
        package_id: str | None = None,
    ) -> PackagePathIndexEntry | None:
        entries = self.list_package_path_index_by_path(
            snapshot_version=snapshot_version,
            package_path=package_path,
            package_id=package_id,
        )
        if len(entries) > 1:
            raise CloudLocalMappingError(
                f"Package path {package_path!r} is ambiguous in snapshot {snapshot_version!r}"
            )
        return entries[0] if entries else None

    def list_package_path_index_by_path(
        self,
        *,
        snapshot_version: str,
        package_path: str,
        package_id: str | None = None,
    ) -> list[PackagePathIndexEntry]:
        snapshot_version = _required_text(snapshot_version, "snapshot_version")
        normalized_package_path = normalize_package_path_key(package_path)
        clauses = ["snapshot_version=?", "normalized_package_path=?"]
        params: list[Any] = [snapshot_version, normalized_package_path]
        if package_id is not None:
            clauses.append("package_id=?")
            params.append(_required_text(package_id, "package_id"))
        with self._reader() as conn:
            rows = conn.execute(
                "SELECT * FROM package_path_index "
                f"WHERE {' AND '.join(clauses)} "
                "ORDER BY package_path, package_id",
                params,
            ).fetchall()
        return [_row_to_package_path_index_entry(row) for row in rows]

    def list_package_path_index(
        self,
        *,
        snapshot_version: str | None = None,
        selectable_only: bool = False,
    ) -> list[PackagePathIndexEntry]:
        clauses: list[str] = []
        params: list[Any] = []
        if snapshot_version is not None:
            clauses.append("snapshot_version=?")
            params.append(snapshot_version)
        if selectable_only:
            clauses.append("can_select_as_upload_target=1")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._reader() as conn:
            rows = conn.execute(
                "SELECT * FROM package_path_index"
                f"{where} ORDER BY snapshot_version, package_path",
                params,
            ).fetchall()
        return [_row_to_package_path_index_entry(row) for row in rows]


def read_cloud_skill_sidecar(skill_dir: str | Path) -> dict[str, Any] | None:
    """Read ``.cloud_skill.json`` from a skill directory if it exists."""

    sidecar_path = Path(skill_dir) / CLOUD_SKILL_SIDECAR_FILENAME
    if not sidecar_path.exists():
        return None
    try:
        payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid cloud skill sidecar JSON: {sidecar_path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Cloud skill sidecar must contain a JSON object: {sidecar_path}")
    return payload


def write_cloud_skill_sidecar(
    skill_dir: str | Path,
    payload: Mapping[str, Any],
) -> Path:
    """Write ``.cloud_skill.json`` to a skill directory."""

    target_dir = Path(skill_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    sidecar_path = target_dir / CLOUD_SKILL_SIDECAR_FILENAME
    sidecar_path.write_text(_dump_json(dict(payload)) + "\n", encoding="utf-8")
    return sidecar_path


def write_cloud_skill_info(skill_dir: str | Path, binding: SkillCloudBinding) -> Path:
    return write_cloud_skill_sidecar(
        skill_dir,
        {
            "schema_version": 1,
            "api_version": "v2",
            "local_skill_id": binding.local_skill_id,
            "cloud_skill_id": binding.cloud_skill_id,
            "package_id_at_pull": binding.package_id_at_pull,
            "package_path_at_pull": binding.package_path_at_pull,
            "package_snapshot_version_at_pull": binding.package_snapshot_version_at_pull,
            "current_package_id": binding.current_package_id,
            "current_package_path": binding.current_package_path,
            "source_cloud_skill_id": binding.source_cloud_skill_id,
            "manifest_hash": binding.manifest_hash,
            "local_content_hash": binding.local_content_hash,
            "sync_state": binding.sync_state,
            "last_pulled_at": binding.last_pulled_at,
            "last_pushed_at": binding.last_pushed_at,
        },
    )


def compute_skill_local_content_hash(skill_dir: str | Path) -> str:
    """Compute a stable hash of local skill contents, excluding local sidecars."""

    root = Path(skill_dir)
    if not root.is_dir():
        raise ValueError(f"Skill directory not found: {root}")
    hasher = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if not path.is_file() or path.name in _HASH_EXCLUDED_FILENAMES:
            continue
        relative_path = path.relative_to(root).as_posix()
        data = path.read_bytes()
        hasher.update(relative_path.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(str(len(data)).encode("ascii"))
        hasher.update(b"\0")
        hasher.update(data)
        hasher.update(b"\0")
    return f"sha256:{hasher.hexdigest()}"


compute_local_content_hash = compute_skill_local_content_hash


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_local_skill_id(skill_dir: str | Path) -> str | None:
    try:
        value = (Path(skill_dir) / SKILL_ID_FILENAME).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def write_local_skill_id(skill_dir: str | Path, local_skill_id: str) -> Path:
    local_id = _required_text(local_skill_id, "local_skill_id")
    path = Path(skill_dir) / SKILL_ID_FILENAME
    path.write_text(local_id + "\n", encoding="utf-8")
    return path


def generate_local_skill_id(skill_name: str) -> str:
    name = _required_text(skill_name or "skill", "skill_name")
    return f"{name}__imp_{uuid.uuid4().hex[:8]}"


def ensure_local_skill_id(skill_dir: str | Path, *, skill_name: str | None = None) -> str:
    existing = read_local_skill_id(skill_dir)
    if existing:
        return existing
    local_id = generate_local_skill_id(skill_name or Path(skill_dir).name)
    write_local_skill_id(skill_dir, local_id)
    return local_id


def _row_to_skill_cloud_binding(row: sqlite3.Row) -> SkillCloudBinding:
    return SkillCloudBinding(
        local_skill_id=row["local_skill_id"],
        cloud_skill_id=row["cloud_skill_id"],
        local_path=row["local_path"],
        package_id_at_pull=row["package_id_at_pull"],
        package_path_at_pull=row["package_path_at_pull"],
        package_snapshot_version_at_pull=row["package_snapshot_version_at_pull"],
        current_package_id=row["current_package_id"],
        current_package_path=row["current_package_path"],
        source_cloud_skill_id=row["source_cloud_skill_id"],
        manifest_hash=row["manifest_hash"],
        local_content_hash=row["local_content_hash"],
        sync_state=row["sync_state"],
        last_pulled_at=row["last_pulled_at"],
        last_pushed_at=row["last_pushed_at"],
    )


def _row_to_skill_local_classification(row: sqlite3.Row) -> SkillLocalClassification:
    return SkillLocalClassification(
        local_skill_id=row["local_skill_id"],
        category=row["category"],
        local_category_path=row["local_category_path"],
        classification_confidence=row["classification_confidence"],
        classification_rationale=row["classification_rationale"],
        review_state=row["review_state"],
        evidence=json.loads(row["evidence_json"]),
        updated_at=row["updated_at"],
    )


def _row_to_package_cache_entry(row: sqlite3.Row) -> PackageCacheEntry:
    return PackageCacheEntry(
        package_id=row["package_id"],
        package_path=row["package_path"],
        projection_hash=row["projection_hash"],
        serving_epoch=row["serving_epoch"],
        source_epoch=row["source_epoch"],
        last_pulled_at=row["last_pulled_at"],
    )


def _row_to_package_path_index_entry(row: sqlite3.Row) -> PackagePathIndexEntry:
    can_create = row["can_create_child_regular_package"]
    return PackagePathIndexEntry(
        snapshot_version=row["snapshot_version"],
        package_path=row["package_path"],
        package_id=row["package_id"],
        normalized_package_path=row["normalized_package_path"],
        root_sub_domain_package_id=row["root_sub_domain_package_id"],
        parent_package_id=row["parent_package_id"],
        package_kind=row["package_kind"],
        can_select_as_upload_target=bool(row["can_select_as_upload_target"]),
        can_create_child_regular_package=(
            None if can_create is None else bool(can_create)
        ),
        select_disabled_reason=row["select_disabled_reason"],
        fetched_from=row["fetched_from"],
        fetched_at=row["fetched_at"],
    )


def _required_text(value: str, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _optional_bool(value: bool | None) -> int | None:
    if value is None:
        return None
    return 1 if value else 0


def normalize_package_path_key(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    segments = [
        re.sub(r"\s+", " ", segment.strip()).casefold()
        for segment in text.replace("\\", "/").split("/")
        if segment.strip()
    ]
    return "/".join(segments)


def _dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
