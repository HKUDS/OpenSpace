"""Controller-side package placement resolution for v2 skill upload."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable
import re

from openspace.cloud.client import CloudError, OpenSpaceClient
from openspace.cloud.local_mapping import (
    CloudLocalMappingStore,
    normalize_package_path_key,
    utc_now_iso,
)


PACKAGE_PLACEMENT_REQUIRED = "PACKAGE_PLACEMENT_REQUIRED"
PACKAGE_PLACEMENT_AMBIGUOUS = "PACKAGE_PLACEMENT_AMBIGUOUS"
PACKAGE_PLACEMENT_NOT_FOUND = "PACKAGE_PLACEMENT_NOT_FOUND"
PACKAGE_TARGET_NOT_SELECTABLE = "PACKAGE_TARGET_NOT_SELECTABLE"
PACKAGE_SNAPSHOT_STALE = "PACKAGE_SNAPSHOT_STALE"
PACKAGE_PLACEMENT_REF_NOT_FOUND = "PACKAGE_PLACEMENT_REF_NOT_FOUND"
PACKAGE_PLACEMENT_INVALID_CHILD_SEGMENT = "PACKAGE_PLACEMENT_INVALID_CHILD_SEGMENT"
PACKAGE_PLACEMENT_MULTI_SEGMENT_CREATE = "PACKAGE_PLACEMENT_MULTI_SEGMENT_CREATE"
PACKAGE_ID_NOT_IN_CURRENT_SNAPSHOT = "PACKAGE_ID_NOT_IN_CURRENT_SNAPSHOT"
PACKAGE_PLACEMENT_CONFLICTING_FIELDS = "PACKAGE_PLACEMENT_CONFLICTING_FIELDS"


@dataclass(frozen=True)
class PackageNode:
    package_id: str
    package_path: str
    package_path_segments: tuple[str, ...]
    snapshot_version: str
    package_kind: str = ""
    parent_package_id: str | None = None
    root_sub_domain_package_id: str | None = None
    can_select_as_upload_target: bool = False
    can_create_child_regular_package: bool | None = None
    select_disabled_reason: str | None = None

    @property
    def normalized_package_path(self) -> str:
        return normalize_package_path_key(self.package_path)


@dataclass(frozen=True)
class DomainIndexSnapshot:
    snapshot_version: str
    nodes: tuple[PackageNode, ...]


@dataclass(frozen=True)
class SubtreeSnapshot:
    root_sub_domain_package_id: str
    snapshot_version: str
    nodes: tuple[PackageNode, ...]
    path_index: dict[str, tuple[PackageNode, ...]]
    nodes_by_id: dict[str, PackageNode]


@dataclass(frozen=True)
class ResolvedUploadPlacement:
    requested_package_id: str | None = None
    requested_parent_package_id: str | None = None
    requested_new_package_segment: str | None = None
    snapshot_version_used: str | None = None
    package_path: str | None = None
    root_sub_domain_package_id: str | None = None

    def to_upload_kwargs(self) -> dict[str, str | None]:
        return {
            "requested_package_id": self.requested_package_id,
            "requested_parent_package_id": self.requested_parent_package_id,
            "requested_new_package_segment": self.requested_new_package_segment,
            "snapshot_version_used": self.snapshot_version_used,
        }


@dataclass(frozen=True)
class PlacementCandidate:
    selection_ref: str
    snapshot_version: str
    requested_package_id: str | None = None
    requested_parent_package_id: str | None = None
    requested_new_package_segment: str | None = None
    package_path: str | None = None
    root_sub_domain_package_id: str | None = None


class PackagePlacementError(CloudError):
    """Structured local error raised by package placement controllers."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message,
            code=code,
            kind="validation",
            retryable=False,
            details=details,
        )


