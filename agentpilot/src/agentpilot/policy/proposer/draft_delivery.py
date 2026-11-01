"""Bounded native draft delivery without semantic shortening.

The short legacy limits are presentation targets here, not storage limits.
Only ``direction`` is worker input. ``audit_snapshot()`` retains the exact wire
draft for the proposer journal; it must not enter implementation_evidence.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from agentpilot.app.model_harnesses import (
    _LOCAL_ALIAS_TOKEN,
    STRUCTURED_DIRECTION_STORAGE_MAX_RESPONSE_BYTES,
    STRUCTURED_PROPOSER_CONTRACT,
    DirectionAliasCatalog,
    StructuredProposalParseError,
    _evidence_catalog,
    _new_path_entries,
    _relative_new_path,
    _repository_file_bytes,
    build_direction_alias_catalog,
    parse_trace_aware_direction,
)
from agentpilot.domain import ScopeKind, StructuredDirection, canonical_json
from agentpilot.domain.common import deep_freeze
from agentpilot.domain.policy import (
    FAILURE_MECHANISM_MAX_CHARS,
    FAILURE_MECHANISM_STORAGE_MAX_CHARS,
    IMPLEMENTATION_DIRECTION_MAX_CHARS,
    IMPLEMENTATION_DIRECTION_STORAGE_MAX_CHARS,
    NEW_PATH_ROLE_MAX_CHARS,
    OBSERVED_BEHAVIOR_MAX_CHARS,
    OBSERVED_BEHAVIOR_STORAGE_MAX_CHARS,
)
from agentpilot.policy.proposer.base import DraftDeliveryError, StructuredProposalRequest

NATIVE_DRAFT_MAX_BYTES = STRUCTURED_DIRECTION_STORAGE_MAX_RESPONSE_BYTES
NATIVE_DIRECTION_MAX_BYTES = 24 * 1024
NATIVE_TEXT_LIMITS = (
    OBSERVED_BEHAVIOR_STORAGE_MAX_CHARS,
    FAILURE_MECHANISM_STORAGE_MAX_CHARS,
    IMPLEMENTATION_DIRECTION_STORAGE_MAX_CHARS,
)
TEXT_FIELDS = ("observed_behavior", "failure_mechanism", "implementation_direction")
PRESENTATION_TARGETS = dict(
    zip(
        TEXT_FIELDS,
        (
            OBSERVED_BEHAVIOR_MAX_CHARS,
            FAILURE_MECHANISM_MAX_CHARS,
            IMPLEMENTATION_DIRECTION_MAX_CHARS,
        ),
    )
)
_FIELDS = {*TEXT_FIELDS, "scope_refs", "new_paths", "evidence_refs"}


@dataclass(frozen=True)
class DraftIssue:
    field: str
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"field": self.field, "code": self.code, "message": self.message}


@dataclass(frozen=True)
class PreparedDraft:
    raw_text: str
    raw_sha256: str
    raw_bytes: int
    normalized_args: Mapping[str, Any] | None = None
    issues: tuple[DraftIssue, ...] = ()
    normalizations: tuple[Mapping[str, Any], ...] = ()
    direction: StructuredDirection | None = None

    @property
    def deliverable(self) -> bool:
        return self.direction is not None and not self.issues

    def metadata(self) -> dict[str, Any]:
        """Small audit metadata; intentionally excludes raw or repeated intent."""
        return {
            "contract": "bounded-native-draft-delivery-v1",
            "raw_sha256": self.raw_sha256,
            "raw_bytes": self.raw_bytes,
            "wire_limit_bytes": NATIVE_DRAFT_MAX_BYTES,
            "typed_direction_limit_bytes": NATIVE_DIRECTION_MAX_BYTES,
            "issues": [issue.as_dict() for issue in self.issues],
            "normalizations": [dict(item) for item in self.normalizations],
            "delivered_direction_id": None
            if self.direction is None
            else self.direction.direction_id,
        }

    def audit_snapshot(self) -> dict[str, Any]:
        """Exact received text, including malformed/oversize text; audit only.

        Delivery is bounded by NATIVE_DRAFT_MAX_BYTES. An over-bound response
        is never accepted or silently clipped: the exception still references
        the received provider text, whose transport limit belongs to the caller.
        """
        return {**self.metadata(), "raw_text": self.raw_text, "audit_only": True}

    def require_direction(self) -> StructuredDirection:
        if self.deliverable:
            assert self.direction is not None
            return self.direction
        issues = self.issues or (
            DraftIssue(
                "direction",
                "not_validated",
                "Draft has not been validated for delivery",
            ),
        )
        raise DraftDeliveryError(
            draft=self.audit_snapshot(),
            issues=tuple(issue.as_dict() for issue in issues),
        )


def native_draft_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Keep refs, scope and fixed-family consts; change native text bounds only."""
    result = copy.deepcopy(dict(schema))
    for name, hard_limit in zip(TEXT_FIELDS, NATIVE_TEXT_LIMITS):
        field = result["properties"][name]
        field["maxLength"] = hard_limit
        field["description"] = (
            f"Prefer a concise presentation near {PRESENTATION_TARGETS[name]} characters; "
            f"preserve the full concrete intent when needed, up to {hard_limit} characters. "
            "Do not discard material facts or behavior just to meet the short target."
            f" The complete resolved typed direction must fit within {NATIVE_DIRECTION_MAX_BYTES} UTF-8 JSON bytes for worker delivery."
            + (" Repeat the fixed family exactly." if "const" in field else "")
        )
    return result


