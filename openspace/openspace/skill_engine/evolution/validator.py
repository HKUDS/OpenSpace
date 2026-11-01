"""Deterministic validator for staged skill evolution edits."""

from __future__ import annotations

import difflib
import inspect
import re
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from openspace.skill_engine.capture_contract import (
    capture_contract_ref_ids,
    normalize_capture_contract,
)
from openspace.skill_engine.evidence import EvidencePacket, ResourceRef
from openspace.skill_engine.evidence.redaction import contains_secret
from openspace.skill_engine.evolution.authoring_contract import (
    contract_validation_failures,
)
from openspace.skill_engine.skill_utils import (
    SKILL_FILENAME,
    parse_frontmatter,
)
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

_OUTCOMES = {"approve", "reject", "needs_human_review"}
_MAX_SKILL_NAME_LENGTH = 50
_SAFE_SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_HIGH_RISK_RUNTIME_OVERLAY_FIELDS = {
    "allowed-tools",
    "disable-model-invocation",
    "user-invocable",
    "model",
    "effort",
    "hooks",
    "context",
    "agent",
    "shell",
}
_RUNTIME_OVERLAY_FIELD_ALIASES = {
    "allowed_tools": "allowed-tools",
    "allowedTools": "allowed-tools",
    "disable_model_invocation": "disable-model-invocation",
    "disableModelInvocation": "disable-model-invocation",
    "user_invocable": "user-invocable",
    "userInvocable": "user-invocable",
}
_FORBIDDEN_FRONTMATTER_KEYS = _HIGH_RISK_RUNTIME_OVERLAY_FIELDS
_LOCAL_FORBIDDEN_FILENAMES = {
    ".env",
    ".skill_id",
    ".cloud_skill.json",
    ".upload_meta.json",
}
_CAUSAL_REF_TYPES = {
    "runtime_snapshot",
    "transcript_message",
    "tool_event",
    "tool_result",
    "tool_incident",
    "file_history",
    "skill_event",
    "manual_request_ref",
}
_CAPTURE_FALLBACK_ONLY_TYPES = {
    "memory_ref",
    "recording_ref",
    "background_task_result",
    "compact_summary",
}
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\bAuthorization\s*:\s*(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\bCookie\s*:\s*[^;\n]+(?:;[^\n]+)?"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(
        r"(?i)\b[A-Z0-9_]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*"
        r"\s*[:=]\s*[^\s\"']+"
    ),
)


@dataclass(frozen=True, slots=True)
class ValidationResult:
    validation_id: str
    authoring_id: str
    decision_id: str
    packet_id: str
    outcome: str
    deterministic_failures: list[str] = field(default_factory=list)
    semantic_warnings: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    provenance_refs: list[str] = field(default_factory=list)
    checked_at: str = ""
    checked_by: str = "deterministic_validator"

    @property
    def passed(self) -> bool:
        return self.outcome == "approve"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ValidationResult":
        outcome = str(data.get("outcome") or "reject").strip().lower()
        if outcome not in _OUTCOMES:
            outcome = "reject"
        return cls(
            validation_id=str(data.get("validation_id") or ""),
            authoring_id=str(data.get("authoring_id") or ""),
            decision_id=str(data.get("decision_id") or ""),
            packet_id=str(data.get("packet_id") or ""),
            outcome=outcome,
            deterministic_failures=_str_list(data.get("deterministic_failures")),
            semantic_warnings=_str_list(data.get("semantic_warnings")),
            changed_files=_str_list(data.get("changed_files")),
            provenance_refs=_str_list(data.get("provenance_refs")),
            checked_at=str(data.get("checked_at") or ""),
            checked_by=str(data.get("checked_by") or "deterministic_validator"),
        )


