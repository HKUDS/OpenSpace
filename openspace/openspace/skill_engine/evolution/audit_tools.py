"""Packet-scoped read-only tools for evolution authoring and validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from openspace.grounding.core.tool.base import BaseTool
from openspace.grounding.core.types import BackendType
from openspace.skill_engine.evidence import EvidencePacket, ResourceRef
from openspace.skill_engine.evidence.redaction import contains_secret


class PacketAuditReadError(PermissionError):
    """Raised when a packet audit read would exceed packet scope."""


class PacketAuditReader:
    """Read only refs and files explicitly selected into an EvidencePacket."""

    def __init__(self, packet: EvidencePacket) -> None:
        self.packet = packet
        self._refs = {
            ref.ref_id: ref
            for refs in packet.selected_refs.values()
            for ref in refs
            if ref.ref_id
        }
        self._readable_paths = {
            path.ref_id: path
            for path in packet.readable_paths
            if path.ref_id and path.readable and not path.contains_secret
        }

    def list_packet_refs(self) -> list[dict[str, Any]]:
        return [
            {
                "ref_id": ref.ref_id,
                "ref_type": ref.ref_type,
                "uri": ref.uri,
                "reliability": ref.reliability,
                "role": ref.role,
                "contains_secret": ref.contains_secret,
                "preview": ref.preview,
                "metadata": dict(ref.metadata),
            }
            for ref in sorted(self._refs.values(), key=lambda item: item.ref_id)
        ]

    def read_ref_preview(self, ref_id: str) -> str:
        ref = self._require_ref(ref_id)
        if ref.contains_secret:
            raise PacketAuditReadError(f"Ref is marked secret: {ref_id}")
        return ref.preview or ""

    def read_ref_full(self, ref_id: str) -> str:
        ref = self._require_ref(ref_id)
        path = self._require_readable_path(ref)
        content = path.read_text(encoding="utf-8", errors="replace")
        if contains_secret(content):
            raise PacketAuditReadError(f"Readable ref content contains secrets: {ref_id}")
        return content

    def read_skill_file(self, ref_id: str) -> str:
        ref = self._require_ref(ref_id)
        if ref.ref_type != "skill_file":
            raise PacketAuditReadError(f"Ref is not a skill_file: {ref_id}")
        return self.read_ref_full(ref_id)

    def _require_ref(self, ref_id: str) -> ResourceRef:
        ref = self._refs.get(str(ref_id or ""))
        if ref is None:
            raise PacketAuditReadError(f"Ref is not in packet scope: {ref_id}")
        return ref

    def _require_readable_path(self, ref: ResourceRef) -> Path:
        if ref.contains_secret:
            raise PacketAuditReadError(f"Ref is marked secret: {ref.ref_id}")
        readable = self._readable_paths.get(ref.ref_id)
        if readable is None:
            raise PacketAuditReadError(
                f"Ref has no whitelisted readable path: {ref.ref_id}"
            )
        path = Path(readable.path).expanduser().resolve()
        uri_path = (
            Path(_path_from_ref_uri(ref.uri)).expanduser().resolve()
            if ref.uri
            else path
        )
        if path != uri_path:
            raise PacketAuditReadError(f"Readable path does not match ref URI: {ref.ref_id}")
        if not path.is_file():
            raise PacketAuditReadError(f"Readable path is not a file: {path}")
        if contains_secret(str(path)):
            raise PacketAuditReadError(f"Readable path appears sensitive: {path}")
        return path


def _path_from_ref_uri(uri: str | None) -> str:
    if not uri:
        return ""
    return str(uri).split("#", 1)[0]


class _PacketAuditTool(BaseTool):
    backend_type = BackendType.NOT_SET
    _is_read_only = True
    _is_concurrency_safe = True
    _is_destructive = False

    def __init__(self, reader: PacketAuditReader) -> None:
        self._reader = reader
        super().__init__()

    async def _arun(self, **_kwargs: Any) -> Any:
        raise NotImplementedError


class ReadRefPreviewTool(_PacketAuditTool):
    _name = "read_ref_preview"
    _description = "Read the packet-scoped preview for a selected evidence ref."
    parameter_descriptions = {"ref_id": "Evidence ref id selected into the packet."}

    async def _arun(self, ref_id: str) -> str:
        return self._reader.read_ref_preview(ref_id)


class ReadRefFullTool(_PacketAuditTool):
    _name = "read_ref_full"
    _description = "Read full content for a selected ref with a whitelisted readable path."
    parameter_descriptions = {"ref_id": "Evidence ref id selected into the packet."}

    async def _arun(self, ref_id: str) -> str:
        return self._reader.read_ref_full(ref_id)


class ListPacketRefsTool(_PacketAuditTool):
    _name = "list_packet_refs"
    _description = "List evidence refs selected into the packet."

    async def _arun(self) -> list[dict[str, Any]]:
        return self._reader.list_packet_refs()


class ReadSkillFileTool(_PacketAuditTool):
    _name = "read_skill_file"
    _description = "Read a packet-scoped skill_file ref from its whitelisted path."
    parameter_descriptions = {"ref_id": "Skill file ref id selected into the packet."}

    async def _arun(self, ref_id: str) -> str:
        return self._reader.read_skill_file(ref_id)


def build_packet_audit_tools(packet: EvidencePacket) -> list[BaseTool]:
    reader = PacketAuditReader(packet)
    return [
        ReadRefPreviewTool(reader),
        ReadRefFullTool(reader),
        ListPacketRefsTool(reader),
        ReadSkillFileTool(reader),
    ]