def prepare_draft(raw: str | Mapping[str, Any]) -> PreparedDraft:
    """Capture the input before any validation or explicit path normalization."""
    raw_text = raw if isinstance(raw, str) else canonical_json(raw)
    encoded = raw_text.encode("utf-8", errors="surrogatepass")
    prepared = PreparedDraft(
        raw_text, hashlib.sha256(encoded).hexdigest(), len(encoded)
    )
    issues = []
    try:
        raw_text.encode("utf-8")
    except UnicodeError:
        issues.append(
            DraftIssue("direction", "invalid_utf8", "Draft must be valid UTF-8 text")
        )
    if len(encoded) > NATIVE_DRAFT_MAX_BYTES:
        issues.append(
            DraftIssue(
                "direction",
                "wire_limit",
                f"Draft exceeds {NATIVE_DRAFT_MAX_BYTES} UTF-8 bytes; exact text retained for audit",
            )
        )
    if issues:
        return replace(prepared, issues=tuple(issues))

    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                issues.append(
                    DraftIssue(
                        f"json.{key}", "duplicate_key", f"Duplicate JSON key {key!r}"
                    )
                )
            value[key] = item
        return value

    def nonfinite(value):
        raise ValueError(f"Nonfinite JSON constant {value} is not allowed")

    try:
        payload = json.loads(
            raw_text, object_pairs_hook=unique_object, parse_constant=nonfinite
        )
    except (ValueError, TypeError) as exc:
        issues.append(
            DraftIssue(
                "direction", "invalid_json", f"Draft must be one JSON object: {exc}"
            )
        )
        return replace(prepared, issues=tuple(issues))
    if not isinstance(payload, dict):
        issues.append(
            DraftIssue("direction", "not_object", "Draft must be one JSON object")
        )
        return replace(prepared, issues=tuple(issues))
    return replace(prepared, normalized_args=deep_freeze(payload), issues=tuple(issues))