class EvolutionValidator:
    """Commit-preflight validator for staged skill edits.

    The validator is intentionally deterministic-first. Optional semantic
    review can add warnings or make the outcome stricter, but cannot override
    hard deterministic failures.
    """

    def __init__(
        self,
        *,
        evidence_store: Any | None = None,
        skill_store: Any | None = None,
        registry: Any | None = None,
        semantic_validator: Any | None = None,
        semantic_enabled: bool = False,
        checked_by: str = "deterministic_validator",
    ) -> None:
        self.evidence_store = evidence_store
        self.skill_store = skill_store
        self.registry = registry
        self.semantic_validator = semantic_validator
        self.semantic_enabled = bool(semantic_enabled)
        self.checked_by = checked_by

    def validate(
        self,
        authoring: Any,
        validator_packet: EvidencePacket,
        decision: Any,
        admission: Any,
    ) -> ValidationResult:
        result = self._run_validation(
            authoring,
            validator_packet,
            decision,
            admission,
            run_semantic=True,
        )
        return self._persist_fail_closed(result)

    async def validate_async(
        self,
        authoring: Any,
        validator_packet: EvidencePacket,
        decision: Any,
        admission: Any,
    ) -> ValidationResult:
        result = self._run_validation(
            authoring,
            validator_packet,
            decision,
            admission,
            run_semantic=False,
        )
        if (
            not result.deterministic_failures
            and self.semantic_enabled
            and self.semantic_validator is not None
        ):
            outcome, warnings = await self._run_semantic_validator_async(
                result.outcome,
                result.semantic_warnings,
                authoring=authoring,
                validator_packet=validator_packet,
                decision=decision,
                admission=admission,
            )
            result = replace(
                result,
                outcome=outcome,
                semantic_warnings=warnings,
                checked_by=f"{self.checked_by}+semantic",
            )
        return self._persist_fail_closed(result)

    def _run_validation(
        self,
        authoring: Any,
        validator_packet: EvidencePacket,
        decision: Any,
        admission: Any,
        *,
        run_semantic: bool,
    ) -> ValidationResult:
        validation_id = f"val_{uuid.uuid4().hex}"
        checked_at = _utc_now()
        try:
            return self._validate(
                validation_id=validation_id,
                checked_at=checked_at,
                authoring=authoring,
                validator_packet=validator_packet,
                decision=decision,
                admission=admission,
                run_semantic=run_semantic,
            )
        except Exception as exc:
            logger.debug("Evolution validation failed internally", exc_info=True)
            return ValidationResult(
                validation_id=validation_id,
                authoring_id=str(_attr(authoring, "authoring_id") or ""),
                decision_id=str(
                    _attr(authoring, "decision_id")
                    or _attr(decision, "decision_id")
                    or ""
                ),
                packet_id=str(_attr(validator_packet, "packet_id") or ""),
                outcome="reject",
                deterministic_failures=["validator_internal_error"],
                semantic_warnings=[str(exc)[:500]],
                changed_files=_changed_files(_attr(authoring, "staged_edit")),
                provenance_refs=_provenance_refs(
                    authoring,
                    validator_packet,
                    decision,
                    admission,
                ),
                checked_at=checked_at,
                checked_by=self.checked_by,
            )

    def _persist_fail_closed(self, result: ValidationResult) -> ValidationResult:
        try:
            self._persist(result)
        except Exception as exc:
            logger.debug(
                "Failed to persist validation result %s",
                result.validation_id,
                exc_info=True,
            )
            result = replace(
                result,
                outcome="reject",
                deterministic_failures=_dedupe(
                    [*result.deterministic_failures, "validation_persist_failed"]
                ),
                semantic_warnings=_dedupe(
                    [*result.semantic_warnings, str(exc)[:500]]
                ),
            )
            try:
                self._persist(result)
            except Exception:
                logger.debug(
                    "Failed to persist validation reject result %s",
                    result.validation_id,
                    exc_info=True,
                )
        return result

    def _validate(
        self,
        *,
        validation_id: str,
        checked_at: str,
        authoring: Any,
        validator_packet: EvidencePacket,
        decision: Any,
        admission: Any,
        run_semantic: bool = True,
    ) -> ValidationResult:
        staged = _attr(authoring, "staged_edit")
        action = _action_type(decision, staged)
        failures: list[str] = []
        review_warnings: list[str] = []
        changed_files = _changed_files(staged)
        provenance_refs = _provenance_refs(
            authoring,
            validator_packet,
            decision,
            admission,
        )

        if str(_attr(authoring, "status") or "").strip().lower() != "staged":
            failures.append("authoring_not_staged")
        if staged is None:
            failures.append("missing_staged_edit")

        if str(getattr(validator_packet, "packet_type", "") or "").lower() != "validator":
            failures.append("validator_packet_required")

        if staged is not None:
            snapshot = _snapshot(staged)
            failures.extend(self._schema_failures(snapshot, action, admission))
            failures.extend(
                self._scope_failures(
                    staged,
                    validator_packet,
                    action,
                    decision,
                    changed_files,
                    snapshot,
                )
            )
            failures.extend(
                self._provenance_failures(
                    staged,
                    validator_packet,
                    decision,
                    admission,
                    action,
                )
            )
            failures.extend(
                self._capture_contract_failures(
                    staged,
                    action,
                    decision,
                    admission,
                )
            )
            failures.extend(self._secret_failures(snapshot, validator_packet))
            failures.extend(self._duplicate_failures(staged, action))
            failures.extend(contract_validation_failures(staged, action))
            review_warnings.extend(self._overlay_review_warnings(staged, admission))

        failures = _dedupe(failures)
        semantic_warnings = _dedupe(review_warnings)
        outcome = "reject" if failures else (
            "needs_human_review" if semantic_warnings else "approve"
        )

        if (
            run_semantic
            and not failures
            and self.semantic_enabled
            and self.semantic_validator is not None
        ):
            outcome, semantic_warnings = self._run_semantic_validator(
                outcome,
                semantic_warnings,
                authoring=authoring,
                validator_packet=validator_packet,
                decision=decision,
                admission=admission,
            )

        return ValidationResult(
            validation_id=validation_id,
            authoring_id=str(_attr(authoring, "authoring_id") or ""),
            decision_id=str(
                _attr(authoring, "decision_id")
                or _attr(decision, "decision_id")
                or ""
            ),
            packet_id=validator_packet.packet_id,
            outcome=outcome,
            deterministic_failures=failures,
            semantic_warnings=semantic_warnings,
            changed_files=changed_files,
            provenance_refs=provenance_refs,
            checked_at=checked_at,
            checked_by=self.checked_by,
        )

    def _schema_failures(
        self,
        snapshot: dict[str, str],
        action: str,
        admission: Any,
    ) -> list[str]:
        failures: list[str] = []
        skill_md = snapshot.get(SKILL_FILENAME)
        if skill_md is None:
            return ["missing_skill_md"]

        frontmatter, frontmatter_error = _strict_frontmatter(skill_md)
        if frontmatter_error:
            failures.append(frontmatter_error)
            return failures
        if _has_secondary_frontmatter(skill_md):
            failures.append("multiple_frontmatter_blocks")

        name = str(frontmatter.get("name") or "").strip()
        if not name:
            failures.append("missing_skill_name")
        elif len(name) > _MAX_SKILL_NAME_LENGTH:
            failures.append("skill_name_too_long")
        elif not _SAFE_SKILL_NAME_RE.match(name) or _sanitize_skill_name(name) != name:
            failures.append("skill_name_not_sanitized")

        description = str(frontmatter.get("description") or "").strip()
        if action in {"DERIVED", "CAPTURED"} and not description:
            failures.append("missing_description")

        allowed_keys = _allowed_frontmatter_keys(admission)
        for key in sorted(str(item) for item in frontmatter):
            normalized_key = _normalize_overlay_key(key)
            if (
                normalized_key in _FORBIDDEN_FRONTMATTER_KEYS
                and normalized_key not in allowed_keys
            ):
                failures.append(f"forbidden_frontmatter_key:{normalized_key}")

        return failures

    def _capture_contract_failures(
        self,
        staged: Any,
        action: str,
        decision: Any,
        admission: Any,
    ) -> list[str]:
        if action != "CAPTURED":
            return []

        failures: list[str] = []
        if not bool(_attr(admission, "source_validation_passed", False)):
            failures.append("captured_source_validation_not_admitted")
        decision_contract = normalize_capture_contract(
            _attr(decision, "proposal_contract")
        )
        staged_contract = normalize_capture_contract(
            _mapping_or_empty(_attr(staged, "apply_metadata")).get(
                "proposal_contract"
            )
        )
        if not decision_contract:
            failures.append("captured_missing_decision_contract")
        if not staged_contract:
            failures.append("captured_missing_staged_contract")
        elif staged_contract != decision_contract:
            failures.append("captured_contract_changed_during_authoring")

        staged_refs = set(_str_list(_attr(staged, "evidence_refs")))
        if not set(capture_contract_ref_ids(decision_contract)).issubset(staged_refs):
            failures.append("captured_contract_refs_missing_from_provenance")
        return failures

    def _scope_failures(
        self,
        staged: Any,
        packet: EvidencePacket,
        action: str,
        decision: Any,
        changed_files: list[str],
        snapshot: dict[str, str],
    ) -> list[str]:
        failures: list[str] = []
        diff_paths = _diff_paths(str(_attr(staged, "content_diff") or ""))
        expected_changed = diff_paths or set(snapshot)
        if set(changed_files) != expected_changed:
            failures.append("changed_files_mismatch")

        paths_to_check = set(changed_files) | set(snapshot) | diff_paths
        for rel_path in sorted(paths_to_check):
            if not _safe_relative_path(rel_path):
                failures.append(f"path_escape:{rel_path}")
                continue
            if _forbidden_local_path(rel_path):
                failures.append(f"forbidden_changed_file:{rel_path}")

        target_dir = _resolved_path(_attr(staged, "target_dir"))
        if target_dir is None:
            failures.append("missing_target_dir")
            return failures

        if action == "FIX":
            target_skill_ids = _target_skill_ids(decision, staged)
            if len(target_skill_ids) != 1:
                failures.append("fix_requires_single_target")
            expected_dirs = _target_skill_dirs(packet, target_skill_ids)
            if not expected_dirs:
                failures.append("missing_target_skill_ref")
            elif target_dir not in expected_dirs:
                failures.append("fix_target_dir_mismatch")
            return failures

        if action in {"DERIVED", "CAPTURED"}:
            allowed_roots = _allowed_skill_roots(packet, action, decision)
            if allowed_roots and not any(
                target_dir.parent == root or root in target_dir.parent.parents
                for root in allowed_roots
            ):
                failures.append("target_dir_outside_allowed_skill_root")
        return failures

    def _provenance_failures(
        self,
        staged: Any,
        packet: EvidencePacket,
        decision: Any,
        admission: Any,
        action: str,
    ) -> list[str]:
        failures: list[str] = []
        if action == "NOOP":
            return failures

        staged_refs = _str_list(_attr(staged, "evidence_refs"))
        if not staged_refs:
            failures.append("missing_provenance_refs")

        packet_refs = _packet_refs(packet)
        decision_refs = set(_decision_ref_ids(decision))
        admission_refs = set(_admission_ref_ids(admission))
        allowed_refs = set(packet_refs) | decision_refs | admission_refs
        allowed_refs.update(
            {
                f"packet:{packet.packet_id}",
                f"decision:{_attr(decision, 'decision_id') or ''}",
                f"authoring:{_attr(staged, 'authoring_id') or ''}",
            }
        )
        for ref_id in staged_refs:
            if ref_id and ref_id not in allowed_refs:
                failures.append(f"missing_provenance_ref:{ref_id}")

        apply_metadata = _mapping_or_empty(_attr(staged, "apply_metadata"))
        authoring_packet_id = str(apply_metadata.get("action_packet_id") or "")
        if not authoring_packet_id:
            authoring_packet_id = str(_attr(staged, "packet_id") or "")
        if not authoring_packet_id:
            authoring_packet_id = str(_attr(staged, "source_packet_id") or "")
        source_packet_ids = {
            str(ref.metadata.get("packet_id") or ref.ref_id.removeprefix("packet:"))
            for ref in packet_refs.values()
            if ref.ref_type == "evidence_packet_ref"
        }
        if authoring_packet_id and authoring_packet_id not in source_packet_ids:
            failures.append("missing_action_packet_ref")
        if authoring_packet_id:
            for ref in packet_refs.values():
                if ref.ref_type != "evidence_packet_ref":
                    continue
                packet_id = str(
                    ref.metadata.get("packet_id") or ref.ref_id.removeprefix("packet:")
                )
                if packet_id != authoring_packet_id:
                    continue
                source_watermark = _int_or_none(ref.metadata.get("manifest_watermark"))
                if (
                    source_watermark is not None
                    and int(packet.manifest_watermark) < source_watermark
                ):
                    failures.append("validator_packet_watermark_regressed")
                break

        if action == "FIX":
            target_skill_ids = _target_skill_ids(decision, staged)
            if not _target_skill_dirs(packet, target_skill_ids):
                failures.append("fix_missing_target_skill_refs")
            referenced = set(staged_refs) | decision_refs | admission_refs
            has_causal_ref = any(
                ref.ref_id in referenced and ref.ref_type in _CAUSAL_REF_TYPES
                for ref in packet_refs.values()
            )
            if not has_causal_ref:
                failures.append("fix_missing_causality_refs")

        if action == "CAPTURED":
            primary_refs = [
                ref
                for ref in packet_refs.values()
                if str(ref.role).lower() == "primary"
            ]
            if not any(
                ref.ref_type not in _CAPTURE_FALLBACK_ONLY_TYPES
                for ref in primary_refs
            ):
                failures.append("captured_requires_primary_non_fallback_refs")

        return failures

    def _secret_failures(
        self,
        snapshot: dict[str, str],
        packet: EvidencePacket,
    ) -> list[str]:
        failures: list[str] = []
        for rel_path, content in snapshot.items():
            if _content_contains_secret(content):
                failures.append(f"secret_in_proposed_content:{rel_path}")

        proposed_text = "\n".join(snapshot.values())
        for ref in _packet_refs(packet).values():
            if not ref.contains_secret:
                continue
            preview = str(ref.preview or "").strip()
            if len(preview) >= 40 and preview in proposed_text:
                failures.append(f"sensitive_ref_copied_wholesale:{ref.ref_id}")
        return failures

    def _duplicate_failures(self, staged: Any, action: str) -> list[str]:
        if action not in {"CAPTURED", "DERIVED"}:
            return []

        proposed_name = str(_attr(staged, "proposed_name") or "").strip()
        proposed_description = str(_attr(staged, "proposed_description") or "").strip()
        proposed_tags = _frontmatter_tags(_snapshot(staged).get(SKILL_FILENAME, ""))
        skip_ids = set(_str_list(_attr(staged, "parent_skill_ids")))
        skip_ids.update(_str_list(_attr(staged, "target_skill_ids")))
        proposed_skill_id = str(_attr(staged, "proposed_skill_id") or "")
        if proposed_skill_id:
            skip_ids.add(proposed_skill_id)

        failures: list[str] = []
        for existing in self._existing_skills():
            skill_id = str(existing.get("skill_id") or "")
            if skill_id and skill_id in skip_ids:
                continue
            if _is_duplicate_skill(
                proposed_name=proposed_name,
                proposed_description=proposed_description,
                proposed_tags=proposed_tags,
                existing=existing,
            ):
                identifier = skill_id or str(existing.get("name") or "unknown")
                failures.append(f"duplicate_skill:{identifier}")
        return failures

    def _overlay_review_warnings(self, staged: Any, admission: Any) -> list[str]:
        allowed = _allowed_overlay_fields(admission)
        overlay_fields = _mapping_or_empty(_attr(staged, "overlay_fields"))
        warnings: list[str] = []
        for key in sorted(overlay_fields):
            normalized_key = _normalize_overlay_key(key)
            if (
                normalized_key in _HIGH_RISK_RUNTIME_OVERLAY_FIELDS
                and normalized_key not in allowed
            ):
                warnings.append(f"high_risk_overlay_field:{normalized_key}")
        return warnings

    def _run_semantic_validator(
        self,
        outcome: str,
        semantic_warnings: list[str],
        *,
        authoring: Any,
        validator_packet: EvidencePacket,
        decision: Any,
        admission: Any,
    ) -> tuple[str, list[str]]:
        try:
            response = self._call_semantic_reviewer(
                authoring,
                validator_packet,
                decision,
                admission,
            )
            if inspect.isawaitable(response):
                close = getattr(response, "close", None)
                if callable(close):
                    close()
                action = _action_type(decision, _attr(authoring, "staged_edit"))
                next_outcome = (
                    "needs_human_review" if action == "CAPTURED" else outcome
                )
                return next_outcome, _dedupe(
                    [*semantic_warnings, "semantic_async_reviewer_requires_async_validation"]
                )
        except Exception as exc:
            action = _action_type(decision, _attr(authoring, "staged_edit"))
            warning = f"semantic_validator_error:{str(exc)[:160]}"
            next_outcome = "needs_human_review" if action == "CAPTURED" else outcome
            return next_outcome, _dedupe([*semantic_warnings, warning])

        return _apply_semantic_response(outcome, semantic_warnings, response)

    async def _run_semantic_validator_async(
        self,
        outcome: str,
        semantic_warnings: list[str],
        *,
        authoring: Any,
        validator_packet: EvidencePacket,
        decision: Any,
        admission: Any,
    ) -> tuple[str, list[str]]:
        try:
            response = self._call_semantic_reviewer(
                authoring,
                validator_packet,
                decision,
                admission,
            )
            if inspect.isawaitable(response):
                response = await response
        except Exception as exc:
            action = _action_type(decision, _attr(authoring, "staged_edit"))
            warning = f"semantic_validator_error:{str(exc)[:160]}"
            next_outcome = "reject" if action == "CAPTURED" else outcome
            return next_outcome, _dedupe([*semantic_warnings, warning])
        return _apply_semantic_response(outcome, semantic_warnings, response)

    def _call_semantic_reviewer(
        self,
        authoring: Any,
        validator_packet: EvidencePacket,
        decision: Any,
        admission: Any,
    ) -> Any:
        reviewer = self.semantic_validator
        if reviewer is None:
            return {"outcome": "approve", "warnings": []}
        if callable(reviewer):
            return reviewer(authoring, validator_packet, decision, admission)
        method = getattr(reviewer, "validate", None)
        if not callable(method):
            raise TypeError("semantic validator has no callable validate method")
        return method(authoring, validator_packet, decision, admission)

    def _existing_skills(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        if self.skill_store is not None:
            load_all = getattr(self.skill_store, "load_all", None)
            if callable(load_all):
                try:
                    for record in load_all(active_only=True).values():
                        result.append(_skill_record_mapping(record))
                except Exception:
                    logger.debug("Validator SkillStore lookup failed", exc_info=True)
        if self.registry is not None:
            list_skills = getattr(self.registry, "list_skills", None)
            if callable(list_skills):
                try:
                    for meta in list_skills():
                        result.append(_skill_record_mapping(meta))
                except Exception:
                    logger.debug("Validator registry lookup failed", exc_info=True)
        deduped: dict[str, dict[str, Any]] = {}
        for item in result:
            key = str(item.get("skill_id") or item.get("path") or item.get("name") or "")
            if key:
                deduped[key] = item
        return list(deduped.values())

    def _persist(self, result: ValidationResult) -> None:
        persist = getattr(self.evidence_store, "persist_validation", None)
        if not callable(persist):
            return
        persist(result)


def _apply_semantic_response(
    outcome: str,
    semantic_warnings: list[str],
    response: Any,
) -> tuple[str, list[str]]:
    next_outcome = outcome
    warnings = list(semantic_warnings)
    if isinstance(response, Mapping):
        warnings.extend(_str_list(response.get("warnings")))
        requested = str(response.get("outcome") or "").strip().lower()
        if requested in {"reject", "needs_human_review"}:
            next_outcome = requested
    elif isinstance(response, ValidationResult):
        warnings.extend(response.semantic_warnings)
        if response.outcome in {"reject", "needs_human_review"}:
            next_outcome = response.outcome
    elif isinstance(response, str) and response.strip():
        requested = response.strip().lower()
        if requested in {"reject", "needs_human_review"}:
            next_outcome = requested
        elif requested not in {"approve", "passed", "ok"}:
            warnings.append(requested)
    return next_outcome, _dedupe(warnings)


def _strict_frontmatter(content: str) -> tuple[dict[str, Any], str | None]:
    if not content.startswith("---"):
        return {}, "missing_frontmatter"
    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return {}, "invalid_frontmatter"
    raw = match.group(1)
    try:
        import yaml

        parsed = yaml.safe_load(raw) or {}
        if not isinstance(parsed, dict):
            return {}, "invalid_frontmatter"
        return dict(parsed), None
    except Exception:
        parsed = parse_frontmatter(content)
        if not parsed:
            return {}, "invalid_frontmatter"
        # The fallback parser is intentionally permissive. Treat values that
        # leave obvious unbalanced YAML collection markers as malformed.
        for value in parsed.values():
            text = str(value).strip()
            if text in {"[", "{", "]", "}"}:
                return {}, "invalid_frontmatter"
        return parsed, None


def _has_secondary_frontmatter(content: str) -> bool:
    first = re.match(r"^---\n.*?\n---(?:\n|$)", content, re.DOTALL)
    if first is None:
        return False
    lines = content[first.end():].splitlines()
    fenced = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fenced = not fenced
            continue
        if fenced or stripped != "---":
            continue
        block = lines[index + 1:index + 14]
        if any(re.match(r"^(name|description)\s*:", item.strip()) for item in block):
            return True
    return False


def _snapshot(staged: Any) -> dict[str, str]:
    snapshot = _attr(staged, "content_snapshot")
    if isinstance(snapshot, Mapping):
        return {str(key): str(value) for key, value in snapshot.items()}
    return {}


def _changed_files(staged: Any) -> list[str]:
    if staged is None:
        return []
    return sorted(dict.fromkeys(_str_list(_attr(staged, "changed_files"))))


def _action_type(decision: Any, staged: Any = None) -> str:
    raw = (
        _attr(decision, "proposed_action")
        or _attr(decision, "action_type")
        or _attr(decision, "evolution_type")
        or _attr(staged, "action_type")
        or ""
    )
    return str(getattr(raw, "value", raw) or "").strip().upper()


def _target_skill_ids(decision: Any, staged: Any = None) -> list[str]:
    return _str_list(
        _attr(decision, "target_skill_ids")
        or _attr(decision, "target_skills")
        or _attr(staged, "target_skill_ids")
    )


def _target_skill_dirs(packet: EvidencePacket, skill_ids: list[str]) -> set[Path]:
    targets = set(skill_ids)
    result: set[Path] = set()
    for ref in _packet_refs(packet).values():
        if ref.ref_type != "skill_file":
            continue
        ref_skill_ids = _metadata_values(ref.metadata, "skill_id", "skill_ids")
        if targets and not ref_skill_ids.intersection(targets):
            continue
        path_text = str(ref.metadata.get("path") or ref.uri or "").split("#", 1)[0]
        path = _resolved_path(path_text)
        if path is None:
            continue
        result.add(path.parent if path.name == SKILL_FILENAME else path)
    return result


def _allowed_skill_roots(
    packet: EvidencePacket,
    action: str,
    decision: Any,
) -> set[Path]:
    roots: set[Path] = set()
    for key in ("capture_destination_root", "capture_root", "capture_skill_dir"):
        path = _resolved_path(packet.instructions.get(key))
        if path is not None:
            roots.add(path)
    for ref in _packet_refs(packet).values():
        for key in ("capture_destination_root", "capture_root", "capture_skill_dir"):
            path = _resolved_path(ref.metadata.get(key))
            if path is not None:
                roots.add(path)
    if action == "DERIVED":
        for target_dir in _target_skill_dirs(packet, _target_skill_ids(decision)):
            roots.add(target_dir.parent)
    return roots


def _safe_relative_path(path: str) -> bool:
    text = str(path or "").replace("\\", "/")
    if not text or text.startswith("/"):
        return False
    pure = PurePosixPath(text)
    if pure.is_absolute():
        return False
    return all(part not in {"", ".", ".."} for part in pure.parts)


def _forbidden_local_path(path: str) -> bool:
    parts = [part.lower() for part in PurePosixPath(path.replace("\\", "/")).parts]
    return any(
        part in _LOCAL_FORBIDDEN_FILENAMES
        or part.startswith(".env.")
        or part.endswith(".env")
        for part in parts
    )


def _diff_paths(diff: str) -> set[str]:
    paths: set[str] = set()
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            value = line.removeprefix("+++ b/").strip()
        elif line.startswith("--- a/"):
            value = line.removeprefix("--- a/").strip()
        else:
            continue
        if value and value != "/dev/null":
            paths.add(value)
    return paths


def _content_contains_secret(content: str) -> bool:
    if contains_secret(content):
        return True
    return any(pattern.search(content) for pattern in _SECRET_PATTERNS)


def _packet_refs(packet: EvidencePacket) -> dict[str, ResourceRef]:
    refs: dict[str, ResourceRef] = {}
    for group in packet.selected_refs.values():
        for ref in group:
            if ref.ref_id:
                refs[ref.ref_id] = ref
    for path in packet.readable_paths:
        if path.ref_id and path.ref_id not in refs:
            refs[path.ref_id] = ResourceRef(
                ref_id=path.ref_id,
                ref_type="tool_result",
                uri=path.path,
                reliability="persisted",
                role="supporting",
                contains_secret=path.contains_secret,
                metadata={"path": path.path, "purpose": path.purpose},
            )
    return refs


def _decision_ref_ids(decision: Any) -> list[str]:
    refs: list[str] = []
    decision_id = str(_attr(decision, "decision_id") or "")
    if decision_id:
        refs.append(f"decision:{decision_id}")
    source_analysis_id = str(_attr(decision, "source_analysis_id") or "")
    if source_analysis_id:
        refs.append(source_analysis_id)
    for claim in list(_attr(decision, "evidence_claims") or []):
        refs.extend(_str_list(_attr(claim, "refs")))
    return _dedupe(refs)


def _admission_ref_ids(admission: Any) -> list[str]:
    refs: list[str] = []
    admission_id = str(_attr(admission, "admission_id") or "")
    if admission_id:
        refs.append(f"admission:{admission_id}")
    refs.extend(_str_list(_attr(admission, "required_refs_checked")))
    return _dedupe(refs)


def _provenance_refs(
    authoring: Any,
    packet: EvidencePacket,
    decision: Any,
    admission: Any,
) -> list[str]:
    refs: list[str] = []
    authoring_id = str(_attr(authoring, "authoring_id") or "")
    if authoring_id:
        refs.append(f"authoring:{authoring_id}")
    refs.extend(_decision_ref_ids(decision))
    refs.extend(_admission_ref_ids(admission))
    if getattr(packet, "packet_id", ""):
        refs.append(f"packet:{packet.packet_id}")
    staged = _attr(authoring, "staged_edit")
    if staged is not None:
        refs.extend(_str_list(_attr(staged, "evidence_refs")))
    refs.extend(_packet_refs(packet))
    return _dedupe(refs)


def _frontmatter_tags(skill_md: str) -> list[str]:
    frontmatter, error = _strict_frontmatter(skill_md)
    if error:
        return []
    return _str_list(frontmatter.get("tags"))


def _is_duplicate_skill(
    *,
    proposed_name: str,
    proposed_description: str,
    proposed_tags: list[str],
    existing: dict[str, Any],
) -> bool:
    existing_name = str(existing.get("name") or "")
    existing_description = str(existing.get("description") or "")
    if _normalized_text(proposed_name) and (
        _normalized_text(proposed_name) == _normalized_text(existing_name)
    ):
        return True
    name_ratio = _similarity(proposed_name, existing_name)
    description_ratio = _similarity(proposed_description, existing_description)
    if name_ratio >= 0.9 and description_ratio >= 0.82:
        return True
    if description_ratio >= 0.94 and name_ratio >= 0.75:
        return True
    existing_tags = set(_str_list(existing.get("tags")))
    if proposed_tags and existing_tags and set(proposed_tags).intersection(existing_tags):
        return name_ratio >= 0.82 or description_ratio >= 0.88
    return False


def _skill_record_mapping(record: Any) -> dict[str, Any]:
    if isinstance(record, Mapping):
        return {
            "skill_id": str(record.get("skill_id") or ""),
            "name": str(record.get("name") or ""),
            "description": str(record.get("description") or ""),
            "tags": _str_list(record.get("tags")),
            "path": str(record.get("path") or ""),
        }
    metadata = _mapping_or_empty(getattr(record, "metadata", None))
    return {
        "skill_id": str(getattr(record, "skill_id", "") or metadata.get("skill_id") or ""),
        "name": str(getattr(record, "name", "") or metadata.get("name") or ""),
        "description": str(
            getattr(record, "description", "") or metadata.get("description") or ""
        ),
        "tags": _str_list(getattr(record, "tags", None) or metadata.get("tags")),
        "path": str(getattr(record, "path", "") or metadata.get("path") or ""),
    }


def _allowed_frontmatter_keys(admission: Any) -> set[str]:
    keys: set[str] = set()
    for field_name in (
        "allowed_frontmatter_keys",
        "approved_frontmatter_keys",
        "approved_runtime_overlay_fields",
    ):
        keys.update(_normalize_overlay_key(item) for item in _str_list(_attr(admission, field_name)))
    metadata = _mapping_or_empty(_attr(admission, "metadata"))
    for field_name in (
        "allowed_frontmatter_keys",
        "approved_frontmatter_keys",
        "approved_runtime_overlay_fields",
    ):
        keys.update(_normalize_overlay_key(item) for item in _str_list(metadata.get(field_name)))
    return keys


def _allowed_overlay_fields(admission: Any) -> set[str]:
    keys: set[str] = set()
    for field_name in (
        "allowed_overlay_fields",
        "approved_overlay_fields",
        "approved_runtime_overlay_fields",
    ):
        keys.update(_normalize_overlay_key(item) for item in _str_list(_attr(admission, field_name)))
    metadata = _mapping_or_empty(_attr(admission, "metadata"))
    for field_name in (
        "allowed_overlay_fields",
        "approved_overlay_fields",
        "approved_runtime_overlay_fields",
    ):
        keys.update(_normalize_overlay_key(item) for item in _str_list(metadata.get(field_name)))
    return keys


def _normalize_overlay_key(key: Any) -> str:
    text = str(key or "").strip()
    return _RUNTIME_OVERLAY_FIELD_ALIASES.get(text, text)


def _metadata_values(metadata: Mapping[str, Any], *keys: str) -> set[str]:
    values: set[str] = set()
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str):
            values.add(value)
        elif isinstance(value, (list, tuple, set)):
            values.update(str(item) for item in value if str(item))
    return values


def _sanitize_skill_name(name: str) -> str:
    clean = re.sub(r"[^a-z0-9\-]", "-", name.lower().strip())
    clean = re.sub(r"-{2,}", "-", clean).strip("-")
    return clean[:_MAX_SKILL_NAME_LENGTH].strip("-")


def _normalized_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _similarity(left: Any, right: Any) -> float:
    left_text = _normalized_text(left)
    right_text = _normalized_text(right)
    if not left_text or not right_text:
        return 0.0
    return difflib.SequenceMatcher(None, left_text, right_text).ratio()


def _attr(value: Any, name: str, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _mapping_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _resolved_path(value: Any) -> Path | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return Path(text).expanduser().resolve()
    except OSError:
        return None


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item)]
    return []


def _dedupe(items: list[str]) -> list[str]:
    return [item for item in dict.fromkeys(items) if item]


def _int_or_none(value: Any) -> int | None:
    try:
        if value is None or str(value) == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