class PackageSnapshotCache:
    """Lightweight snapshot cache backed by current cloud picker endpoints."""

    def __init__(
        self,
        client: OpenSpaceClient,
        *,
        mapping_store: CloudLocalMappingStore | None = None,
    ) -> None:
        self._client = client
        self._mapping_store = mapping_store
        self._domain_index: DomainIndexSnapshot | None = None
        self._subtrees: dict[tuple[str, str], SubtreeSnapshot] = {}

    def refresh_domain_index(self, *, force: bool = True) -> DomainIndexSnapshot:
        if self._domain_index is not None and not force:
            return self._domain_index
        payload = self._client.get_package_domain_index()
        snapshot = self._domain_snapshot_from_payload(payload)
        if (
            self._domain_index is not None
            and self._domain_index.snapshot_version != snapshot.snapshot_version
        ):
            self._subtrees.clear()
        self._domain_index = snapshot
        self._cache_nodes(snapshot.nodes, fetched_from="domain_index")
        return snapshot

    def get_subtree_for_upload(
        self,
        root_sub_domain_package_id: str,
        *,
        snapshot_version: str,
    ) -> SubtreeSnapshot:
        key = (root_sub_domain_package_id, snapshot_version)
        cached = self._subtrees.get(key)
        if cached is not None:
            return cached
        payload = self._client.get_package_subtree_for_upload(
            root_sub_domain_package_id,
            snapshot_version=snapshot_version,
        )
        subtree_snapshot = str(payload.get("snapshot_version") or snapshot_version)
        if subtree_snapshot != snapshot_version:
            raise PackagePlacementError(
                PACKAGE_SNAPSHOT_STALE,
                "Package snapshot changed while loading upload subtree",
                details={
                    "requested_snapshot_version": snapshot_version,
                    "current_snapshot_version": subtree_snapshot,
                    "root_sub_domain_package_id": root_sub_domain_package_id,
                },
            )
        subtree = self._subtree_from_payload(
            payload,
            root_sub_domain_package_id=root_sub_domain_package_id,
            snapshot_version=snapshot_version,
        )
        self._subtrees[key] = subtree
        self._cache_nodes(subtree.nodes, fetched_from="subtree_for_upload")
        return subtree

    def _domain_snapshot_from_payload(self, payload: dict[str, Any]) -> DomainIndexSnapshot:
        snapshot_version = _required_text(payload.get("snapshot_version"), "snapshot_version")
        nodes = tuple(
            _node_from_payload(node, snapshot_version=snapshot_version)
            for node in payload.get("nodes") or []
            if isinstance(node, dict)
        )
        return DomainIndexSnapshot(snapshot_version=snapshot_version, nodes=nodes)

    def _subtree_from_payload(
        self,
        payload: dict[str, Any],
        *,
        root_sub_domain_package_id: str,
        snapshot_version: str,
    ) -> SubtreeSnapshot:
        nodes = tuple(
            _node_from_payload(
                node,
                snapshot_version=snapshot_version,
                root_sub_domain_package_id=root_sub_domain_package_id,
            )
            for node in payload.get("nodes") or []
            if isinstance(node, dict)
        )
        path_index: dict[str, list[PackageNode]] = {}
        nodes_by_id: dict[str, PackageNode] = {}
        for node in nodes:
            path_index.setdefault(node.normalized_package_path, []).append(node)
            nodes_by_id[node.package_id] = node
        return SubtreeSnapshot(
            root_sub_domain_package_id=root_sub_domain_package_id,
            snapshot_version=snapshot_version,
            nodes=nodes,
            path_index={key: tuple(value) for key, value in path_index.items()},
            nodes_by_id=nodes_by_id,
        )

    def _cache_nodes(self, nodes: Iterable[PackageNode], *, fetched_from: str) -> None:
        if self._mapping_store is None:
            return
        fetched_at = utc_now_iso()
        for node in nodes:
            self._mapping_store.upsert_package_path_index_entry(
                snapshot_version=node.snapshot_version,
                package_path=node.package_path,
                package_id=node.package_id,
                root_sub_domain_package_id=node.root_sub_domain_package_id,
                parent_package_id=node.parent_package_id,
                package_kind=node.package_kind,
                can_select_as_upload_target=node.can_select_as_upload_target,
                can_create_child_regular_package=node.can_create_child_regular_package,
                select_disabled_reason=node.select_disabled_reason,
                fetched_from=fetched_from,
                fetched_at=fetched_at,
            )