def validate_draft(
    raw: str | Mapping[str, Any] | PreparedDraft,
    *,
    request: StructuredProposalRequest,
    allowed_evidence_refs: frozenset[str],
    allowed_scope_refs: frozenset[str],
    catalog: DirectionAliasCatalog | None = None,
    evidence_catalog=None,
    contract: str = STRUCTURED_PROPOSER_CONTRACT,
    max_evidence_refs: int = 4,
) -> PreparedDraft:
    """Collect all independently checkable errors, then use the existing parser.

    Invalid syntax cannot support semantic checks. Scope/evidence/catalogue
    integrity failures from the trusted caller are not treated as model repairs.
    """
    prepared = prepare_draft(raw.raw_text if isinstance(raw, PreparedDraft) else raw)
    if prepared.normalized_args is None:
        return prepared
    expected_catalog = build_direction_alias_catalog(request, contract=contract)
    if catalog is not None and catalog != expected_catalog:
        raise ValueError("draft catalogue differs from the request's trusted catalogue")
    catalog = expected_catalog
    evidence_catalog = (
        _evidence_catalog(allowed_evidence_refs, request.request_id, contract=contract)
        if evidence_catalog is None
        else tuple(evidence_catalog)
    )
    if {item.canonical_ref for item in evidence_catalog} != set(allowed_evidence_refs):
        raise ValueError("evidence catalogue differs from session-seen refs")
    if len({item.ref for item in evidence_catalog}) != len(evidence_catalog):
        raise ValueError("evidence catalogue has duplicate aliases")
    if len({item.canonical_ref for item in evidence_catalog}) != len(evidence_catalog):
        raise ValueError("evidence catalogue has duplicate canonical refs")
    if type(max_evidence_refs) is not int or not 1 <= max_evidence_refs <= 64:
        raise ValueError("max_evidence_refs must be in 1..64")
    payload = json.loads(canonical_json(prepared.normalized_args))
    issues = list(prepared.issues)
    normalizations = []

    def add(field, code, message):
        issues.append(DraftIssue(field, code, message))

    # The established native empty-creation default is explicit and audited.
    if "new_paths" not in payload:
        payload["new_paths"] = []
        normalizations.append({"field": "new_paths", "operation": "default_empty_list"})
    for name in sorted(_FIELDS - set(payload)):
        add(name, "missing_field", f"Missing required field {name}")
    for name in sorted(set(payload) - _FIELDS):
        add(name, "unknown_field", f"Unknown direction field {name}")

    def text(value, field, maximum, *, allow_aliases=False):
        if not isinstance(value, str) or not value.strip():
            add(field, "invalid_text", f"{field} must be non-empty text")
            return None
        stripped = value.strip()
        if stripped != value:
            normalizations.append(
                {
                    "field": field,
                    "operation": "trim_surrounding_whitespace",
                    "before_chars": len(value),
                    "after_chars": len(stripped),
                }
            )
        if len(stripped) > maximum:
            add(
                field,
                "text_limit",
                f"{field} exceeds hard storage limit {maximum} characters",
            )
        try:
            stripped.encode("utf-8")
        except UnicodeError:
            add(field, "invalid_utf8", f"{field} must be valid UTF-8 text")
        if not allow_aliases and _LOCAL_ALIAS_TOKEN.search(stripped):
            add(
                field,
                "alias_in_prose",
                f"{field} contains request-local aliases; put citations in the reference fields without rewriting the intent",
            )
        return stripped

    for name, maximum in zip(TEXT_FIELDS, NATIVE_TEXT_LIMITS):
        if name in payload:
            value = text(payload[name], name, maximum)
            if value is not None:
                payload[name] = value
    if (
        request.arm_spec is not None
        and payload.get("failure_mechanism") != request.arm_spec.failure_mechanism
    ):
        add(
            "failure_mechanism",
            "fixed_family_drift",
            "ARM_REALIZATION_DRIFT: failure_mechanism must exactly match the frozen UCB family",
        )

    def refs(name, maximum):
        if name not in payload:
            return []
        value = payload[name]
        if not isinstance(value, list):
            add(name, "invalid_list", f"{name} must be an array")
            return []
        if not 1 <= len(value) <= maximum:
            add(name, "list_limit", f"{name} must contain 1..{maximum} items")
        result = []
        for index, item in enumerate(value):
            parsed = text(item, f"{name}[{index}]", 1024, allow_aliases=True)
            if parsed is not None:
                result.append(parsed)
        if len(result) != len(set(result)):
            add(name, "duplicate_refs", f"{name} contains duplicates")
        payload[name] = result
        return result

    scope_refs = refs("scope_refs", request.max_scope_refs)
    scope_by_ref = {
        item.ref: item
        for item in catalog.scopes
        if item.selectable and item.ref in allowed_scope_refs
    }
    for ref in sorted(set(scope_refs) - scope_by_ref.keys()):
        add(
            "scope_refs",
            "unknown_scope_ref",
            f"Unissued or unselectable scope ref: {ref}",
        )
    raw_evidence = refs("evidence_refs", max_evidence_refs)
    evidence_aliases = {item.ref for item in evidence_catalog}
    for ref in sorted(set(raw_evidence) - evidence_aliases):
        hint = (
            "; this is an edit scope, not evidence. Use the evidence citation returned by a source read"
            if any(item.ref == ref for item in catalog.scopes) else ""
        )
        add(
            "evidence_refs",
            "unknown_evidence_ref",
            f"Unissued evidence ref: {ref}{hint}; no reference was silently dropped",
        )

    roots = {item.ref: item for item in catalog.roots}
    parent_paths = frozenset(item.path for item in request.repository_map.files)
    created = []
    destinations = []
    moved_entries = set()
    if not isinstance(payload["new_paths"], list):
        add("new_paths", "invalid_list", "new_paths must be an array")
    else:
        if len(payload["new_paths"]) > request.max_new_paths:
            add(
                "new_paths",
                "list_limit",
                f"new_paths must contain at most {request.max_new_paths} items",
            )
        for index, item in enumerate(payload["new_paths"]):
            field = f"new_paths[{index}]"
            if not isinstance(item, dict):
                add(field, "invalid_object", f"{field} must be an object")
                continue
            before = len(issues)
            required = {"root_ref", "relative_path", "role"}
            for name in sorted(required - set(item)):
                add(f"{field}.{name}", "missing_field", f"Missing {field}.{name}")
            for name in sorted(set(item) - required):
                add(f"{field}.{name}", "unknown_field", f"Unknown {field}.{name}")
            root_ref = (
                text(
                    item.get("root_ref"), field + ".root_ref", 1024, allow_aliases=True
                )
                if "root_ref" in item
                else None
            )
            root = roots.get(root_ref)
            if root_ref is not None and root is None:
                add(
                    field + ".root_ref",
                    "unknown_root",
                    f"{field} references an unissued creation root",
                )
            if "role" in item:
                role = text(item["role"], field + ".role", NEW_PATH_ROLE_MAX_CHARS)
                if role is not None:
                    item["role"] = role
            if "relative_path" in item:
                try:
                    relative = _relative_new_path(
                        item["relative_path"], field + ".relative_path"
                    )
                    relative.encode("utf-8")
                    if root is not None and relative.startswith(root.path + "/"):
                        suffix = relative[len(root.path) + 1 :]
                        if suffix.startswith(root.path + "/"):
                            add(
                                field + ".relative_path",
                                "repeated_root_prefix",
                                f"{field} repeats the trusted root prefix; supply one unambiguous root-relative path",
                            )
                        else:
                            normalizations.append(
                                {
                                    "field": field + ".relative_path",
                                    "operation": "repository_relative_to_root_relative",
                                    "before": relative,
                                    "after": suffix,
                                    "resolved_path": relative,
                                    "root_ref": root.ref,
                                }
                            )
                            relative = suffix
                    item["relative_path"] = relative
                    if root is not None:
                        resolved = root.path + "/" + relative
                        destinations.append(resolved)
                        if resolved not in parent_paths and not relative.endswith(root.allowed_suffixes):
                            add(
                                field + ".relative_path",
                                "invalid_suffix",
                                f"{field} has a disallowed suffix",
                            )
                        if resolved in parent_paths:
                            editable = [row for row in scope_by_ref.values()
                                        if row.target.kind is ScopeKind.FILE and row.target.path == resolved]
                            if len(editable) == 1 and len(issues) == before:
                                moved = editable[0]
                                if moved.ref not in scope_refs:
                                    scope_refs.append(moved.ref)
                                moved_entries.add(index)
                                # A creation role may carry instructions absent from
                                # the main direction. Preserve that text for the worker.
                                role = item.get("role", "")
                                direction_text = payload.get("implementation_direction")
                                if isinstance(direction_text, str) and role not in direction_text:
                                    payload["implementation_direction"] += f"\nExisting file role ({resolved}): {role}"
                                normalizations.append({
                                    "field": field, "operation": "new_path_moved_to_scope",
                                    "resolved_path": resolved, "scope_ref": moved.ref,
                                    "original_entry": dict(item),
                                })
                            else:
                                add(field + ".relative_path", "existing_path",
                                    f"{field} already exists but is not an unambiguous issued editable file; choose an allowed scope")
                except (StructuredProposalParseError, UnicodeError) as exc:
                    add(field + ".relative_path", "unsafe_path", str(exc))
            if len(issues) == before and index not in moved_entries:
                try:
                    created.extend(
                        _new_path_entries(
                            [item],
                            catalog=catalog,
                            maximum=1,
                            parent_paths=parent_paths,
                        )
                    )
                except StructuredProposalParseError as exc:
                    add(field, "invalid_new_path", str(exc))
        if len(destinations) != len(set(destinations)):
            add(
                "new_paths",
                "duplicate_destinations",
                "new_paths resolves to duplicate destinations",
            )
        payload["new_paths"] = [item for index, item in enumerate(payload["new_paths"])
                                if index not in moved_entries]
    if len(scope_refs) > request.max_scope_refs:
        add("scope_refs", "list_limit", "Corrected scope exceeds max_scope_refs")
    if isinstance(payload.get("implementation_direction"), str) and len(payload["implementation_direction"]) > NATIVE_TEXT_LIMITS[2]:
        add("implementation_direction", "text_limit", "Corrected direction exceeds hard storage limit")

    selected = [scope_by_ref[ref] for ref in set(scope_refs) if ref in scope_by_ref]
    existing = {path for item in selected for path in item.resolved_paths}
    if len(existing) > request.max_resolved_scope_files:
        add(
            "scope_refs",
            "scope_file_limit",
            "Scope expands beyond max_resolved_scope_files",
        )
    sizes = _repository_file_bytes(request.repository_map)
    if sum(sizes[path] for path in existing) > request.max_resolved_scope_bytes:
        add(
            "scope_refs",
            "scope_byte_limit",
            "Scope expands beyond max_resolved_scope_bytes",
        )
    if (
        len(existing | {item.path for item in created})
        > request.max_resolved_scope_files
    ):
        add(
            "new_paths",
            "resolved_file_limit",
            "Scope including new paths exceeds max_resolved_scope_files",
        )

    direction = None
    if not issues:
        try:
            direction = parse_trace_aware_direction(
                canonical_json(payload),
                request=request,
                allowed_evidence_refs=allowed_evidence_refs,
                allowed_scope_refs=allowed_scope_refs,
                evidence_catalog=evidence_catalog,
                contract=contract,
                text_limits=NATIVE_TEXT_LIMITS,
                max_response_bytes=NATIVE_DRAFT_MAX_BYTES,
            )
            if any(getattr(direction, name) != payload[name] for name in TEXT_FIELDS):
                add(
                    "direction",
                    "text_rewrite",
                    "Parser changed full intent text; draft was not delivered",
                )
                direction = None
            elif (
                len(canonical_json(direction).encode("utf-8"))
                > NATIVE_DIRECTION_MAX_BYTES
            ):
                add(
                    "direction",
                    "typed_direction_byte_limit",
                    f"Resolved typed direction exceeds {NATIVE_DIRECTION_MAX_BYTES} UTF-8 JSON bytes for worker delivery; exact raw intent retained for correction",
                )
                direction = None
        except (StructuredProposalParseError, ValueError) as exc:
            add("direction", "resolution_error", str(exc))
    return replace(
        prepared,
        normalized_args=deep_freeze(payload),
        issues=tuple(issues),
        normalizations=tuple(deep_freeze(item) for item in normalizations),
        direction=direction,
    )


def parse_draft(raw, **kwargs) -> StructuredDirection:
    """Deliver the same typed direction or raise a draft-carrying retry error."""
    return validate_draft(raw, **kwargs).require_direction()


__all__ = [
    "NATIVE_DIRECTION_MAX_BYTES",
    "NATIVE_DRAFT_MAX_BYTES",
    "NATIVE_TEXT_LIMITS",
    "PRESENTATION_TARGETS",
    "DraftIssue",
    "PreparedDraft",
    "native_draft_schema",
    "parse_draft",
    "prepare_draft",
    "validate_draft",
]
