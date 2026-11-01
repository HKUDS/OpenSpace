"""SkillEvolver authoring backend primitives.

Three evolution types:
  FIX      — repair broken/outdated instructions (in-place, same name)
  DERIVED  — create enhanced version from existing skill (new directory)
  CAPTURED — capture novel reusable pattern from execution (brand new skill)

Runtime mutation must enter through TriggerJob → EvidencePacket →
DecisionRationale → Admission → staged authoring → validation → commit.
This module keeps the old authoring primitives private while staging support
is extracted.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import shutil
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, TYPE_CHECKING

from openspace.tool_runtime.orchestration import run_tools

from .types import (
    EvolutionSuggestion,
    EvolutionType,
    ExecutionAnalysis,
    SkillCategory,
    SkillLineage,
    SkillOrigin,
    SkillRecord,
    SkillTrustState,
)
from .patch import (
    PatchType,
    SkillEditResult,
    collect_skill_snapshot,
    create_skill,
    fix_skill,
    derive_skill,
    SKILL_FILENAME,
)
from .skill_utils import (
    get_frontmatter_field as _extract_frontmatter_field,
    set_frontmatter_field as _set_frontmatter_field,
    strip_markdown_fences as _strip_markdown_fences,
    truncate as _truncate,
    validate_skill_dir as _validate_skill_dir,
)
from .registry import write_skill_id
from .store import SkillStore
from openspace.prompts import SkillEnginePrompts
from openspace.telemetry.call_source import reset_call_source, set_call_source
from openspace.utils.logging import Logger

if TYPE_CHECKING:
    from .registry import SkillRegistry
    from openspace.llm import LLMClient
    from openspace.grounding.core.tool import BaseTool
    from openspace.grounding.core.quality.types import ToolQualityRecord

logger = Logger.get_logger(__name__)

_SKILL_CONTENT_MAX_CHARS = 12_000   # Max chars of SKILL.md in evolution prompt
_MAX_SKILL_NAME_LENGTH = 50         # Max chars for a skill name (directory name)


@dataclass
class _EvolutionFinalOutput:
    edit_content: str
    change_summary: Optional[str] = None
    overlay_fields: Dict[str, Any] = field(default_factory=dict)
    overlay_metadata: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    intent_spec: Dict[str, Any] = field(default_factory=dict)
    eval_plan: Dict[str, Any] = field(default_factory=dict)


def _sanitize_skill_name(name: str) -> str:
    """Enforce naming rules for skill names (used as directory names).

    - Lowercase, hyphens only (no underscores or special chars)
    - Truncate to ``_MAX_SKILL_NAME_LENGTH`` at a word boundary
    - Remove trailing hyphens
    """
    # Normalize: lowercase, replace underscores and spaces with hyphens
    clean = re.sub(r"[^a-z0-9\-]", "-", name.lower().strip())
    # Collapse multiple hyphens
    clean = re.sub(r"-{2,}", "-", clean).strip("-")

    if len(clean) <= _MAX_SKILL_NAME_LENGTH:
        return clean

    # Truncate at a hyphen boundary to avoid cutting words
    truncated = clean[:_MAX_SKILL_NAME_LENGTH]
    last_hyphen = truncated.rfind("-")
    if last_hyphen > _MAX_SKILL_NAME_LENGTH // 2:
        truncated = truncated[:last_hyphen]
    return truncated.strip("-")


def _parse_runtime_overlay_payload(
    payload: Any,
) -> tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
    if not isinstance(payload, dict):
        return {}, {}

    raw_fields = payload.get("fields")
    if not isinstance(raw_fields, dict):
        raw_fields = {
            key: value
            for key, value in payload.items()
            if key not in {"rationale", "metadata", "notes"}
        }
    raw_rationale = payload.get("rationale")
    rationale = raw_rationale if isinstance(raw_rationale, dict) else {}
    raw_metadata = payload.get("metadata")
    metadata_input = raw_metadata if isinstance(raw_metadata, dict) else {}

    fields: Dict[str, Any] = {}
    metadata: Dict[str, Dict[str, Any]] = {}
    for raw_key, value in raw_fields.items():
        key = _normalize_runtime_overlay_field_name(raw_key)
        if key not in _ALLOWED_RUNTIME_OVERLAY_FIELDS:
            continue
        normalized = _normalize_runtime_overlay_field_value(key, value)
        if normalized is _INVALID_RUNTIME_OVERLAY_VALUE:
            continue
        fields[key] = normalized
        field_meta: Dict[str, Any] = {
            "risk": "high" if key in _HIGH_RISK_RUNTIME_OVERLAY_FIELDS else "low",
            "source": "skill_evolver",
        }
        reason = rationale.get(raw_key, rationale.get(key))
        if isinstance(reason, str) and reason.strip():
            field_meta["rationale"] = reason.strip()
        extra = metadata_input.get(raw_key, metadata_input.get(key))
        if isinstance(extra, dict):
            field_meta.update(extra)
        metadata[key] = field_meta
    return fields, metadata


def _extract_evolution_finalization(
    content: str,
) -> tuple[Optional[_EvolutionFinalOutput], Optional[str], bool]:
    """Parse the structured finalization block from an evolution response.

    Returns ``(output, failure_reason, found_block)``. A missing block is not a
    failure by itself because the agent may still be gathering information.
    """

    stripped = _strip_markdown_fences(content)
    matches = list(_EVOLUTION_FINALIZATION_BLOCK_RE.finditer(stripped))
    if not matches:
        return None, None, False
    if len(matches) != 1:
        return None, "Expected exactly one evolution finalization block", True

    match = matches[0]
    if stripped[match.end():].strip():
        return (
            None,
            "Evolution finalization block must be the final response content",
            True,
        )
    raw_block = _strip_markdown_fences(match.group(1).strip())
    try:
        payload = json.loads(raw_block)
    except Exception:
        return None, "Malformed evolution finalization JSON", True
    if not isinstance(payload, dict):
        return None, "Evolution finalization payload must be a JSON object", True

    status = str(payload.get("status", "")).strip().lower()
    if status == "failed":
        reason = str(payload.get("reason", "")).strip()
        return None, reason or "LLM declined to produce edit", True
    if status != "complete":
        return None, f"Unsupported evolution finalization status: {status or '(missing)'}", True

    edit_content = stripped[:match.start()].strip()
    edit_content = _strip_markdown_fences(edit_content)
    if not edit_content.strip():
        return None, "Evolution finalization completed without edit content", True

    overlay_fields: Dict[str, Any] = {}
    overlay_metadata: Dict[str, Dict[str, Any]] = {}
    runtime_overlay = payload.get("runtime_overlay")
    if isinstance(runtime_overlay, dict):
        final_fields, final_metadata = _parse_runtime_overlay_payload(runtime_overlay)
        overlay_fields.update(final_fields)
        overlay_metadata.update(final_metadata)

    change_summary = str(payload.get("change_summary", "")).strip() or None
    intent_spec = payload.get("intent_spec")
    eval_plan = payload.get("eval_plan")
    if not isinstance(intent_spec, dict) or not intent_spec:
        return None, "Evolution finalization missing intent_spec", True
    if not isinstance(eval_plan, dict) or not eval_plan:
        return None, "Evolution finalization missing eval_plan", True
    return (
        _EvolutionFinalOutput(
            edit_content=edit_content,
            change_summary=change_summary,
            overlay_fields=overlay_fields,
            overlay_metadata=overlay_metadata,
            intent_spec=dict(intent_spec),
            eval_plan=dict(eval_plan),
        ),
        None,
        True,
    )


def _normalize_runtime_overlay_field_name(raw_key: Any) -> str:
    key = str(raw_key or "").strip()
    return _RUNTIME_OVERLAY_FIELD_ALIASES.get(key, key)


_INVALID_RUNTIME_OVERLAY_VALUE = object()


def _normalize_runtime_overlay_field_value(key: str, value: Any) -> Any:
    if key == "hooks":
        return value if isinstance(value, dict) and value else _INVALID_RUNTIME_OVERLAY_VALUE
    if key == "context":
        text = str(value or "").strip().lower()
        return "fork" if text == "fork" else _INVALID_RUNTIME_OVERLAY_VALUE
    if key == "shell":
        text = str(value or "").strip().lower()
        return text if text in {"bash", "powershell"} else _INVALID_RUNTIME_OVERLAY_VALUE
    if key in {"allowed-tools", "paths", "arguments"}:
        if isinstance(value, (list, tuple, set)):
            items = [str(item).strip() for item in value if str(item).strip()]
            return items if items else _INVALID_RUNTIME_OVERLAY_VALUE
        text = str(value or "").strip()
        return text if text else _INVALID_RUNTIME_OVERLAY_VALUE
    if key in {"disable-model-invocation", "user-invocable"}:
        if isinstance(value, bool):
            return value
        text = str(value or "").strip().lower()
        if text in {"1", "true", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "no", "n", "off"}:
            return False
        return _INVALID_RUNTIME_OVERLAY_VALUE
    if isinstance(value, str):
        text = value.strip()
        return text if text else _INVALID_RUNTIME_OVERLAY_VALUE
    return value if value is not None else _INVALID_RUNTIME_OVERLAY_VALUE

_ANALYSIS_CONTEXT_MAX = 5           # Max recent analyses to include in prompt
_ANALYSIS_NOTE_MAX_CHARS = 500      # Per-analysis note truncation

# Agent loop / retry constants
_MAX_EVOLUTION_ITERATIONS = 5       # Max tool-calling rounds for evolution agent
_MAX_EVOLUTION_ATTEMPTS = 3         # Max apply-retry attempts per evolution
_MAX_EVOLUTION_LENGTH_RECOVERIES = 1

_EVOLUTION_FINALIZATION_BLOCK_RE = re.compile(
    r"\n?\*\*\* Begin Evolution Finalization\s*\n(.*?)\n\*\*\* End Evolution Finalization\s*",
    re.DOTALL | re.IGNORECASE,
)


def _build_evolution_length_recovery_message(
    attempt: int,
) -> Dict[str, Any]:
    return {
        "role": "user",
        "content": (
            "Your previous skill-authoring response was truncated by the output "
            "token limit. Do not continue it verbatim. Replace it with one "
            "concise, complete response that fits: output the full SKILL.md edit "
            "content, followed by exactly one Evolution Finalization block. "
            "Include every required finalization field, especially status, "
            "change_summary, intent_spec, and eval_plan. Keep only reusable "
            "instructions and essential examples. Do not call tools. "
            f"(recovery attempt {attempt}/{_MAX_EVOLUTION_LENGTH_RECOVERIES})"
        ),
        "_meta": {
            "type": "evolution_max_output_tokens_recovery",
            "is_meta": True,
        },
    }


_LOW_RISK_RUNTIME_OVERLAY_FIELDS = {
    "description",
    "when_to_use",
    "argument-hint",
    "arguments",
    "version",
    "paths",
}
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
_ALLOWED_RUNTIME_OVERLAY_FIELDS = (
    _LOW_RISK_RUNTIME_OVERLAY_FIELDS | _HIGH_RISK_RUNTIME_OVERLAY_FIELDS
)
_RUNTIME_OVERLAY_FIELD_ALIASES = {
    "allowed_tools": "allowed-tools",
    "allowedTools": "allowed-tools",
    "disable_model_invocation": "disable-model-invocation",
    "disableModelInvocation": "disable-model-invocation",
    "user_invocable": "user-invocable",
    "userInvocable": "user-invocable",
    "whenToUse": "when_to_use",
    "when-to-use": "when_to_use",
    "argument_hint": "argument-hint",
    "argumentHint": "argument-hint",
}


class EvolutionTrigger(str, Enum):
    """What initiated this evolution."""
    ANALYSIS         = "analysis"           # Post-execution analysis suggestion


@dataclass
class EvolutionContext:
    """Unified context for all evolution triggers.

    For trigger 1 (ANALYSIS): source_task_id is set, recent_analyses may be
    just the single triggering analysis.
    For triggers 2/3: source_task_id is None, recent_analyses are loaded
    from the skill's historical records.
    """
    trigger: EvolutionTrigger
    suggestion: EvolutionSuggestion

    # Parent skill context
    skill_records: List[SkillRecord] = field(default_factory=list)
    skill_contents: List[str] = field(default_factory=list)
    skill_dirs: List[Path] = field(default_factory=list)

    # Task context
    source_task_id: Optional[str] = None
    recent_analyses: List[ExecutionAnalysis] = field(default_factory=list)

    # Available tools for agent loop (read, web_search, shell, MCP, etc.)
    available_tools: List["BaseTool"] = field(default_factory=list)

    # For CAPTURED: preferred directory to write the new skill.
    # Set from the calling host agent's skill directory so captured skills
    # are written back to the correct host, not always to _skill_dirs[0].
    capture_dir: Optional[Path] = None


class SkillEvolver:
    """Authoring backend primitives for skill evolution.

    Runtime callers must enter through TriggerJob/EvolutionEngine.  This class
    keeps the legacy authoring implementation available for staged backend
    extraction, but it no longer exposes public direct mutation triggers.

    Concurrency:
        ``max_concurrent`` controls the semaphore that throttles parallel
        evolutions across all trigger types.  File I/O is synchronous and
        naturally serialized by the event loop; only LLM calls run in
        parallel.

    Background:
        Background task tracking remains only for in-flight tasks created by
        older runtimes during shutdown; new runtime flow does not schedule
        SkillEvolver direct work.
    """

    def __init__(
        self,
        store: SkillStore,
        registry: "SkillRegistry",
        llm_client: "LLMClient",
        model: Optional[str] = None,
        available_tools: Optional[List["BaseTool"]] = None,
        *,
        max_tokens: Optional[int] = None,
        max_concurrent: int = 3,
        allow_legacy_direct_mutation: bool = False,
    ) -> None:
        self._store = store
        self._registry = registry
        self._llm_client = llm_client
        self._model = model
        self._max_tokens = (
            max(1, int(max_tokens)) if max_tokens is not None else None
        )
        self._available_tools: List["BaseTool"] = available_tools or []
        self._allow_legacy_direct_mutation = bool(allow_legacy_direct_mutation)

        # Concurrency: semaphore limits parallel LLM sessions
        self._max_concurrent = max(1, max_concurrent)
        self._semaphore = asyncio.Semaphore(self._max_concurrent)

        # Track background tasks so they can be awaited on shutdown.
        self._background_tasks: Set[asyncio.Task] = set()

    def _ensure_legacy_direct_mutation_allowed(self) -> None:
        if self._allow_legacy_direct_mutation:
            return
        raise RuntimeError(
            "SkillEvolver direct mutation is disabled; use the "
            "evidence-backed EvolutionEngine flow."
        )

    def set_available_tools(self, tools: List["BaseTool"]) -> None:
        """Update the tools available for evolution agent loops."""
        self._available_tools = list(tools)

    async def _call_evolution_model(
        self,
        *,
        messages: List[Dict[str, Any]],
        tools: Optional[List["BaseTool"]] = None,
        model: Optional[str] = None,
    ):
        kwargs: Dict[str, Any] = {
            "messages": messages,
            "tools": tools,
            "model": model,
        }
        if self._max_tokens is not None:
            kwargs["max_tokens"] = self._max_tokens
        return await self._llm_client.call_model_with_fallback(**kwargs)

    async def wait_background(self) -> None:
        """Await all outstanding background evolution tasks.

        Call this during shutdown / cleanup to ensure nothing is lost.
        """
        if self._background_tasks:
            logger.info(
                f"Waiting for {len(self._background_tasks)} background "
                f"evolution task(s) to finish..."
            )
            results = await asyncio.gather(*self._background_tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, BaseException):
                    logger.warning(f"Background evolution task failed during shutdown: {r}")
            self._background_tasks.clear()

    async def _evolve_context(self, ctx: EvolutionContext) -> Optional[SkillRecord]:
        """Execute one authoring context. Returns new SkillRecord or None.

        The global semaphore is NOT acquired here — it is managed at the
        caller level so the concurrency limit covers the whole batch.
        """
        self._ensure_legacy_direct_mutation_allowed()
        _src_tok = set_call_source("evolver")

        evo_type = ctx.suggestion.evolution_type
        try:
            if evo_type == EvolutionType.FIX:
                return await self._evolve_fix(ctx)
            elif evo_type == EvolutionType.DERIVED:
                return await self._evolve_derived(ctx)
            elif evo_type == EvolutionType.CAPTURED:
                return await self._evolve_captured(ctx)
            else:
                logger.warning(f"Unknown evolution type: {evo_type}")
                return None
        except Exception as e:
            targets = "+".join(ctx.suggestion.target_skill_ids) or "(new)"
            logger.error(f"Evolution failed [{evo_type.value}] target={targets}: {e}")
            return None
        finally:
            reset_call_source(_src_tok)

    async def _record_origin_trust(
        self,
        record: SkillRecord,
        ctx: EvolutionContext,
    ) -> None:
        recorder = getattr(self._store, "record_trust_observation", None)
        if not callable(recorder):
            return
        task_id = str(ctx.source_task_id or "")
        observation_id = (
            f"task:{task_id}" if task_id else f"skill-origin:{record.skill_id}"
        )
        try:
            observed = await recorder(
                record.skill_id,
                observation_id,
                "success",
                task_id=task_id,
                source="legacy_evolution_origin",
            )
            if isinstance(observed, SkillRecord):
                record.trust_state = observed.trust_state
                record.trust_successes = observed.trust_successes
                record.trust_failures = observed.trust_failures
                record.last_updated = observed.last_updated
        except Exception:
            logger.warning(
                "Evolution trust origin record failed for %s",
                record.skill_id,
                exc_info=True,
            )

    async def _suggest_runtime_overlays_from_analysis(
        self,
        analysis: ExecutionAnalysis,
    ) -> None:
        """Write conservative runtime-field overlay suggestions from outcomes.

        OpenSpace's evolve loop can improve skill manifests without mutating
        community/plugin skill files.  This method only suggests low-risk
        fields and never auto-approves permission, hook, shell, model, effort,
        context, or agent changes.
        """

        if not analysis.skill_judgments:
            return

        phase_failed = set(analysis.skill_phase_failed_skill_ids or [])
        note = " ".join(str(analysis.execution_note or "").split())
        for judgment in analysis.skill_judgments:
            if not judgment.skill_applied:
                continue
            if not analysis.task_completed or judgment.skill_id in phase_failed:
                continue
            try:
                meta = self._registry.get_skill(judgment.skill_id)
            except Exception:
                meta = None
            if meta is None:
                continue

            suggestions: dict[str, Any] = {}
            if not getattr(meta, "when_to_use", None):
                basis = note or judgment.note or f"successful task {analysis.task_id}"
                suggestions["when_to_use"] = _truncate(
                    f"Use when this workflow matches: {basis}",
                    240,
                )

            if not suggestions:
                continue

            try:
                self._registry.write_runtime_overlay(
                    judgment.skill_id,
                    suggestions,
                    approved=False,
                    field_metadata={
                        key: {
                            "risk": "low",
                            "source": "skill_evolver.analysis",
                            "rationale": (
                                "Successful applied skill lacked low-risk "
                                "runtime selection metadata."
                            ),
                        }
                        for key in suggestions
                    },
                )
            except Exception:
                logger.debug(
                    "Runtime overlay suggestion skipped for %s",
                    judgment.skill_id,
                    exc_info=True,
                )
                continue

            try:
                await self._store.record_skill_event(
                    judgment.skill_id,
                    "field_suggested",
                    source="evolver_overlay",
                    task_id=analysis.task_id,
                    metadata={
                        "fields": sorted(suggestions),
                        "approved": False,
                        "reason": "successful applied skill lacked low-risk runtime metadata",
                    },
                )
            except Exception:
                logger.debug(
                    "Runtime overlay field_suggested event skipped for %s",
                    judgment.skill_id,
                    exc_info=True,
                )

    async def _write_generated_runtime_overlay(
        self,
        skill_id: str,
        fields: Dict[str, Any],
        metadata: Dict[str, Dict[str, Any]],
        ctx: EvolutionContext,
    ) -> None:
        """Persist LLM-generated runtime overlay suggestions for human review."""

        if not fields:
            return

        enriched_meta: Dict[str, Dict[str, Any]] = {}
        for key in fields:
            base = dict(metadata.get(key) or {})
            base.setdefault(
                "risk",
                "high" if key in _HIGH_RISK_RUNTIME_OVERLAY_FIELDS else "low",
            )
            base.setdefault("source", "skill_evolver.generated")
            base["evolution_type"] = ctx.suggestion.evolution_type.value
            if ctx.source_task_id:
                base["source_task_id"] = ctx.source_task_id
            enriched_meta[key] = base

        try:
            self._registry.write_runtime_overlay(
                skill_id,
                fields,
                approved=False,
                field_metadata=enriched_meta,
            )
        except Exception:
            logger.debug(
                "Generated runtime overlay suggestion skipped for %s",
                skill_id,
                exc_info=True,
            )
            return

        try:
            await self._store.record_skill_event(
                skill_id,
                "field_suggested",
                source="skill_evolver_generated_overlay",
                task_id=ctx.source_task_id or "",
                metadata={
                    "fields": sorted(fields),
                    "approved": False,
                    "high_risk_fields": sorted(
                        key for key in fields if key in _HIGH_RISK_RUNTIME_OVERLAY_FIELDS
                    ),
                    "reason": "LLM emitted structured runtime overlay suggestions",
                },
            )
        except Exception:
            logger.debug(
                "Generated runtime overlay field_suggested event skipped for %s",
                skill_id,
                exc_info=True,
            )

    async def _execute_contexts(
        self,
        contexts: List[EvolutionContext],
        trigger_label: str,
    ) -> List[SkillRecord]:
        """Execute a list of evolution contexts in parallel (throttled).

        Used by all three triggers after building/confirming contexts.
        """
        self._ensure_legacy_direct_mutation_allowed()

        async def _throttled(c: EvolutionContext) -> Optional[SkillRecord]:
            async with self._semaphore:
                return await self._evolve_context(c)

        raw = await asyncio.gather(
            *[_throttled(c) for c in contexts],
            return_exceptions=True,
        )
        results: List[SkillRecord] = []
        for r in raw:
            if isinstance(r, BaseException):
                logger.error(f"[Trigger:{trigger_label}] Evolution task raised: {r}")
            elif r is not None:
                results.append(r)

        if results:
            names = [r.name for r in results]
            logger.info(
                f"[Trigger:{trigger_label}] Evolved {len(results)} skill(s): {names}"
            )
        return results

    def schedule_background(
        self,
        coro,
        *,
        label: str = "background_evolution",
    ) -> Optional[asyncio.Task]:
        """Launch a coroutine as a background ``asyncio.Task``.

        Used by the runtime post-execution quality evolution path when
        ``background_triggers`` is True.  The task is tracked so it can
        be awaited on shutdown via ``wait_background()``.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(f"No running event loop — cannot schedule {label}")
            return None

        task = loop.create_task(coro, name=label)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        task.add_done_callback(self._log_background_result)
        return task

    @staticmethod
    def _log_background_result(task: asyncio.Task) -> None:
        """Log the outcome of a background evolution task."""
        if task.cancelled():
            logger.debug(f"Background task '{task.get_name()}' was cancelled")
            return
        exc = task.exception()
        if exc:
            logger.error(
                f"Background task '{task.get_name()}' failed: {exc}",
                exc_info=exc,
            )

    async def _evolve_fix(self, ctx: EvolutionContext) -> Optional[SkillRecord]:
        """In-place fix: same name, same directory, new version record.

        Uses explicit ``call_model()`` + ``run_tools()`` turns for
        information gathering before the apply-retry cycle.
        """
        self._ensure_legacy_direct_mutation_allowed()
        if not ctx.skill_records or not ctx.skill_contents or not ctx.skill_dirs:
            logger.warning("FIX requires exactly 1 parent (skill_records/contents/dirs)")
            return None

        parent = ctx.skill_records[0]
        parent_content = ctx.skill_contents[0]
        parent_dir = ctx.skill_dirs[0]

        # Build prompt with full directory content for multi-file skills
        dir_content = self._format_skill_dir_content(parent_dir)
        prompt = SkillEnginePrompts.evolution_fix(
            current_content=_truncate(dir_content or parent_content, _SKILL_CONTENT_MAX_CHARS),
            direction=ctx.suggestion.direction,
            failure_context=self._format_analysis_context(ctx.recent_analyses),
        )

        # Agent loop: LLM can gather information via tools before generating edits
        evolution_output = await self._run_evolution_loop(prompt, ctx)
        if not evolution_output:
            return None

        new_content = evolution_output.edit_content
        overlay_fields = evolution_output.overlay_fields
        overlay_metadata = evolution_output.overlay_metadata

        change_summary = evolution_output.change_summary

        # Apply-retry cycle
        edit_result = await self._apply_with_retry(
            apply_fn=lambda content: fix_skill(parent_dir, content, PatchType.AUTO),
            initial_content=new_content,
            skill_dir=parent_dir,
            ctx=ctx,
            prompt=prompt,
        )
        if edit_result is None or not edit_result.ok:
            return None

        # Re-read name/description from the updated SKILL.md on disk —
        # the LLM may have refined the description (or even name) during the fix.
        updated_skill_md = edit_result.content_snapshot.get(SKILL_FILENAME, "")
        fixed_name = _extract_frontmatter_field(updated_skill_md, "name") or parent.name
        fixed_desc = _extract_frontmatter_field(updated_skill_md, "description") or parent.description

        new_id = f"{fixed_name}__v{parent.lineage.generation + 1}_{uuid.uuid4().hex[:8]}"
        model = self._model or self._llm_client.model

        new_record = SkillRecord(
            skill_id=new_id,
            name=fixed_name,
            description=fixed_desc,
            path=parent.path,
            trust_state=SkillTrustState.PROVISIONAL,
            category=parent.category,
            tags=list(parent.tags),
            visibility=parent.visibility,
            creator_id=parent.creator_id,
            lineage=SkillLineage(
                origin=SkillOrigin.FIXED,
                generation=parent.lineage.generation + 1,
                parent_skill_ids=[parent.skill_id],
                source_task_id=ctx.source_task_id,
                change_summary=change_summary or ctx.suggestion.direction,
                content_diff=edit_result.content_diff,
                content_snapshot=edit_result.content_snapshot,
                created_by=model,
            ),
            tool_dependencies=list(parent.tool_dependencies),
            critical_tools=list(parent.critical_tools),
        )

        await self._store.evolve_skill(new_record, [parent.skill_id])

        # Stamp the new skill_id into the sidecar file so next discover()
        write_skill_id(parent_dir, new_id)

        await self._write_generated_runtime_overlay(
            new_id,
            overlay_fields,
            overlay_metadata,
            ctx,
        )

        new_meta = self._registry.load_skill_from_dir(parent_dir)
        if new_meta is not None:
            self._registry.update_skill(parent.skill_id, new_meta)

        logger.info(
            f"FIX: {parent.name} gen{parent.lineage.generation} → "
            f"gen{new_record.lineage.generation} [{new_id}]"
        )
        return new_record
    
    async def _evolve_derived(self, ctx: EvolutionContext) -> Optional[SkillRecord]:
        """Create enhanced version in a new directory.

        Supports single-parent (enhance) and multi-parent (merge/fuse).
        Uses agent loop for information gathering + apply-retry cycle.
        """
        self._ensure_legacy_direct_mutation_allowed()
        if not ctx.skill_records or not ctx.skill_contents or not ctx.skill_dirs:
            logger.warning("DERIVED requires at least one parent skill_record + content + dir")
            return None

        first_parent = ctx.skill_records[0]   # For fallback defaults only
        is_merge = len(ctx.skill_records) > 1

        # Build prompt — include all parent contents for multi-parent merge
        if is_merge:
            parent_sections = []
            for i, (rec, sd) in enumerate(zip(ctx.skill_records, ctx.skill_dirs)):
                dir_content = self._format_skill_dir_content(sd)
                label = f"Parent {i + 1}: {rec.name}"
                parent_sections.append(
                    f"## {label}\n{_truncate(dir_content or ctx.skill_contents[i], _SKILL_CONTENT_MAX_CHARS)}"
                )
            combined_content = "\n\n---\n\n".join(parent_sections)
        else:
            dir_content = self._format_skill_dir_content(ctx.skill_dirs[0])
            combined_content = _truncate(dir_content or ctx.skill_contents[0], _SKILL_CONTENT_MAX_CHARS)

        prompt = SkillEnginePrompts.evolution_derived(
            parent_content=combined_content,
            direction=ctx.suggestion.direction,
            execution_insights=self._format_analysis_context(ctx.recent_analyses),
        )

        # Agent loop
        evolution_output = await self._run_evolution_loop(prompt, ctx)
        if not evolution_output:
            return None

        new_content = evolution_output.edit_content
        overlay_fields = evolution_output.overlay_fields
        overlay_metadata = evolution_output.overlay_metadata
        change_summary = evolution_output.change_summary

        # Determine new skill name from frontmatter, or generate one
        new_name = _extract_frontmatter_field(new_content, "name")
        if not new_name or new_name == first_parent.name:
            suffix = "-merged" if is_merge else "-enhanced"
            new_name = f"{first_parent.name}{suffix}"
            new_content = _set_frontmatter_field(new_content, "name", new_name)

        # Cap name length to avoid ever-growing chains like
        # "panel-component-enhanced-enhanced-merged_abc123"
        new_name = _sanitize_skill_name(new_name)
        new_content = _set_frontmatter_field(new_content, "name", new_name)

        # Directory name always matches the skill name
        target_dir = ctx.skill_dirs[0].parent / new_name
        if target_dir.exists():
            new_name = f"{new_name}-{uuid.uuid4().hex[:6]}"
            new_name = _sanitize_skill_name(new_name)
            target_dir = ctx.skill_dirs[0].parent / new_name
            new_content = _set_frontmatter_field(new_content, "name", new_name)

        # Apply-retry cycle for derive_skill
        edit_result = await self._apply_with_retry(
            apply_fn=lambda content: derive_skill(ctx.skill_dirs, target_dir, content, PatchType.AUTO),
            initial_content=new_content,
            skill_dir=target_dir,
            ctx=ctx,
            prompt=prompt,
            cleanup_on_retry=target_dir,  # Remove failed target dir before retry
        )
        if edit_result is None or not edit_result.ok:
            return None

        # Extract description from new content
        new_desc = _extract_frontmatter_field(new_content, "description") or first_parent.description

        # Collect parent info from ALL parents
        parent_ids = [r.skill_id for r in ctx.skill_records]
        max_gen = max(r.lineage.generation for r in ctx.skill_records)
        all_tool_deps: set = set()
        all_critical: set = set()
        all_tags: set = set()
        for rec in ctx.skill_records:
            all_tool_deps.update(rec.tool_dependencies)
            all_critical.update(rec.critical_tools)
            all_tags.update(rec.tags)

        new_id = f"{new_name}__v0_{uuid.uuid4().hex[:8]}"
        model = self._model or self._llm_client.model

        new_record = SkillRecord(
            skill_id=new_id,
            name=new_name,
            description=new_desc,
            path=str(target_dir / SKILL_FILENAME),
            trust_state=SkillTrustState.PROVISIONAL,
            category=ctx.suggestion.category or first_parent.category,
            tags=sorted(all_tags),
            visibility=first_parent.visibility,
            creator_id=first_parent.creator_id,
            lineage=SkillLineage(
                origin=SkillOrigin.DERIVED,
                generation=max_gen + 1,
                parent_skill_ids=parent_ids,
                source_task_id=ctx.source_task_id,
                change_summary=change_summary or ctx.suggestion.direction,
                content_diff=edit_result.content_diff,
                content_snapshot=edit_result.content_snapshot,
                created_by=model,
            ),
            tool_dependencies=sorted(all_tool_deps),
            critical_tools=sorted(all_critical),
        )

        await self._store.evolve_skill(new_record, parent_ids)
        await self._record_origin_trust(new_record, ctx)

        # Stamp skill_id sidecar so discover() uses this ID on restart
        write_skill_id(target_dir, new_id)

        target_dir = await self._materialize_generated_local_taxonomy(
            record=new_record,
            target_dir=target_dir,
            ctx=ctx,
            parent_skill_ids=parent_ids,
            action_type="DERIVED",
        )

        await self._write_generated_runtime_overlay(
            new_id,
            overlay_fields,
            overlay_metadata,
            ctx,
        )

        # Register the new skill so it's immediately available with the full
        # OpenSpace runtime contract parsed from its SKILL.md frontmatter.
        new_meta = self._registry.load_skill_from_dir(target_dir)
        if new_meta is not None:
            self._registry.add_skill(new_meta)

        parent_names = " + ".join(r.name for r in ctx.skill_records)
        logger.info(f"DERIVED: {parent_names} → {new_name} [{new_id}]")
        return new_record

    async def _evolve_captured(self, ctx: EvolutionContext) -> Optional[SkillRecord]:
        """Capture a novel pattern as a brand-new skill.

        Uses agent loop for information gathering + apply-retry cycle.
        """
        self._ensure_legacy_direct_mutation_allowed()
        # Build prompt and call LLM
        # For CAPTURED, we use analyses as context (the tasks where the pattern was observed)
        task_descriptions = []
        for a in ctx.recent_analyses[:_ANALYSIS_CONTEXT_MAX]:
            if a.execution_note:
                task_descriptions.append(
                    f"- task={a.task_id}: {a.execution_note[:200]}"
                )

        prompt = SkillEnginePrompts.evolution_captured(
            direction=ctx.suggestion.direction,
            category=(ctx.suggestion.category or SkillCategory.WORKFLOW).value,
            execution_highlights="\n".join(task_descriptions) if task_descriptions else "(no task context available)",
        )

        # Agent loop
        evolution_output = await self._run_evolution_loop(prompt, ctx)
        if not evolution_output:
            return None

        new_content = evolution_output.edit_content
        overlay_fields = evolution_output.overlay_fields
        overlay_metadata = evolution_output.overlay_metadata
        change_summary = evolution_output.change_summary

        # Extract name/description from the generated content
        new_name = _extract_frontmatter_field(new_content, "name")
        new_desc = _extract_frontmatter_field(new_content, "description")
        if not new_name:
            logger.warning("CAPTURED: LLM did not produce a valid skill name")
            return None

        # Sanitize name (enforce length limit + valid chars)
        new_name = _sanitize_skill_name(new_name)
        new_content = _set_frontmatter_field(new_content, "name", new_name)

        # Create new skill directory via create_skill (handles multi-file FULL)
        # Priority chain for choosing the target skill root:
        #   1. ctx.capture_dir — explicitly set from host agent's skill_dirs param
        #   2. Infer from analysis — if this task used a skill from dir B,
        #      captured skills belong alongside it (same host agent context)
        #   3. registry._skill_dirs[0] — ultimate fallback
        base_dir: Optional[Path] = None
        if ctx.capture_dir and ctx.capture_dir.is_dir():
            base_dir = ctx.capture_dir
        else:
            base_dir = self._infer_capture_dir_from_analysis(ctx)

        if base_dir is None:
            skill_dirs = self._registry._skill_dirs
            if not skill_dirs:
                logger.warning("CAPTURED: no skill directories configured")
                return None
            base_dir = skill_dirs[0]
        target_dir = base_dir / new_name
        if target_dir.exists():
            new_name = f"{new_name}-{uuid.uuid4().hex[:6]}"
            new_name = _sanitize_skill_name(new_name)
            target_dir = base_dir / new_name
            new_content = _set_frontmatter_field(new_content, "name", new_name)

        # Apply-retry cycle for create_skill
        edit_result = await self._apply_with_retry(
            apply_fn=lambda content: create_skill(target_dir, content, PatchType.AUTO),
            initial_content=new_content,
            skill_dir=target_dir,
            ctx=ctx,
            prompt=prompt,
            cleanup_on_retry=target_dir,
        )
        if edit_result is None or not edit_result.ok:
            return None

        snapshot = edit_result.content_snapshot
        add_all_diff = edit_result.content_diff

        new_id = f"{new_name}__v0_{uuid.uuid4().hex[:8]}"
        model = self._model or self._llm_client.model

        new_record = SkillRecord(
            skill_id=new_id,
            name=new_name,
            description=new_desc or new_name,
            path=str(target_dir / SKILL_FILENAME),
            trust_state=SkillTrustState.PROVISIONAL,
            category=ctx.suggestion.category or SkillCategory.WORKFLOW,
            lineage=SkillLineage(
                origin=SkillOrigin.CAPTURED,
                generation=0,
                parent_skill_ids=[],
                source_task_id=ctx.source_task_id,
                change_summary=change_summary or ctx.suggestion.direction,
                content_diff=add_all_diff,
                content_snapshot=snapshot,
                created_by=model,
            ),
        )

        await self._store.save_record(new_record)
        await self._record_origin_trust(new_record, ctx)

        # Stamp skill_id sidecar so discover() uses this ID on restart
        write_skill_id(target_dir, new_id)

        target_dir = await self._materialize_generated_local_taxonomy(
            record=new_record,
            target_dir=target_dir,
            ctx=ctx,
            parent_skill_ids=[],
            action_type="CAPTURED",
        )

        await self._write_generated_runtime_overlay(
            new_id,
            overlay_fields,
            overlay_metadata,
            ctx,
        )

        # Register the new skill so it's immediately available with the full
        # OpenSpace runtime contract parsed from its SKILL.md frontmatter.
        new_meta = self._registry.load_skill_from_dir(target_dir)
        if new_meta is not None:
            self._registry.add_skill(new_meta)

        logger.info(f"CAPTURED: {new_name} [{new_id}]")
        return new_record

    async def _materialize_generated_local_taxonomy(
        self,
        *,
        record: SkillRecord,
        target_dir: Path,
        ctx: EvolutionContext,
        parent_skill_ids: list[str],
        action_type: str,
    ) -> Path:
        try:
            from openspace.cloud.local_mapping import CloudLocalMappingStore
            from openspace.cloud.skill_classification import (
                build_local_category_path,
                classify_skill_dir,
                initialize_local_skill_taxonomy,
                materialize_skill_category_tree,
                persist_skill_classification,
            )

            db_path = getattr(self._store, "db_path", None)
            if db_path is None and getattr(self._store, "base", None) is not None:
                db_path = getattr(self._store.base, "db_path", None)
            mapping_store = CloudLocalMappingStore(db_path)
            try:
                parent_records = []
                for parent_id in parent_skill_ids:
                    parent_record = self._store.load_record(parent_id)
                    if parent_record is not None:
                        parent_records.append(parent_record)
                if parent_records:
                    initialize_local_skill_taxonomy(
                        mapping_store=mapping_store,
                        skills=parent_records,
                    )
                parent_classification = None
                parent_cloud_path = ""
                for parent_id in parent_skill_ids:
                    parent_classification = mapping_store.get_skill_local_classification(parent_id)
                    parent_binding = mapping_store.get_skill_cloud_binding_by_local(parent_id)
                    if parent_binding is not None and not parent_cloud_path:
                        parent_cloud_path = (
                            parent_binding.current_package_path
                            or parent_binding.package_path_at_pull
                            or ""
                        )
                    if parent_classification is not None:
                        break

                decision_path = str(getattr(ctx.suggestion, "local_category_path", "") or "").strip()
                inherited_path = (
                    parent_classification.local_category_path
                    if parent_classification is not None
                    and parent_classification.local_category_path
                    and action_type == "DERIVED"
                    else ""
                )
                selected_path = decision_path or inherited_path
                category = record.category.value
                local_category_path = build_local_category_path(
                    category,
                    local_category_path=selected_path,
                    cloud_package_path=parent_cloud_path or None,
                    local_path=str(target_dir),
                    name=record.name,
                )
                classification = classify_skill_dir(
                    target_dir,
                    local_skill_id=record.skill_id,
                    cloud_package_path=parent_cloud_path or None,
                    local_category=category,
                    local_category_path=local_category_path,
                    origin="derive" if action_type == "DERIVED" else "capture",
                )
                classification = replace(
                    classification,
                    category=category,
                    local_category_path=local_category_path,
                    evidence={
                        **dict(classification.evidence or {}),
                        "origin": "derive" if action_type == "DERIVED" else "capture",
                        "evolution_action_type": action_type,
                        "parent_skill_ids": list(parent_skill_ids),
                    },
                )
                saved = persist_skill_classification(mapping_store, classification)
                materialized_dir = materialize_skill_category_tree(
                    target_dir,
                    saved,
                    skills_root=target_dir.parent,
                )
                if materialized_dir != target_dir:
                    record.path = str(materialized_dir / SKILL_FILENAME)
                    await self._store.save_record(record)
                return materialized_dir
            finally:
                mapping_store.close()
        except Exception as exc:
            logger.debug("generated local taxonomy materialization skipped: %s", exc)
            return target_dir

    def _infer_capture_dir_from_analysis(
        self, ctx: EvolutionContext,
    ) -> Optional[Path]:
        """Infer the best skill root for a CAPTURED skill from analysis context.

        When ``capture_dir`` is not explicitly set (no ``skill_dirs`` param
        from the host agent), we look at which skills were used during the
        task that triggered the capture.  If a used skill lives under one
        of the registered skill roots, that root is a reasonable home for
        the new captured skill (same host agent context).
        """
        if not ctx.recent_analyses:
            return None

        registry_roots = self._registry._skill_dirs
        if not registry_roots:
            return None

        for analysis in ctx.recent_analyses:
            for judgment in analysis.skill_judgments:
                if not judgment.skill_applied:
                    continue
                rec = self._store.load_record(judgment.skill_id)
                if not rec or not rec.path:
                    continue
                skill_path = Path(rec.path).parent  # e.g. /A/foo/
                for root in registry_roots:
                    try:
                        skill_path.relative_to(root)
                        logger.debug(
                            "CAPTURED: inferred capture dir %s from "
                            "applied skill %s", root, judgment.skill_id,
                        )
                        return root
                    except ValueError:
                        continue

        return None

    async def _run_evolution_loop(
        self,
        prompt: str,
        ctx: EvolutionContext,
    ) -> Optional[_EvolutionFinalOutput]:
        """Run evolution as a structured-finalization agent loop.

        Modeled after ``GroundingAgent.process()`` — the loop continues
        until the LLM outputs an explicit finalization JSON block, NOT
        based on whether tools were called.

        Termination signals (checked every iteration, regardless of tool use):
          - finalization ``status=complete`` → success, return edit payload.
          - finalization ``status=failed``   → failure, return None.

        Tool availability:
          - Iterations 1 … N-1: tools enabled (LLM may gather information).
          - Iteration N (final): tools disabled, LLM must output a decision.

        Each non-final iteration without finalization gets a nudge message
        telling the LLM which iteration it is on and how many remain.

        Conversations are recorded to ``conversations.jsonl`` via
        ``RecordingManager`` (agent_name="SkillEvolver") so the full
        evolution dialogue is preserved for debugging and replay.
        """
        from openspace.recording import RecordingManager

        model = self._model or self._llm_client.model

        # Merge tools from context and instance-level
        evolution_tools: List["BaseTool"] = list(ctx.available_tools or [])
        if not evolution_tools:
            evolution_tools = list(self._available_tools)

        messages: List[Dict[str, Any]] = [
            {"role": "user", "content": prompt},
        ]

        # Record initial conversation setup
        await RecordingManager.record_conversation_setup(
            setup_messages=copy.deepcopy(messages),
            tools=evolution_tools if evolution_tools else None,
            agent_name="SkillEvolver",
            extra={
                "evolution_type": ctx.suggestion.evolution_type.value,
                "trigger": ctx.trigger.value,
                "target_skills": ctx.suggestion.target_skill_ids,
            },
        )

        length_recovery_count = 0

        for iteration in range(_MAX_EVOLUTION_ITERATIONS):
            is_last = iteration == _MAX_EVOLUTION_ITERATIONS - 1
            is_length_recovery = length_recovery_count > 0

            # Snapshot message count before any additions + LLM call
            msg_count_before = len(messages)

            # Final round: disable tools and force a decision
            if is_last:
                messages.append({
                    "role": "system",
                    "content": (
                        f"This is your FINAL round (iteration "
                        f"{iteration + 1}/{_MAX_EVOLUTION_ITERATIONS}) — "
                        f"no more tool calls allowed. "
                        f"You MUST output either the skill edit content followed "
                        f"by a structured Evolution Finalization block with "
                        f'\"status\": \"complete\", or only a finalization block '
                        f'with \"status\": \"failed\" and a reason. Follow the '
                        f"output format specified in the original instructions."
                    ),
                })

            try:
                model_response = await self._call_evolution_model(
                    messages=messages,
                    tools=(
                        evolution_tools
                        if (evolution_tools and not is_last and not is_length_recovery)
                        else None
                    ),
                    model=model,
                )
            except Exception as e:
                logger.error(f"Evolution LLM call failed (iter {iteration + 1}): {e}")
                return None
            # Keep fallback state local to this evolution run.  The helper
            # exposes the model that produced the response without mutating
            # the shared client defaults.
            model = model_response.effective_model or model

            assistant_message = model_response.assistant_message
            messages.append(assistant_message)
            raw_content = assistant_message.get("content", "")
            content = (
                raw_content if isinstance(raw_content, str) else str(raw_content or "")
            )
            has_tool_calls = bool(model_response.tool_calls)
            followup_messages = self._llm_client.get_model_response_followup_messages(
                model_response
            )
            has_api_error = self._llm_client.model_response_has_api_error(
                model_response
            )
            recover_length = bool(
                has_api_error
                and model_response.stop_reason == "length"
                and not has_tool_calls
                and not is_last
                and length_recovery_count < _MAX_EVOLUTION_LENGTH_RECOVERIES
            )
            if recover_length:
                length_recovery_count += 1
                messages.append(
                    _build_evolution_length_recovery_message(length_recovery_count)
                )
            elif followup_messages:
                messages.extend(followup_messages)

            tool_results: list[dict[str, Any]] = []
            prevent_continuation = False
            tool_stop_reason: str | None = None

            if not has_api_error and has_tool_calls and evolution_tools and not is_last:
                tool_context = self._llm_client.build_auxiliary_tool_use_context(
                    tools=evolution_tools,
                    messages=messages,
                    model=model,
                    agent_id="skill_evolver",
                    agent_type=self.__class__.__name__,
                    task_description=prompt,
                    current_iteration=iteration + 1,
                    max_iterations=_MAX_EVOLUTION_ITERATIONS,
                )
                tools_result = await run_tools(
                    model_response.tool_calls,
                    model_response.tool_map,
                    tool_context,
                    assistant_message=assistant_message,
                )
                messages.extend(tools_result.messages)
                prevent_continuation = tools_result.prevent_continuation
                tool_stop_reason = tools_result.stop_reason
                tool_results = self._llm_client.collect_tool_results(
                    model_response.tool_calls,
                    model_response.tool_map,
                    tools_result.messages,
                )

            updated_messages = messages

            # Record iteration delta
            delta = updated_messages[msg_count_before:]
            await RecordingManager.record_iteration_context(
                iteration=iteration + 1,
                delta_messages=copy.deepcopy(delta),
                response_metadata={
                    "has_tool_calls": has_tool_calls,
                    "tool_calls_count": len(tool_results),
                    "has_finalization_block": bool(
                        content and _EVOLUTION_FINALIZATION_BLOCK_RE.search(content)
                    ),
                    "prevent_continuation": prevent_continuation,
                    "stop_reason": model_response.stop_reason,
                    "tool_stop_reason": tool_stop_reason,
                    "content_length": len(content),
                    "output_tokens": int(
                        getattr(model_response.usage, "output_tokens", 0) or 0
                    ),
                    "effective_model": model_response.effective_model or model,
                    "has_api_error": has_api_error,
                    "api_error_followup_count": len(followup_messages),
                    "length_recovery_attempt": (
                        length_recovery_count if recover_length else 0
                    ),
                },
                agent_name="SkillEvolver",
            )

            messages = updated_messages

            if has_api_error:
                if recover_length:
                    logger.warning(
                        "Evolution LLM response was truncated; retrying with "
                        "compact finalization (iter=%s stop_reason=%s "
                        "output_tokens=%s content_chars=%s recovery=%s/%s)",
                        iteration + 1,
                        model_response.stop_reason or "unknown",
                        int(getattr(model_response.usage, "output_tokens", 0) or 0),
                        len(content),
                        length_recovery_count,
                        _MAX_EVOLUTION_LENGTH_RECOVERIES,
                    )
                    continue
                logger.warning(
                    "Evolution LLM returned a non-recoverable API error "
                    "followup (iter=%s stop_reason=%s output_tokens=%s "
                    "content_chars=%s)",
                    iteration + 1,
                    model_response.stop_reason or "unknown",
                    int(getattr(model_response.usage, "output_tokens", 0) or 0),
                    len(content),
                )
                return None

            if prevent_continuation:
                logger.warning(
                    "Evolution agent stopped after tool hooks prevented "
                    "continuation: %s",
                    tool_stop_reason or "hook_stopped",
                )
                return None

            # ── Finalization check (every iteration, regardless of tool calls) ──
            if content:
                final_output, failure_reason, found_finalization = (
                    _extract_evolution_finalization(content)
                )
                if failure_reason is not None:
                    targets = "+".join(ctx.suggestion.target_skill_ids) or "(new)"
                    logger.warning(
                        f"Evolution LLM signalled failure "
                        f"[{ctx.suggestion.evolution_type.value}] "
                        f"target={targets}: {failure_reason}"
                    )
                    return None
                if final_output is not None:
                    return final_output
                if found_finalization:
                    return None

            # No finalization found
            if is_last:
                # Final round exhausted without a decision
                logger.warning(
                    f"Evolution agent finished {_MAX_EVOLUTION_ITERATIONS} iterations "
                    f"without a structured finalization block"
                )
                return None

            if has_tool_calls:
                logger.debug(
                    f"Evolution agent used tools "
                    f"(iter {iteration + 1}/{_MAX_EVOLUTION_ITERATIONS})"
                )
            else:
                # No tools, no finalization — nudge the LLM
                logger.debug(
                    f"Evolution agent produced content without finalization or tools "
                    f"(iter {iteration + 1}/{_MAX_EVOLUTION_ITERATIONS})"
                )

            # Iteration guidance
            remaining = _MAX_EVOLUTION_ITERATIONS - iteration - 1
            messages.append({
                "role": "system",
                "content": (
                    f"Iteration {iteration + 1}/{_MAX_EVOLUTION_ITERATIONS} complete "
                    f"({remaining} remaining). "
                    f"If your edit is ready, output it followed by an Evolution "
                    f"Finalization block with status=complete. "
                    f"If you cannot complete this evolution, output only an "
                    f"Evolution Finalization block with status=failed and a reason. "
                    f"Otherwise, continue gathering information with tools."
                ),
            })

        # Should never reach here (is_last handles the final iteration)
        return None

    async def _apply_with_retry(
        self,
        *,
        apply_fn,
        initial_content: str,
        skill_dir: Path,
        ctx: EvolutionContext,
        prompt: str,
        cleanup_on_retry: Optional[Path] = None,
    ) -> Optional[SkillEditResult]:
        """Apply an edit with retry on failure.

        If the first attempt fails (patch parse error, path mismatch, etc.),
        feeds the error back to the LLM and asks for a corrected version.

        After successful application, runs structural validation.

        Retry conversations are recorded to ``conversations.jsonl`` under
        agent_name="SkillEvolver.retry" so failed apply attempts and LLM
        corrections are preserved for debugging.

        Args:
            apply_fn: Callable that takes content str and returns SkillEditResult.
            initial_content: First LLM-generated content to try.
            skill_dir: Skill directory for validation.
            ctx: Evolution context (for retry LLM calls).
            prompt: Original prompt (for retry context).
            cleanup_on_retry: Directory to remove before retrying (for derive/create).
        """
        from openspace.recording import RecordingManager

        current_content = initial_content
        msg_history: List[Dict[str, Any]] = [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": initial_content},
        ]

        # Track whether we've recorded the retry setup (only on first retry)
        retry_setup_recorded = False
        model = self._model or self._llm_client.model

        for attempt in range(_MAX_EVOLUTION_ATTEMPTS):
            # Clean up previous failed attempt (for derive/create)
            if attempt > 0 and cleanup_on_retry and cleanup_on_retry.exists():
                shutil.rmtree(cleanup_on_retry, ignore_errors=True)

            # Apply the edit
            edit_result = apply_fn(current_content)

            if edit_result.ok:
                # Validate the result
                validation_error = _validate_skill_dir(skill_dir)
                if validation_error is None:
                    if attempt > 0:
                        logger.info(
                            f"Apply-retry succeeded on attempt {attempt + 1}/{_MAX_EVOLUTION_ATTEMPTS}"
                        )
                    return edit_result
                else:
                    # Validation failed — treat as error for retry
                    error_msg = f"Validation failed: {validation_error}"
                    logger.warning(
                        f"Apply succeeded but validation failed "
                        f"(attempt {attempt + 1}/{_MAX_EVOLUTION_ATTEMPTS}): "
                        f"{validation_error}"
                    )
            else:
                error_msg = edit_result.error or "Unknown apply error"
                logger.warning(
                    f"Apply failed (attempt {attempt + 1}/{_MAX_EVOLUTION_ATTEMPTS}): "
                    f"{error_msg}"
                )

            # Last attempt? Give up.
            if attempt >= _MAX_EVOLUTION_ATTEMPTS - 1:
                logger.error(
                    f"Apply-retry exhausted after {_MAX_EVOLUTION_ATTEMPTS} attempts. "
                    f"Last error: {error_msg}"
                )
                # Clean up any partially created directory
                if cleanup_on_retry and cleanup_on_retry.exists():
                    shutil.rmtree(cleanup_on_retry, ignore_errors=True)
                return None

            # Record retry setup on first retry attempt
            if not retry_setup_recorded:
                await RecordingManager.record_conversation_setup(
                    setup_messages=copy.deepcopy(msg_history),
                    agent_name="SkillEvolver.retry",
                    extra={
                        "evolution_type": ctx.suggestion.evolution_type.value,
                        "target_skills": ctx.suggestion.target_skill_ids,
                        "first_error": error_msg[:300],
                    },
                )
                retry_setup_recorded = True

            # Feed error back to LLM for retry, including current file
            # content so the LLM doesn't hallucinate what's on disk.
            current_on_disk = self._format_skill_dir_content(skill_dir) if skill_dir.is_dir() else ""
            retry_prompt = (
                f"The previous edit was not successful. "
                f"This was the error:\n\n{error_msg}\n\n"
            )
            if current_on_disk:
                retry_prompt += (
                    f"Here is the CURRENT content of the skill files on disk "
                    f"(use this as the ground truth for any SEARCH/REPLACE or "
                    f"context anchors):\n\n{_truncate(current_on_disk, _SKILL_CONTENT_MAX_CHARS)}\n\n"
                )
            retry_prompt += (
                f"Please fix the issue and generate the edit again. "
                f"Follow the same output format as before."
            )
            msg_history.append({"role": "user", "content": retry_prompt})

            # Call LLM for corrected version (no tools — just fix the edit)
            try:
                result = await self._call_evolution_model(
                    messages=msg_history,
                    model=model,
                )
                model = result.effective_model or model
                new_content = result.assistant_message.get("content", "")
                if not new_content:
                    logger.warning("Retry LLM returned empty content")
                    continue

                final_output, failure_reason, found_finalization = (
                    _extract_evolution_finalization(new_content)
                )
                if failure_reason is not None:
                    logger.warning(
                        "Retry LLM signalled evolution failure: %s",
                        failure_reason,
                    )
                    return None
                if final_output is None:
                    if found_finalization:
                        logger.warning("Retry LLM returned unusable finalization")
                    else:
                        logger.warning("Retry LLM did not return finalization block")
                    continue

                new_content = final_output.edit_content
                msg_history.append({"role": "assistant", "content": new_content})
                current_content = new_content

                # Record retry iteration
                await RecordingManager.record_iteration_context(
                    iteration=attempt + 1,
                    delta_messages=[
                        {"role": "user", "content": retry_prompt},
                        {"role": "assistant", "content": new_content},
                    ],
                    response_metadata={
                        "has_tool_calls": False,
                        "attempt": attempt + 1,
                        "error": error_msg[:300],
                    },
                    agent_name="SkillEvolver.retry",
                )

            except Exception as e:
                logger.error(f"Retry LLM call failed: {e}")
                continue

        return None

    def _build_context_from_analysis(
        self,
        analysis: ExecutionAnalysis,
        suggestion: EvolutionSuggestion,
        capture_dir: Optional[Path] = None,
    ) -> Optional[EvolutionContext]:
        """Build EvolutionContext from a single analysis suggestion.

        Loads all target skills referenced by ``suggestion.target_skill_ids``.
        For FIX: exactly 1 parent required.
        For DERIVED: 1+ parents (multi-parent = merge).
        For CAPTURED: parents list is empty; ``capture_dir`` controls where
        the new skill is written (defaults to registry's first skill root).
        """
        records: List[SkillRecord] = []
        contents: List[str] = []
        dirs: List[Path] = []

        if suggestion.evolution_type in (EvolutionType.FIX, EvolutionType.DERIVED):
            if not suggestion.target_skill_ids:
                logger.warning("FIX/DERIVED suggestion missing target_skill_ids")
                return None

            resolved_target_ids: List[str] = []
            for target_id in suggestion.target_skill_ids:
                rec = self._resolve_target_skill_record(target_id)
                if not rec:
                    logger.warning(f"Target skill not found: {target_id}")
                    return None
                content = self._load_skill_content(rec)
                if not content:
                    logger.warning(f"Cannot load content for skill: {target_id}")
                    return None
                skill_dir = Path(rec.path).parent if rec.path else None

                records.append(rec)
                resolved_target_ids.append(rec.skill_id)
                contents.append(content)
                if skill_dir:
                    dirs.append(skill_dir)

            if resolved_target_ids != suggestion.target_skill_ids:
                logger.info(
                    "Resolved evolution targets %s -> %s",
                    suggestion.target_skill_ids,
                    resolved_target_ids,
                )
                suggestion = copy.copy(suggestion)
                suggestion.target_skill_ids = resolved_target_ids

            # FIX must target exactly one skill
            if suggestion.evolution_type == EvolutionType.FIX and len(records) != 1:
                logger.warning(
                    f"FIX requires exactly 1 target, got {len(records)}: "
                    f"{suggestion.target_skill_ids}"
                )
                return None

        return EvolutionContext(
            trigger=EvolutionTrigger.ANALYSIS,
            suggestion=suggestion,
            skill_records=records,
            skill_contents=contents,
            skill_dirs=dirs,
            source_task_id=analysis.task_id,
            recent_analyses=[analysis],
            available_tools=self._available_tools,
            capture_dir=capture_dir,
        )

    def _resolve_target_skill_record(self, target: str) -> Optional[SkillRecord]:
        """Resolve an analyzer target to an active SkillRecord.

        Analyzer prompts ask for true skill IDs, but models sometimes emit the
        human-readable skill name or a path-like directory name.  Accept those
        forms here so a valid FIX/DERIVED suggestion does not fail before the
        evolver can inspect evidence.
        """
        value = str(target or "").strip()
        if not value:
            return None

        rec = self._store.load_record(value)
        if rec:
            return rec

        active_records = self._store.load_active()
        by_name = [
            r for r in active_records.values()
            if r.name == value or Path(r.path).parent.name == value
        ]
        if len(by_name) == 1:
            return by_name[0]
        if len(by_name) > 1:
            logger.warning(
                "Ambiguous evolution target %r matched active skills: %s",
                value,
                [r.skill_id for r in by_name],
            )
            return None

        return None

    def _load_skill_content(self, record: SkillRecord) -> str:
        """Load SKILL.md content from disk via registry or direct read."""
        # Try registry first (uses cache, keyed by skill_id)
        content = self._registry.load_skill_content(record.skill_id)
        if content:
            return content
        # Fallback: read directly from path
        if record.path:
            p = Path(record.path)
            if p.exists():
                try:
                    return p.read_text(encoding="utf-8")
                except Exception:
                    pass
        return ""

    @staticmethod
    def _format_skill_dir_content(skill_dir: Path) -> str:
        """Format all text files in a skill directory for prompt inclusion.

        Returns a multi-file listing if there are auxiliary files beyond
        SKILL.md, or just the SKILL.md content for single-file skills.
        """
        files = collect_skill_snapshot(skill_dir)
        if not files:
            return ""

        # Single-file skill: return just the content
        if len(files) == 1 and SKILL_FILENAME in files:
            return files[SKILL_FILENAME]

        # Multi-file: format as directory listing
        parts: list[str] = []
        # SKILL.md first
        if SKILL_FILENAME in files:
            parts.append(f"### File: {SKILL_FILENAME}\n```markdown\n{files[SKILL_FILENAME]}\n```")
        for name, content in sorted(files.items()):
            if name == SKILL_FILENAME:
                continue
            parts.append(f"### File: {name}\n```\n{content}\n```")

        return "\n\n".join(parts)

    @staticmethod
    def _format_analysis_context(analyses: List[ExecutionAnalysis]) -> str:
        """Format recent analyses into a concise context block for prompts."""
        if not analyses:
            return "(no execution history available)"

        parts: List[str] = []
        for a in analyses[:_ANALYSIS_CONTEXT_MAX]:
            completed = "completed" if a.task_completed else "failed"

            # Per-skill notes
            skill_notes = []
            for j in a.skill_judgments:
                applied = "applied" if j.skill_applied else "NOT applied"
                note = f"  - {j.skill_id}: {applied}"
                if j.note:
                    note += f" — {j.note[:_ANALYSIS_NOTE_MAX_CHARS]}"
                skill_notes.append(note)

            # Tool issues
            tool_lines = []
            for issue in a.tool_issues[:3]:
                tool_lines.append(f"  - {issue[:200]}")

            block = f"### Task: {a.task_id} ({completed})\n"
            if a.execution_note:
                block += f"{a.execution_note[:_ANALYSIS_NOTE_MAX_CHARS]}\n"
            if skill_notes:
                block += "Skills:\n" + "\n".join(skill_notes) + "\n"
            if tool_lines:
                block += "Tool issues:\n" + "\n".join(tool_lines) + "\n"
            parts.append(block)

        return "\n".join(parts)