class PackagePlacementResolver:
    """Resolve agent-facing refs/paths into authoritative upload UUID fields."""

    def __init__(
        self,
        client: OpenSpaceClient,
        *,
        mapping_store: CloudLocalMappingStore | None = None,
        cache: PackageSnapshotCache | None = None,
    ) -> None:
        self._cache = cache or PackageSnapshotCache(client, mapping_store=mapping_store)
        self._latest_candidates: dict[str, dict[str, PlacementCandidate]] = {}

    def record_latest_candidates(
        self,
        flow_session_id: str,
        candidates: Iterable[PlacementCandidate],
    ) -> None:
        session_id = _required_text(flow_session_id, "flow_session_id")
        self._latest_candidates[session_id] = {
            _required_text(candidate.selection_ref, "selection_ref"): candidate
            for candidate in candidates
        }

    def resolve_selection_ref(
        self,
        *,
        flow_session_id: str,
        selection_ref: str,
    ) -> ResolvedUploadPlacement:
        candidates = self._latest_candidates.get(_required_text(flow_session_id, "flow_session_id")) or {}
        candidate = candidates.get(_required_text(selection_ref, "selection_ref"))
        if candidate is None:
            raise PackagePlacementError(
                PACKAGE_PLACEMENT_REF_NOT_FOUND,
                "Package selection_ref is not from the latest candidates for this flow session",
                details={
                    "flow_session_id": flow_session_id,
                    "selection_ref": selection_ref,
                },
            )
        return self.validate_confirmed_placement(
            requested_package_id=candidate.requested_package_id,
            requested_parent_package_id=candidate.requested_parent_package_id,
            requested_new_package_segment=candidate.requested_new_package_segment,
            snapshot_version_used=candidate.snapshot_version,
            root_sub_domain_package_id=candidate.root_sub_domain_package_id,
        )

    def resolve_cloud_package_path(self, cloud_package_path: str) -> ResolvedUploadPlacement:
        target_segments = _split_package_path(cloud_package_path)
        if len(target_segments) < 3:
            raise PackagePlacementError(
                PACKAGE_TARGET_NOT_SELECTABLE,
                "Upload package path must include domain/sub-domain/regular package",
                details={"cloud_package_path": cloud_package_path},
            )
        domain = self._cache.refresh_domain_index(force=True)
        sub_domain_path = "/".join(target_segments[:2])
        sub_domain_matches = [
            node
            for node in domain.nodes
            if node.package_kind == "sub-domain"
            and node.normalized_package_path == normalize_package_path_key(sub_domain_path)
        ]
        if not sub_domain_matches:
            raise PackagePlacementError(
                PACKAGE_PLACEMENT_NOT_FOUND,
                "No current sub-domain package matches the requested path",
                details={"cloud_package_path": cloud_package_path},
            )
        if len(sub_domain_matches) > 1:
            raise PackagePlacementError(
                PACKAGE_PLACEMENT_AMBIGUOUS,
                "Requested sub-domain package path is ambiguous in the current snapshot",
                details={"candidates": [_node_summary(node) for node in sub_domain_matches]},
            )
        sub_domain = sub_domain_matches[0]
        subtree = self._cache.get_subtree_for_upload(
            sub_domain.package_id,
            snapshot_version=domain.snapshot_version,
        )
        normalized_path = normalize_package_path_key("/".join(target_segments))
        exact_matches = list(subtree.path_index.get(normalized_path) or ())
        if len(exact_matches) > 1:
            raise PackagePlacementError(
                PACKAGE_PLACEMENT_AMBIGUOUS,
                "Requested package path is ambiguous in the current snapshot",
                details={"candidates": [_node_summary(node) for node in exact_matches]},
            )
        if len(exact_matches) == 1:
            return self._placement_for_existing_node(exact_matches[0])
        parent = _deepest_prefix_node(subtree.nodes, target_segments)
        if parent is None:
            parent = sub_domain
        remaining = target_segments[len(parent.package_path_segments):]
        if len(remaining) != 1:
            raise PackagePlacementError(
                PACKAGE_PLACEMENT_MULTI_SEGMENT_CREATE,
                "Package path cannot be created in one upload because more than one segment is missing",
                details={
                    "cloud_package_path": cloud_package_path,
                    "missing_segments": remaining,
                },
            )
        _validate_child_segment(remaining[0])
        if parent.can_create_child_regular_package is False:
            raise PackagePlacementError(
                PACKAGE_TARGET_NOT_SELECTABLE,
                "The matched parent package cannot create child regular packages",
                details={"parent": _node_summary(parent)},
            )
        return ResolvedUploadPlacement(
            requested_parent_package_id=parent.package_id,
            requested_new_package_segment=remaining[0],
            snapshot_version_used=subtree.snapshot_version,
            package_path="/".join(target_segments),
            root_sub_domain_package_id=subtree.root_sub_domain_package_id,
        )

    def validate_confirmed_placement(
        self,
        *,
        requested_package_id: str | None = None,
        requested_parent_package_id: str | None = None,
        requested_new_package_segment: str | None = None,
        snapshot_version_used: str | None = None,
        root_sub_domain_package_id: str | None = None,
    ) -> ResolvedUploadPlacement:
        if requested_package_id and (requested_parent_package_id or requested_new_package_segment):
            raise PackagePlacementError(
                PACKAGE_PLACEMENT_CONFLICTING_FIELDS,
                "Use either requested_package_id or parent+new segment, not both",
            )
        if requested_parent_package_id or requested_new_package_segment:
            if not requested_parent_package_id or not requested_new_package_segment:
                raise PackagePlacementError(
                    PACKAGE_PLACEMENT_CONFLICTING_FIELDS,
                    "requested_parent_package_id and requested_new_package_segment must be provided together",
                )
            _validate_child_segment(requested_new_package_segment)
            parent = self._find_current_node(
                requested_parent_package_id,
                snapshot_version_used=snapshot_version_used,
                root_sub_domain_package_id=root_sub_domain_package_id,
            )
            if parent.can_create_child_regular_package is False:
                raise PackagePlacementError(
                    PACKAGE_TARGET_NOT_SELECTABLE,
                    "The requested parent package cannot create child regular packages",
                    details={"parent": _node_summary(parent)},
                )
            return ResolvedUploadPlacement(
                requested_parent_package_id=parent.package_id,
                requested_new_package_segment=requested_new_package_segment,
                snapshot_version_used=parent.snapshot_version,
                package_path=f"{parent.package_path}/{requested_new_package_segment}",
                root_sub_domain_package_id=parent.root_sub_domain_package_id,
            )
        if requested_package_id:
            node = self._find_current_node(
                requested_package_id,
                snapshot_version_used=snapshot_version_used,
                root_sub_domain_package_id=root_sub_domain_package_id,
            )
            return self._placement_for_existing_node(node)
        raise PackagePlacementError(
            PACKAGE_PLACEMENT_REQUIRED,
            "Package placement is required for this upload",
        )

    def _placement_for_existing_node(self, node: PackageNode) -> ResolvedUploadPlacement:
        if node.package_kind != "regular" or not node.can_select_as_upload_target:
            raise PackagePlacementError(
                PACKAGE_TARGET_NOT_SELECTABLE,
                "Requested package is not selectable for upload",
                details={
                    "package": _node_summary(node),
                    "select_disabled_reason": node.select_disabled_reason,
                },
            )
        return ResolvedUploadPlacement(
            requested_package_id=node.package_id,
            snapshot_version_used=node.snapshot_version,
            package_path=node.package_path,
            root_sub_domain_package_id=node.root_sub_domain_package_id,
        )

    def _find_current_node(
        self,
        package_id: str,
        *,
        snapshot_version_used: str | None = None,
        root_sub_domain_package_id: str | None = None,
    ) -> PackageNode:
        package_id = _required_text(package_id, "package_id")
        domain = self._cache.refresh_domain_index(force=True)
        if snapshot_version_used and snapshot_version_used != domain.snapshot_version:
            raise PackagePlacementError(
                PACKAGE_SNAPSHOT_STALE,
                "Confirmed package placement used an expired snapshot",
                details={
                    "confirmed_snapshot_version": snapshot_version_used,
                    "current_snapshot_version": domain.snapshot_version,
                },
            )
        sub_domain_ids: list[str]
        if root_sub_domain_package_id:
            sub_domain_ids = [root_sub_domain_package_id]
        else:
            sub_domain_ids = [
                node.package_id for node in domain.nodes if node.package_kind == "sub-domain"
            ]
        for sub_domain_id in sub_domain_ids:
            subtree = self._cache.get_subtree_for_upload(
                sub_domain_id,
                snapshot_version=domain.snapshot_version,
            )
            node = subtree.nodes_by_id.get(package_id)
            if node is not None:
                return node
        raise PackagePlacementError(
            PACKAGE_ID_NOT_IN_CURRENT_SNAPSHOT,
            "Package id is not present in the current upload picker snapshot",
            details={"package_id": package_id},
        )


