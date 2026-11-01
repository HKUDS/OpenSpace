"""Evidence contracts and storage for product evolution."""

from .runtime_adapter import RuntimeEvidenceAdapter
from .session_adapter import SessionEvidenceAdapter
from .skill_adapter import SkillEvidenceAdapter
from .store import (
    EvidenceStore,
    resolve_evidence_db_path,
    resolve_evolution_storage_root,
    resolve_skill_store_db_path,
)
from .tool_adapter import ToolEvidenceAdapter
from .memory_adapter import MemoryEvidenceAdapter
from .packet_builder import PacketBuilder
from .profiles import (
    EvidenceProfile,
    RepresentativeSamplingPolicy,
    SelectionPolicy,
    TranscriptWindowPolicy,
)
from .types import (
    EvidenceEvent,
    EvidencePacket,
    EvidenceScope,
    EvidenceSnippet,
    ManifestView,
    PacketBudget,
    PacketBuildResult,
    ReadablePathRef,
    ResourceRef,
)

__all__ = [
    "EvidenceEvent",
    "EvidencePacket",
    "EvidenceProfile",
    "EvidenceScope",
    "EvidenceSnippet",
    "EvidenceStore",
    "ManifestView",
    "MemoryEvidenceAdapter",
    "PacketBudget",
    "PacketBuildResult",
    "PacketBuilder",
    "ReadablePathRef",
    "ResourceRef",
    "RuntimeEvidenceAdapter",
    "SessionEvidenceAdapter",
    "RepresentativeSamplingPolicy",
    "SelectionPolicy",
    "SkillEvidenceAdapter",
    "ToolEvidenceAdapter",
    "TranscriptWindowPolicy",
    "resolve_evidence_db_path",
    "resolve_evolution_storage_root",
    "resolve_skill_store_db_path",
]
