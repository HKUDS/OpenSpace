"""AIME-owned canonical development material references."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class CanonicalDevelopmentMaterialRef:
    """One verified task-local field reference; content bytes are not retained."""

    task_id: str
    field: str
    status: str
    content_ref: str
    content_digest: str
    file_count: int
    total_bytes: int
    source_paths: tuple[Path, ...]


@dataclass(frozen=True)
class CanonicalDevelopmentCorpusHandle:
    """Immutable metadata for one AIME-owned development corpus."""

    benchmark: str
    task_ids: tuple[str, ...]
    canonical_fields: tuple[str, ...]
    materials: tuple[CanonicalDevelopmentMaterialRef, ...]
    corpus_manifest_ref: str
    corpus_manifest_digest: str
    corpus_content_digest: str
    bench_evidence_spec_ref: str
    bench_evidence_spec_digest: str
    bench_identity_digest: str
    split_digest: str
    task_snapshot_digest: str
    content_storage: str
    source_provenance_status: str
    package_status: str = field(
        default="CANONICAL_DEVELOPMENT_CORPUS_PACKAGED", init=False
    )