def placement_from_upload_meta(
    meta: dict[str, Any],
) -> dict[str, str | None]:
    raw = meta.get("upload_placement")
    if not isinstance(raw, dict):
        return {}
    return {
        "requested_package_id": _optional_text(raw.get("requested_package_id")),
        "requested_parent_package_id": _optional_text(raw.get("requested_parent_package_id")),
        "requested_new_package_segment": _optional_text(raw.get("requested_new_package_segment")),
        "snapshot_version_used": _optional_text(raw.get("snapshot_version_used")),
        "root_sub_domain_package_id": _optional_text(raw.get("root_sub_domain_package_id")),
    }


def _node_from_payload(
    node: dict[str, Any],
    *,
    snapshot_version: str,
    root_sub_domain_package_id: str | None = None,
) -> PackageNode:
    package_id = _required_text(node.get("package_id"), "package_id")
    segments = _node_segments(node)
    package_path = str(node.get("package_path") or "/".join(segments)).strip("/")
    return PackageNode(
        package_id=package_id,
        package_path=package_path,
        package_path_segments=tuple(segments or _split_package_path(package_path)),
        snapshot_version=snapshot_version,
        package_kind=str(node.get("package_kind") or ""),
        parent_package_id=_optional_text(node.get("parent_package_id")),
        root_sub_domain_package_id=(
            root_sub_domain_package_id
            or _optional_text(node.get("root_sub_domain_package_id"))
            or (
                package_id
                if str(node.get("package_kind") or "") == "sub-domain"
                else None
            )
        ),
        can_select_as_upload_target=bool(node.get("can_select_as_upload_target")),
        can_create_child_regular_package=_optional_bool(
            node.get("can_create_child_regular_package")
        ),
        select_disabled_reason=_optional_text(node.get("select_disabled_reason")),
    )


