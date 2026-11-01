"""Staged authoring backend for evidence-backed skill evolution."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openspace.prompts import SkillEnginePrompts
from openspace.skill_engine.capture_contract import (
    format_capture_contract,
    normalize_capture_contract,
)
from openspace.skill_engine.evidence import EvidenceEvent, EvidencePacket, ResourceRef
from openspace.skill_engine.evolution.audit_tools import (
    PacketAuditReadError,
    PacketAuditReader,
    build_packet_audit_tools,
)
from openspace.skill_engine.evolver import EvolutionContext, EvolutionTrigger
from openspace.skill_engine.patch import (
    PatchType,
    SkillEditResult,
    SKILL_FILENAME,
    collect_skill_snapshot,
    compute_unified_diff,
    stage_create_skill,
    stage_derive_skill,
    stage_fix_skill,
)
from openspace.skill_engine.skill_utils import (
    get_frontmatter_field,
    set_frontmatter_field,
    truncate,
    validate_skill_dir,
)
from openspace.skill_engine.types import (
    CaptureContract,
    EvolutionSuggestion,
    EvolutionType,
    SkillCategory,
    SkillLineage,
    SkillOrigin,
    SkillRecord,
)
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

_SKILL_CONTENT_MAX_CHARS = 12_000
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


@dataclass(frozen=True)
class StagedSkillEdit:
    staging_id: str
    decision_id: str
    action_type: str
    staging_dir: str
    target_dir: str
    target_skill_ids: list[str]
    parent_skill_ids: list[str]
    proposed_skill_id: str | None
    proposed_name: str
    proposed_description: str
    changed_files: list[str]
    content_diff: str
    content_snapshot: dict[str, str]
    tool_dependencies: list[str]
    critical_tools: list[str]
    overlay_fields: dict[str, Any]
    overlay_metadata: dict[str, Any]
    intent_spec: dict[str, Any]
    eval_plan: dict[str, Any]
    evidence_refs: list[str]
    apply_metadata: dict[str, Any]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AuthoringResult:
    authoring_id: str
    decision_id: str
    packet_id: str
    status: str
    staged_edit: StagedSkillEdit | None
    failure_reason: str | None
    model: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.staged_edit is not None:
            data["staged_edit"] = self.staged_edit.to_dict()
        return data


@dataclass(frozen=True)
class _SourceSkill:
    skill_id: str
    record: SkillRecord
    content: str
    skill_dir: Path
    file_ref: ResourceRef


class SkillEvolverAuthoringBackend:
    """Adapt ``SkillEvolver`` into a staging-only authoring backend."""

    def __init__(
        self,
        evolver: Any,
        staging_root: Path,
        evidence_store: Any,
    ) -> None:
        self.evolver = evolver
        self.staging_root = Path(staging_root).expanduser().resolve()
        self.evidence_store = evidence_store

    async def author_from_action_packet(
        self,
        packet: EvidencePacket,
        *,
        eval_feedback: Any | None = None,
        previous_authoring: Any | None = None,
    ) -> AuthoringResult:
        authoring_id = f"auth_{uuid.uuid4().hex}"
        staging_id = f"stage_{uuid.uuid4().hex}"
        staging_dir = self.staging_root / staging_id
        staging_dir.mkdir(parents=True, exist_ok=True)
        created_at = _utc_now()
        model = _model_name(self.evolver)

        def failed(reason: str, *, status: str = "failed") -> AuthoringResult:
            result = AuthoringResult(
                authoring_id=authoring_id,
                decision_id=_decision_id(packet),
                packet_id=packet.packet_id,
                status=status,
                staged_edit=None,
                failure_reason=reason,
                model=model,
                created_at=created_at,
            )
            self._persist_result(result, staging_dir, packet)
            return result

        if getattr(packet, "packet_type", "") != "action":
            return failed("authoring requires an action EvidencePacket")

        decision_ref = _single_ref(packet, "decision_rationale_ref")
        if decision_ref is None:
            return failed("missing decision_rationale_ref")
        admission_ref = _single_ref(packet, "admission_result_ref")
        if admission_ref is None:
            return failed("missing admission_result_ref")

        admission_outcome = str(
            admission_ref.metadata.get("outcome") or ""
        ).strip().lower()
        if admission_outcome not in {"direct", "accepted"}:
            return failed(
                f"admission outcome is not accepted: {admission_outcome or '(missing)'}",
                status="declined",
            )

        action_type = _action_type(decision_ref)
        if action_type not in {"FIX", "DERIVED", "CAPTURED"}:
            return failed(f"unsupported action type: {action_type or '(missing)'}")
        proposal_contract = normalize_capture_contract(
            decision_ref.metadata.get("proposal_contract")
        )
        if action_type == "CAPTURED":
            if not proposal_contract:
                return failed("CAPTURED action missing proposal contract")
            if not bool(admission_ref.metadata.get("source_validation_passed")):
                return failed("CAPTURED action lacks source validation admission")

        target_skill_ids = _target_skill_ids(decision_ref)
        try:
            sources = _source_skills(packet, target_skill_ids, self.evolver)
        except Exception as exc:
            return failed(str(exc))
        if action_type in {"FIX", "DERIVED"} and not sources:
            return failed(f"{action_type} requires target skill_file refs")
        if action_type == "FIX" and len(sources) != 1:
            return failed("FIX requires exactly one target skill")

        direction = str(
            decision_ref.metadata.get("reason_summary")
            or decision_ref.preview
            or "Apply the admitted skill evolution."
        )
        category = _category(decision_ref) or (
            sources[0].record.category if sources else SkillCategory.WORKFLOW
        )
        prompt = self._build_prompt(
            action_type,
            sources,
            packet,
            direction,
            category=category,
            proposal_contract=proposal_contract,
            eval_feedback=eval_feedback,
            previous_authoring=previous_authoring,
        )
        ctx = EvolutionContext(
            trigger=EvolutionTrigger.ANALYSIS,
            suggestion=EvolutionSuggestion(
                evolution_type=EvolutionType(action_type.lower()),
                target_skill_ids=list(target_skill_ids),
                category=category,
                direction=direction,
                capture_contract=(
                    CaptureContract.from_dict(proposal_contract)
                    if proposal_contract
                    else None
                ),
            ),
            skill_records=[source.record for source in sources],
            skill_contents=[source.content for source in sources],
            skill_dirs=[source.skill_dir for source in sources],
            source_task_id=packet.scope.task_id,
            recent_analyses=[],
            available_tools=(
                []
                if action_type == "CAPTURED"
                else build_packet_audit_tools(packet)
            ),
            capture_dir=_capture_root(packet),
        )

        evolution_output = await self.evolver._run_evolution_loop(prompt, ctx)
        if evolution_output is None:
            return failed("evolution authoring produced no usable finalization")

        edit_content = str(getattr(evolution_output, "edit_content", "") or "")
        if not edit_content.strip():
            return failed("evolution authoring produced empty edit content")

        try:
            edit_result, proposed_name, target_dir = await self._stage_edit(
                action_type=action_type,
                sources=sources,
                packet=packet,
                edit_content=edit_content,
                staging_dir=staging_dir,
                prompt=prompt,
                ctx=ctx,
            )
        except Exception as exc:
            return failed(str(exc))
        if edit_result is None or not edit_result.ok:
            return failed(
                getattr(edit_result, "error", None) or "staging apply failed"
            )

        skill_md = edit_result.content_snapshot.get(SKILL_FILENAME, "")
        proposed_name = (
            get_frontmatter_field(skill_md, "name")
            or proposed_name
            or (sources[0].record.name if sources else "captured-skill")
        )
        proposed_description = (
            get_frontmatter_field(skill_md, "description")
            or (sources[0].record.description if sources else proposed_name)
        )
        parent_skill_ids = [source.record.skill_id for source in sources]
        tool_dependencies = sorted(
            {tool for source in sources for tool in source.record.tool_dependencies}
        )
        critical_tools = sorted(
            {tool for source in sources for tool in source.record.critical_tools}
        )
        proposed_skill_id = _proposed_skill_id(
            action_type,
            proposed_name,
            sources[0].record if sources else None,
        )
        overlay_fields, overlay_metadata = _safe_authoring_overlays(
            action_type=action_type,
            overlay_fields=dict(getattr(evolution_output, "overlay_fields", {}) or {}),
            overlay_metadata=dict(getattr(evolution_output, "overlay_metadata", {}) or {}),
        )
        staged_edit = StagedSkillEdit(
            staging_id=staging_id,
            decision_id=str(decision_ref.metadata.get("decision_id") or ""),
            action_type=action_type,
            staging_dir=str(staging_dir),
            target_dir=str(target_dir),
            target_skill_ids=list(target_skill_ids),
            parent_skill_ids=parent_skill_ids,
            proposed_skill_id=proposed_skill_id,
            proposed_name=proposed_name,
            proposed_description=proposed_description,
            changed_files=_changed_files(edit_result),
            content_diff=edit_result.content_diff,
            content_snapshot=dict(edit_result.content_snapshot),
            tool_dependencies=tool_dependencies,
            critical_tools=critical_tools,
            overlay_fields=overlay_fields,
            overlay_metadata=overlay_metadata,
            intent_spec=dict(getattr(evolution_output, "intent_spec", {}) or {}),
            eval_plan=dict(getattr(evolution_output, "eval_plan", {}) or {}),
            evidence_refs=_evidence_refs(packet, decision_ref, admission_ref),
            apply_metadata={
                "change_summary": getattr(evolution_output, "change_summary", None),
                "intent_spec": dict(getattr(evolution_output, "intent_spec", {}) or {}),
                "eval_plan": dict(getattr(evolution_output, "eval_plan", {}) or {}),
                "source_packet_id": _source_packet_id(packet),
                "action_packet_id": packet.packet_id,
                "admission_id": admission_ref.metadata.get("admission_id"),
                "proposal_contract": proposal_contract,
                "patch_type": PatchType.AUTO.value,
            },
            created_at=created_at,
        )
        result = AuthoringResult(
            authoring_id=authoring_id,
            decision_id=staged_edit.decision_id,
            packet_id=packet.packet_id,
            status="staged",
            staged_edit=staged_edit,
            failure_reason=None,
            model=model,
            created_at=created_at,
        )
        self._persist_result(result, staging_dir, packet)
        logger.info("Evolution authoring staged %s at %s", action_type, staging_dir)
        return result

    def _build_prompt(
        self,
        action_type: str,
        sources: list[_SourceSkill],
        packet: EvidencePacket,
        direction: str,
        *,
        category: SkillCategory,
        proposal_contract: dict[str, Any],
        eval_feedback: Any | None = None,
        previous_authoring: Any | None = None,
    ) -> str:
        packet_context = _packet_context(packet)
        eval_context = _eval_revision_context(eval_feedback, previous_authoring)
        if action_type == "FIX":
            current = sources[0].content if sources else ""
            base = SkillEnginePrompts.evolution_fix(
                current_content=truncate(current, _SKILL_CONTENT_MAX_CHARS),
                direction=direction,
                failure_context=packet_context,
            )
            return _append_eval_revision_context(base, eval_context)
        if action_type == "DERIVED":
            if len(sources) > 1:
                parent_content = "\n\n---\n\n".join(
                    f"## Parent {index + 1}: {source.record.name}\n"
                    f"{truncate(source.content, _SKILL_CONTENT_MAX_CHARS)}"
                    for index, source in enumerate(sources)
                )
            else:
                parent_content = truncate(
                    sources[0].content if sources else "",
                    _SKILL_CONTENT_MAX_CHARS,
                )
            base = SkillEnginePrompts.evolution_derived(
                parent_content=parent_content,
                direction=direction,
                execution_insights=packet_context,
            )
            return _append_eval_revision_context(base, eval_context)
        contract_text = format_capture_contract(proposal_contract)
        bounded_direction = "\n\n".join(
            item for item in (direction, contract_text) if item
        )
        base = SkillEnginePrompts.evolution_captured(
            direction=bounded_direction,
            category=category.value,
            execution_highlights=packet_context,
        )
        return _append_eval_revision_context(base, eval_context)

    async def _stage_edit(
        self,
        *,
        action_type: str,
        sources: list[_SourceSkill],
        packet: EvidencePacket,
        edit_content: str,
        staging_dir: Path,
        prompt: str,
        ctx: EvolutionContext,
    ) -> tuple[SkillEditResult | None, str, Path]:
        if action_type == "FIX":
            source = sources[0]
            proposed_name = source.record.name
            proposed_dir = staging_dir / "proposed" / source.skill_dir.name
            apply_fn = lambda content: stage_fix_skill(
                source.skill_dir,
                staging_dir,
                content,
                PatchType.AUTO,
            )
            target_dir = source.skill_dir
        elif action_type == "DERIVED":
            proposed_name, edit_content = _derived_name(edit_content, sources)
            proposed_dir = staging_dir / "proposed" / proposed_name
            apply_fn = lambda content: stage_derive_skill(
                [source.skill_dir for source in sources],
                staging_dir,
                proposed_name,
                content,
                PatchType.AUTO,
            )
            target_dir = sources[0].skill_dir.parent / proposed_name
        else:
            edit_content = _normalize_captured_edit_content(edit_content)
            proposed_name = get_frontmatter_field(edit_content, "name") or ""
            if not proposed_name:
                proposed_name = _captured_fallback_name(packet)
            proposed_name = _sanitize_skill_name(proposed_name)
            edit_content = set_frontmatter_field(edit_content, "name", proposed_name)
            capture_root = _capture_root(packet)
            if capture_root is None:
                raise ValueError("CAPTURED action packet missing capture destination root")
            proposed_dir = staging_dir / "proposed" / proposed_name
            def apply_fn(content: str) -> SkillEditResult:
                result = stage_create_skill(
                    staging_dir,
                    proposed_name,
                    content,
                    PatchType.AUTO,
                )
                if result.ok:
                    _ensure_captured_frontmatter(
                        proposed_dir,
                        proposed_name=proposed_name,
                        packet=packet,
                        result=result,
                    )
                return result
            target_dir = capture_root / proposed_name

        retry = getattr(self.evolver, "_apply_with_retry", None)
        if callable(retry):
            result = await retry(
                apply_fn=apply_fn,
                initial_content=edit_content,
                skill_dir=proposed_dir,
                ctx=ctx,
                prompt=prompt,
                cleanup_on_retry=staging_dir,
            )
            return result, proposed_name, target_dir

        result = apply_fn(edit_content)
        if result.ok:
            validation_error = validate_skill_dir(proposed_dir)
            if validation_error:
                return SkillEditResult(error=f"Validation failed: {validation_error}"), proposed_name, target_dir
        return result, proposed_name, target_dir

    def _persist_result(
        self,
        result: AuthoringResult,
        staging_dir: Path,
        packet: EvidencePacket,
    ) -> None:
        staging_dir.mkdir(parents=True, exist_ok=True)
        (staging_dir / "authoring.json").write_text(
            json.dumps(result.to_dict(), indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
        (staging_dir / "prompt_refs.json").write_text(
            json.dumps(
                {
                    "packet_id": packet.packet_id,
                    "refs": [
                        ref.ref_id
                        for refs in packet.selected_refs.values()
                        for ref in refs
                        if ref.ref_id
                    ],
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

        if self.evidence_store is None:
            return
        ref = ResourceRef(
            ref_id=f"authoring:{result.authoring_id}",
            ref_type="authoring_result_ref",
            uri=str(staging_dir),
            session_id=packet.scope.session_id,
            task_id=packet.scope.task_id,
            producer="authoring_backend",
            created_at=result.created_at,
            reliability="derived",
            role="derived",
            preview=(
                f"authoring {result.status}"
                + (f": {result.failure_reason}" if result.failure_reason else "")
            )[:500],
            metadata={
                "authoring_id": result.authoring_id,
                "decision_id": result.decision_id,
                "packet_id": result.packet_id,
                "status": result.status,
                "failure_reason": result.failure_reason,
                "staging_dir": str(staging_dir),
                "staged_edit": (
                    result.staged_edit.to_dict()
                    if result.staged_edit is not None
                    else None
                ),
            },
            raw_backrefs=_authoring_backrefs(packet, result),
        )
        event = EvidenceEvent.create(
            event_id=f"evt_authoring_{_digest(result.authoring_id)}",
            event_type="authoring_result_persisted",
            producer="authoring_backend",
            created_at=result.created_at,
            session_id=packet.scope.session_id,
            task_id=packet.scope.task_id,
            idempotency_key=f"authoring_result:{result.authoring_id}",
            derived_refs=[ref],
            metadata={
                "authoring_id": result.authoring_id,
                "packet_id": result.packet_id,
                "status": result.status,
            },
        )
        self.evidence_store.ingest_event(event)


def _safe_authoring_overlays(
    *,
    action_type: str,
    overlay_fields: dict[str, Any],
    overlay_metadata: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if action_type != "CAPTURED":
        return overlay_fields, overlay_metadata

    cleaned_fields = dict(overlay_fields)
    cleaned_metadata = dict(overlay_metadata)
    removed: list[str] = []
    for key in list(cleaned_fields):
        normalized = _normalize_runtime_overlay_key(key)
        if normalized not in _HIGH_RISK_RUNTIME_OVERLAY_FIELDS:
            continue
        cleaned_fields.pop(key, None)
        cleaned_metadata.pop(key, None)
        cleaned_metadata.pop(normalized, None)
        removed.append(normalized)

    if removed:
        cleaned_metadata["_authoring_removed_overlay_fields"] = {
            "fields": sorted(set(removed)),
            "reason": (
                "CAPTURED skills cannot introduce high-risk runtime overlay "
                "fields without explicit admission approval."
            ),
            "source": "authoring_backend",
        }
    return cleaned_fields, cleaned_metadata


def _normalize_runtime_overlay_key(key: Any) -> str:
    text = str(key or "").strip()
    return _RUNTIME_OVERLAY_FIELD_ALIASES.get(text, text)


def _source_skills(
    packet: EvidencePacket,
    target_skill_ids: list[str],
    evolver: Any,
) -> list[_SourceSkill]:
    if not target_skill_ids:
        return []
    reader = PacketAuditReader(packet)
    skill_refs = [
        ref
        for ref in _all_refs(packet)
        if ref.ref_type == "skill_file"
        and str(ref.metadata.get("skill_id") or "") in set(target_skill_ids)
    ]
    by_skill_id = {
        str(ref.metadata.get("skill_id") or ""): ref
        for ref in skill_refs
    }
    sources: list[_SourceSkill] = []
    for skill_id in target_skill_ids:
        ref = by_skill_id.get(skill_id)
        if ref is None:
            raise ValueError(f"missing skill_file ref for target skill: {skill_id}")
        try:
            content = reader.read_skill_file(ref.ref_id)
        except PacketAuditReadError as exc:
            raise ValueError(f"unreadable skill_file ref {ref.ref_id}: {exc}") from exc
        path_text = str(ref.metadata.get("path") or ref.uri or "")
        if not path_text:
            raise ValueError(f"skill_file ref missing path: {ref.ref_id}")
        skill_file = Path(path_text).expanduser().resolve()
        skill_dir = skill_file.parent if skill_file.name == SKILL_FILENAME else skill_file
        record = _load_record(evolver, skill_id)
        if record is None:
            record = _record_from_ref(skill_id, ref, content, skill_file)
        sources.append(
            _SourceSkill(
                skill_id=skill_id,
                record=record,
                content=content,
                skill_dir=skill_dir,
                file_ref=ref,
            )
        )
    return sources


def _load_record(evolver: Any, skill_id: str) -> SkillRecord | None:
    store = getattr(evolver, "_store", None)
    load_record = getattr(store, "load_record", None)
    if callable(load_record):
        try:
            record = load_record(skill_id)
            if isinstance(record, SkillRecord):
                return record
        except Exception:
            logger.debug("Authoring could not load SkillRecord %s", skill_id, exc_info=True)
    return None


def _record_from_ref(
    skill_id: str,
    ref: ResourceRef,
    content: str,
    skill_file: Path,
) -> SkillRecord:
    name = get_frontmatter_field(content, "name") or skill_file.parent.name
    description = get_frontmatter_field(content, "description") or ref.preview or name
    return SkillRecord(
        skill_id=skill_id,
        name=name,
        description=description,
        path=str(skill_file),
        lineage=SkillLineage(origin=SkillOrigin.IMPORTED),
    )


def _single_ref(packet: EvidencePacket, ref_type: str) -> ResourceRef | None:
    refs = packet.selected_refs.get(ref_type) or []
    return refs[0] if refs else None


def _normalize_captured_edit_content(content: str) -> str:
    stripped = str(content or "").lstrip()
    if stripped.startswith("---\n"):
        return stripped
    match = re.search(
        r"(?m)^---\n(?=(?:[^\n]*\n){0,12}name\s*:)",
        str(content or ""),
    )
    if match:
        return str(content)[match.start():].lstrip()
    return stripped


def _all_refs(packet: EvidencePacket) -> list[ResourceRef]:
    return [
        ref
        for refs in packet.selected_refs.values()
        for ref in refs
    ]


def _action_type(decision_ref: ResourceRef) -> str:
    return str(decision_ref.metadata.get("proposed_action") or "").strip().upper()


def _decision_id(packet: EvidencePacket) -> str:
    ref = _single_ref(packet, "decision_rationale_ref")
    if ref is None:
        return ""
    return str(ref.metadata.get("decision_id") or "").strip()


def _target_skill_ids(decision_ref: ResourceRef) -> list[str]:
    return _str_list(decision_ref.metadata.get("target_skill_ids"))


def _category(decision_ref: ResourceRef) -> SkillCategory | None:
    value = decision_ref.metadata.get("category")
    if not value:
        return None
    try:
        return SkillCategory(str(value))
    except ValueError:
        return None


def _capture_root(packet: EvidencePacket) -> Path | None:
    for key in ("capture_destination_root", "capture_root", "capture_skill_dir"):
        value = packet.instructions.get(key)
        if value:
            return Path(value).expanduser().resolve()
    for ref in _all_refs(packet):
        for key in ("capture_destination_root", "capture_root", "capture_skill_dir"):
            value = ref.metadata.get(key)
            if value:
                return Path(str(value)).expanduser().resolve()
    return None


def _packet_context(packet: EvidencePacket) -> str:
    snippets = [
        snippet.text
        for snippet in packet.expanded_snippets
        if str(snippet.text or "").strip()
    ]
    if snippets:
        return "\n\n".join(snippets)
    previews = [
        f"[{ref.ref_id}] {ref.preview}"
        for ref in _all_refs(packet)
        if ref.preview
    ]
    return "\n".join(previews) or f"Evidence packet {packet.packet_id}"


def _source_packet_id(packet: EvidencePacket) -> str | None:
    packet_ref = _single_ref(packet, "evidence_packet_ref")
    if packet_ref is None:
        return None
    return str(packet_ref.metadata.get("packet_id") or packet_ref.ref_id).removeprefix("packet:")


def _eval_revision_context(
    eval_feedback: Any | None,
    previous_authoring: Any | None,
) -> str:
    if eval_feedback is None and previous_authoring is None:
        return ""
    parts: list[str] = []
    if eval_feedback is not None:
        parts.append(_format_eval_feedback(eval_feedback))
    staged = getattr(previous_authoring, "staged_edit", None)
    if staged is None and isinstance(previous_authoring, dict):
        staged = previous_authoring.get("staged_edit")
    snapshot = getattr(staged, "content_snapshot", None)
    if snapshot is None and isinstance(staged, dict):
        snapshot = staged.get("content_snapshot")
    if isinstance(snapshot, dict) and snapshot:
        rendered = []
        for rel_path, content in sorted(snapshot.items()):
            text = str(content)
            rendered.append(f"### Previous draft: {rel_path}\n{text[:12000]}")
        parts.append("\n\n".join(rendered))
    return "\n\n".join(part for part in parts if part.strip())


def _format_eval_feedback(eval_feedback: Any) -> str:
    if isinstance(eval_feedback, str):
        return eval_feedback
    to_dict = getattr(eval_feedback, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
    elif isinstance(eval_feedback, dict):
        payload = eval_feedback
    else:
        payload = {
            "outcome": getattr(eval_feedback, "outcome", ""),
            "failures": getattr(eval_feedback, "failures", []),
            "warnings": getattr(eval_feedback, "warnings", []),
        }
    try:
        return json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    except Exception:
        return str(payload)


def _append_eval_revision_context(prompt: str, eval_context: str) -> str:
    if not eval_context.strip():
        return prompt
    return (
        f"{prompt.rstrip()}\n\n"
        "## Behavior Eval Feedback From Previous Draft\n\n"
        "A previous draft failed the behavior-evaluation gate. Revise the skill "
        "content and the structured finalization fields so the new draft satisfies "
        "the feedback without overfitting to the eval text.\n\n"
        f"{eval_context.strip()}\n"
    )


def _derived_name(
    edit_content: str,
    sources: list[_SourceSkill],
) -> tuple[str, str]:
    first_parent_name = sources[0].record.name if sources else "derived-skill"
    is_merge = len(sources) > 1
    new_name = get_frontmatter_field(edit_content, "name")
    if not new_name or new_name == first_parent_name:
        suffix = "-merged" if is_merge else "-enhanced"
        new_name = f"{first_parent_name}{suffix}"
    new_name = _sanitize_skill_name(new_name)
    return new_name, set_frontmatter_field(edit_content, "name", new_name)


def _sanitize_skill_name(name: str) -> str:
    import re

    clean = re.sub(r"[^a-z0-9\-]", "-", name.lower().strip())
    clean = re.sub(r"-{2,}", "-", clean).strip("-")
    return clean[:50].strip("-") or "skill"


def _captured_fallback_name(packet: EvidencePacket) -> str:
    task_id = str(getattr(packet.scope, "task_id", "") or "").strip()
    profile = str(getattr(packet, "subprofile", "") or "").strip()
    packet_id = str(getattr(packet, "packet_id", "") or uuid.uuid4().hex[:8])
    seed = task_id or profile or packet_id
    return f"captured-{seed}-{packet_id[-6:]}"


def _ensure_captured_frontmatter(
    skill_dir: Path,
    *,
    proposed_name: str,
    packet: EvidencePacket,
    result: SkillEditResult,
) -> None:
    skill_file = skill_dir / SKILL_FILENAME
    if not skill_file.exists():
        return
    try:
        content = skill_file.read_text(encoding="utf-8")
    except OSError:
        return

    updated = content
    if not get_frontmatter_field(updated, "name"):
        updated = set_frontmatter_field(updated, "name", proposed_name)
    if not get_frontmatter_field(updated, "description"):
        updated = set_frontmatter_field(
            updated,
            "description",
            _captured_description(packet, proposed_name),
        )
    if updated == content:
        return

    skill_file.write_text(updated, encoding="utf-8")
    snapshot = collect_skill_snapshot(skill_dir)
    result.content_snapshot = snapshot
    result.content_diff = "\n".join(
        compute_unified_diff("", text, filename=name)
        for name, text in sorted(snapshot.items())
        if compute_unified_diff("", text, filename=name)
    )


def _captured_description(packet: EvidencePacket, proposed_name: str) -> str:
    decision_ref = _single_ref(packet, "decision_rationale_ref")
    if decision_ref is not None:
        raw = (
            decision_ref.metadata.get("reason_summary")
            or decision_ref.preview
            or proposed_name
        )
    else:
        raw = proposed_name
    text = " ".join(str(raw or proposed_name).split())
    return truncate(text, 240) or proposed_name


def _proposed_skill_id(
    action_type: str,
    proposed_name: str,
    parent: SkillRecord | None,
) -> str | None:
    if not proposed_name:
        return None
    if action_type == "FIX" and parent is not None:
        generation = parent.lineage.generation + 1
        return f"{proposed_name}__v{generation}_{uuid.uuid4().hex[:8]}"
    return f"{proposed_name}__v0_{uuid.uuid4().hex[:8]}"


def _changed_files(edit_result: SkillEditResult) -> list[str]:
    files: set[str] = set()
    for line in edit_result.content_diff.splitlines():
        if line.startswith("+++ b/"):
            name = line.removeprefix("+++ b/")
            if name and name != "/dev/null":
                files.add(name)
        elif line.startswith("--- a/"):
            name = line.removeprefix("--- a/")
            if name and name != "/dev/null":
                files.add(name)
    if files:
        return sorted(files)
    return sorted(edit_result.content_snapshot)


def _evidence_refs(
    packet: EvidencePacket,
    decision_ref: ResourceRef,
    admission_ref: ResourceRef,
) -> list[str]:
    refs: list[str] = []
    refs.extend(decision_ref.raw_backrefs)
    refs.extend(admission_ref.raw_backrefs)
    refs.extend(
        ref.ref_id
        for ref in _all_refs(packet)
        if ref.ref_type
        not in {
            "decision_rationale_ref",
            "admission_result_ref",
            "evidence_packet_ref",
        }
    )
    return [item for item in dict.fromkeys(refs) if item]


def _authoring_backrefs(
    packet: EvidencePacket,
    result: AuthoringResult,
) -> list[str]:
    refs = [f"packet:{packet.packet_id}"]
    refs.extend(
        ref.ref_id
        for ref in _all_refs(packet)
        if ref.ref_id
    )
    if result.staged_edit is not None:
        refs.extend(result.staged_edit.evidence_refs)
    return [item for item in dict.fromkeys(refs) if item]


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item)]
    return []


def _model_name(evolver: Any) -> str:
    explicit = getattr(evolver, "_model", None)
    if explicit:
        return str(explicit)
    llm = getattr(evolver, "_llm_client", None)
    return str(getattr(llm, "model", "") or "")


def _digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    import hashlib

    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