def _node_segments(node: dict[str, Any]) -> list[str]:
    raw = node.get("package_path_segments")
    if isinstance(raw, list) and raw:
        return [str(segment).strip() for segment in raw if str(segment).strip()]
    package_path = str(node.get("package_path") or "")
    if package_path:
        return _split_package_path(package_path)
    display = str(node.get("package_display_name") or "").strip()
    return [display] if display else []


def _split_package_path(package_path: str) -> list[str]:
    segments = [
        re.sub(r"\s+", " ", segment.strip())
        for segment in str(package_path).replace("\\", "/").split("/")
        if segment.strip()
    ]
    if not segments:
        raise PackagePlacementError(
            PACKAGE_PLACEMENT_NOT_FOUND,
            "cloud_package_path must not be empty",
        )
    return segments


def _deepest_prefix_node(nodes: Iterable[PackageNode], target_segments: list[str]) -> PackageNode | None:
    target_key_segments = [
        normalize_package_path_key(segment) for segment in target_segments
    ]
    best: PackageNode | None = None
    best_len = -1
    for node in nodes:
        node_key_segments = [
            normalize_package_path_key(segment) for segment in node.package_path_segments
        ]
        if len(node_key_segments) > len(target_key_segments):
            continue
        if node_key_segments == target_key_segments[: len(node_key_segments)] and len(node_key_segments) > best_len:
            best = node
            best_len = len(node_key_segments)
    return best


def _validate_child_segment(segment: str) -> None:
    value = _required_text(segment, "requested_new_package_segment")
    if "/" in value or "\\" in value:
        raise PackagePlacementError(
            PACKAGE_PLACEMENT_INVALID_CHILD_SEGMENT,
            "requested_new_package_segment must be exactly one path segment",
            details={"requested_new_package_segment": segment},
        )


def _node_summary(node: PackageNode) -> dict[str, Any]:
    return {
        "package_id": node.package_id,
        "package_path": node.package_path,
        "package_kind": node.package_kind,
        "can_select_as_upload_target": node.can_select_as_upload_target,
        "select_disabled_reason": node.select_disabled_reason,
    }


def _required_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise PackagePlacementError(
            PACKAGE_PLACEMENT_NOT_FOUND,
            f"{field_name} is required",
            details={"field": field_name},
        )
    return text


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)
