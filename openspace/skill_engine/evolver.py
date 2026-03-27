"""SkillEvolver — execute skill evolution actions.

Three evolution types:
  FIX      — repair broken/outdated instructions (in-place, same name)
  DERIVED  — create enhanced version from existing skill (new directory)
  CAPTURED — capture novel reusable pattern from execution (brand new skill)

Three trigger sources:
  1. Post-analysis — analyzer found evolution suggestions for a specific task
  2. Tool degradation — ToolQualityManager detected problematic tools
  3. Metric monitor — periodic scan of skill health indicators

All triggers produce an EvolutionContext → evolve() → LLM agent loop →
apply-retry cycle → validation → store persistence.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import shutil
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, TYPE_CHECKING

from .types import (
    EvolutionSuggestion,
    EvolutionType,
    ExecutionAnalysis,
    SkillCategory,
    SkillLineage,
    SkillOrigin,
    SkillRecord,
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
    extract_change_summary as _extract_change_summary,
    get_frontmatter_field as _extract_frontmatter_field,
    set_frontmatter_field as _set_frontmatter_field,
    strip_markdown_fences as _strip_markdown_fences,
    truncate as _truncate,
    validate_skill_dir as _validate_skill_dir,
)
from .registry import write_skill_id
from .store import SkillStore
from openspace.prompts import SkillEnginePrompts
from openspace.utils.logging import Logger

if TYPE_CHECKING:
    from .registry import SkillRegistry
    from openspace.llm import LLMClient
    from openspace.grounding.core.tool import BaseTool
    from openspace.grounding.core.quality.types import ToolQualityRecord

logger = Logger.get_logger(__name__)

EVOLUTION_COMPLETE = SkillEnginePrompts.EVOLUTION_COMPLETE
EVOLUTION_FAILED = SkillEnginePrompts.EVOLUTION_FAILED

_SKILL_CONTENT_MAX_CHARS = 12_000   # Max chars of SKILL.md in evolution prompt
_MAX_SKILL_NAME_LENGTH = 50         # Max chars for a skill name (directory name)


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

_ANALYSIS_CONTEXT_MAX = 5           # Max recent analyses to include in prompt
_ANALYSIS_NOTE_MAX_CHARS = 500      # Per-analysis note truncation

# Agent loop / retry constants
_MAX_EVOLUTION_ITERATIONS = 5       # Max tool-calling rounds for evolution agent
_MAX_EVOLUTION_ATTEMPTS = 3         # Max apply-retry attempts per evolution

# Rule-based thresholds for candidate screening (relaxed — LLM confirms)
_FALLBACK_THRESHOLD = 0.4           # Relaxed from 0.5 for wider screening
_LOW_COMPLETION_THRESHOLD = 0.35    # Relaxed from 0.3
_HIGH_APPLIED_FOR_FIX = 0.4        # Relaxed from 0.5
_MODERATE_EFFECTIVE_THRESHOLD = 0.55  # Relaxed from 0.5
_MIN_APPLIED_FOR_DERIVED = 0.25    # Relaxed from 0.3


class EvolutionTrigger(str, Enum):
    """What initiated this evolution."""
    ANALYSIS         = "analysis"           # Post-execution analysis suggestion
    TOOL_DEGRADATION = "tool_degradation"   # Tool quality degradation detected
    METRIC_MONITOR   = "metric_monitor"     # Periodic skill health check


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

    # Trigger-specific context
    tool_issue_summary: str = ""             # For TOOL_DEGRADATION
    metric_summary: str = ""                 # For METRIC_MONITOR

    # Available tools for agent loop (read_file, web_search, shell, MCP, etc.)
    available_tools: List["BaseTool"] = field(default_factory=list)


class SkillEvolver:
    """Execute skill evolution actions.

    Single entry point: ``evolve()`` takes an EvolutionContext, runs an
    LLM agent loop (with optional tool use), applies the edit with retry,
    validates the result, and persists the new SkillRecord via ``SkillStore``.

    Concurrency:
        ``max_concurrent`` controls the semaphore that throttles parallel
        evolutions across all trigger types.  File I/O is synchronous and
        naturally serialized by the event loop; only LLM calls run in
        parallel.

    Anti-loop (Trigger 2 — tool degradation):
        ``_addressed_degradations`` is a ``Dict[str, Set[str]]`` mapping
        ``tool_key → {skill_id, …}`` for skills that have already been
        evolved to handle a specific tool's degradation.  At the start of
        each ``process_tool_degradation`` call, tools that are no longer
        in the problematic list are pruned — so if a tool **recovers and
        then degrades again**, all its dependent skills are re-evaluated.

    Anti-loop (Trigger 3 — metric check):
        Newly-evolved skills have ``total_selections=0``, requiring
        ``min_selections`` (default 5) fresh data points before being
        re-evaluated.  This is data-driven and needs no time-based guard.

    Background:
        Trigger 2 and 3 are always launched as ``asyncio.Task``s via
        ``schedule_background()`` so they never block the main flow.
    """

    def __init__(
        self,
        store: SkillStore,
        registry: "SkillRegistry",
        llm_client: "LLMClient",
        model: Optional[str] = None,
        available_tools: Optional[List["BaseTool"]] = None,
        *,
        max_concurrent: int = 3,
    ) -> None:
        self._store = store
        self._registry = registry
        self._llm_client = llm_client
        self._model = model
        self._available_tools: List["BaseTool"] = available_tools or []

        # Concurrency: semaphore limits parallel LLM sessions
        self._max_concurrent = max(1, max_concurrent)
        self._semaphore = asyncio.Semaphore(self._max_concurrent)

        # Anti-loop for Trigger 2: tracks which skills have already been
        # evolved for each degraded tool.  Keyed by tool_key.
        # Pruned when a tool leaves the problematic list (= recovered).
        self._addressed_degradations: Dict[str, Set[str]] = {}

        # Track background tasks so they can be awaited on shutdown.
        self._background_tasks: Set[asyncio.Task] = set()

    def set_available_tools(self, tools: List["BaseTool"]) -> None:
        """Update the tools available for evolution agent loops."""
        self._available_tools = list(tools)

    async def wait_background(self) -> None:
        """Await all outstanding background evolution tasks.

        Call this during shutdown / cleanup to ensure nothing is lost.
        """
        if self._background_tasks:
            logger.info(
                f"Waiting for {len(self._background_tasks)} background "
                f"evolution task(s) to finish..."
            )
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()

    async def evolve(self, ctx: EvolutionContext) -> Optional[SkillRecord]:
        """Execute one evolution action. Returns new SkillRecord or None.

        The global semaphore is NOT acquired here — it is managed at the
        trigger-method level so the concurrency limit covers the whole batch.
        """
        try:
            from gdpval_bench.token_tracker import set_call_source, reset_call_source
            _src_tok = set_call_source("evolver")
        except ImportError:
            _src_tok = None

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
            if _src_tok is not None:
                reset_call_source(_src_tok)

    # Trigger 1: post-analysis
    async def process_analysis(
        self, analysis: ExecutionAnalysis,
    ) -> List[SkillRecord]:
        """Process all evolution suggestions from a completed analysis.

        Called immediately after ``ExecutionAnalyzer.analyze_execution()``.
        Each suggestion becomes one evolution action, executed in parallel
        (throttled by semaphore).
        """
        if not analysis.candidate_for_evolution:
            return []

        # Build contexts first (cheap, no LLM calls)
        contexts: List[EvolutionContext] = []
        for suggestion in analysis.evolution_suggestions:
            ctx = self._build_context_from_analysis(analysis, suggestion)
            if ctx is not None:
                contexts.append(ctx)

        if not contexts:
            return []

        results = await self._execute_contexts(contexts, "analysis")

        if results:
            names = [r.name for r in results]
            logger.info(
                f"[Trigger:analysis] Evolved {len(results)} skill(s): {names} "
                f"from task {analysis.task_id}"
            )
        return results

    # Trigger 2: tool quality degradation
    async def process_tool_degradation(
        self, problematic_tools: List["ToolQualityRecord"],
    ) -> List[SkillRecord]:
        """Fix skills that depend on degraded tools.

        Two-phase: rule-based candidate screening → LLM confirmation.

        Anti-loop (state-driven):
          ``_addressed_degradations[tool_key]`` records skill names that
          have already been evolved for that tool's degradation.  They are
          skipped on subsequent calls as long as the tool stays degraded.

          At the start of each call, tools that **recovered** (no longer
          in ``problematic_tools``) are pruned from the dict — so if the
          tool degrades again later, all dependent skills are re-evaluated.
        """
        if not problematic_tools:
            return []

        # Prune recovered tools: if a tool_key used to be tracked but is
        # no longer in the current problematic list, it recovered — clear
        # its addressed set so future re-degradation gets a fresh pass.
        current_tool_keys = {t.tool_key for t in problematic_tools}
        recovered = [k for k in self._addressed_degradations if k not in current_tool_keys]
        for k in recovered:
            logger.debug(f"[Trigger:tool_degradation] Tool '{k}' recovered, clearing addressed set")
            del self._addressed_degradations[k]

        # Phase 1: screen & confirm candidates
        confirmed_contexts: List[EvolutionContext] = []
        seen_skills: set = set()  # de-dup by skill_id within this call

        for tool_rec in problematic_tools:
            addressed = self._addressed_degradations.get(tool_rec.tool_key, set())

            skill_ids = self._store.find_skills_by_tool(tool_rec.tool_key)
            for skill_id in skill_ids:
                skill_record = self._store.load_record(skill_id)
                if not skill_record or not skill_record.is_active:
                    continue

                # De-duplicate by skill_id within this call
                if skill_record.skill_id in seen_skills:
                    continue
                seen_skills.add(skill_record.skill_id)

                # Anti-loop: already evolved for this tool's degradation
                if skill_record.skill_id in addressed:
                    logger.debug(
                        f"[Trigger:tool_degradation] Skipping '{skill_record.skill_id}' "
                        f"(already addressed for tool '{tool_rec.tool_key}')"
                    )
                    continue

                recent = self._store.load_analyses(skill_id=skill_record.skill_id, limit=_ANALYSIS_CONTEXT_MAX)
                content = self._load_skill_content(skill_record)
                if not content:
                    continue

                issue_summary = (
                    f"Tool `{tool_rec.tool_key}` degraded — "
                    f"recent success rate: {tool_rec.recent_success_rate:.0%}, "
                    f"total calls: {tool_rec.total_calls}, "
                    f"LLM flagged: {tool_rec.llm_flagged_count} time(s)."
                )

                direction = (
                    f"Tool `{tool_rec.tool_key}` has degraded "
                    f"(success_rate={tool_rec.recent_success_rate:.0%}). "
                    f"Update skill instructions to handle this tool's "
                    f"failures gracefully or suggest alternatives."
                )

                # LLM confirmation: ask whether this skill truly needs fixing
                confirmed = await self._llm_confirm_evolution(
                    skill_record=skill_record,
                    skill_content=content,
                    proposed_type=EvolutionType.FIX,
                    proposed_direction=direction,
                    trigger_context=f"Tool degradation: {issue_summary}",
                    recent_analyses=recent,
                )
                if not confirmed:
                    logger.debug(
                        f"[Trigger:tool_degradation] LLM rejected evolution "
                        f"for skill '{skill_record.skill_id}' (tool={tool_rec.tool_key})"
                    )
                    # Even if LLM rejected, mark as addressed to avoid
                    # repeated LLM confirmation calls on every cycle.
                    self._addressed_degradations.setdefault(
                        tool_rec.tool_key, set()
                    ).add(skill_record.skill_id)
                    continue

                skill_dir = Path(skill_record.path).parent if skill_record.path else None
                confirmed_contexts.append(EvolutionContext(
                    trigger=EvolutionTrigger.TOOL_DEGRADATION,
                    suggestion=EvolutionSuggestion(
                        evolution_type=EvolutionType.FIX,
                        target_skill_ids=[skill_record.skill_id],
                        direction=direction,
                    ),
                    skill_records=[skill_record],
                    skill_contents=[content],
                    skill_dirs=[skill_dir] if skill_dir else [],
                    recent_analyses=recent,
                    tool_issue_summary=issue_summary,
                    available_tools=self._available_tools,
                ))

                # Mark as addressed regardless of whether evolution succeeds
                # (if it fails, Trigger 1/3 can pick it up on new data)
                self._addressed_degradations.setdefault(
                    tool_rec.tool_key, set()
                ).add(skill_record.skill_id)

        if not confirmed_contexts:
            return []

        # Phase 2: execute confirmed evolutions in parallel
        results = await self._execute_contexts(confirmed_contexts, "tool_degradation")
        return results

    # Trigger 3: periodic metric check
    async def process_metric_check(
        self, min_selections: int = 5,
    ) -> List[SkillRecord]:
        """Scan active skills and evolve those with poor health metrics.

        Two-phase: rule-based candidate screening (relaxed thresholds) →
        LLM confirmation.  Called periodically (e.g., every N executions).
        Only considers skills with enough data (``min_selections``).

        Anti-loop (data-driven): newly-evolved skills start with
        ``total_selections=0``, so they naturally need ``min_selections``
        fresh executions before being re-evaluated.  No time-based
        cooldown is needed.
        """
        # Phase 1: screen & confirm candidates
        confirmed_contexts: List[EvolutionContext] = []
        all_active = self._store.load_active()

        for skill_id, record in all_active.items():
            if record.total_selections < min_selections:
                continue

            evo_type, direction = self._diagnose_skill_health(record)
            if evo_type is None:
                continue

            content = self._load_skill_content(record)
            if not content:
                continue

            recent = self._store.load_analyses(skill_id=record.skill_id, limit=_ANALYSIS_CONTEXT_MAX)
            metric_summary = (
                f"selections={record.total_selections}, "
                f"applied_rate={record.applied_rate:.0%}, "
                f"completion_rate={record.completion_rate:.0%}, "
                f"effective_rate={record.effective_rate:.0%}, "
                f"fallback_rate={record.fallback_rate:.0%}"
            )

            # LLM confirmation: ask whether this skill truly needs evolution
            confirmed = await self._llm_confirm_evolution(
                skill_record=record,
                skill_content=content,
                proposed_type=evo_type,
                proposed_direction=direction,
                trigger_context=f"Metric check: {metric_summary}",
                recent_analyses=recent,
            )
            if not confirmed:
                logger.debug(
                    f"[Trigger:metric_monitor] LLM rejected evolution "
                    f"for skill '{record.name}' ({evo_type.value})"
                )
                continue

            skill_dir = Path(record.path).parent if record.path else None
            confirmed_contexts.append(EvolutionContext(
                trigger=EvolutionTrigger.METRIC_MONITOR,
                suggestion=EvolutionSuggestion(
                    evolution_type=evo_type,
                    target_skill_ids=[record.skill_id],
                    direction=direction,
                ),
                skill_records=[record],
                skill_contents=[content],
                skill_dirs=[skill_dir] if skill_dir else [],
                recent_analyses=recent,
                metric_summary=metric_summary,
                available_tools=self._available_tools,
            ))

        if not confirmed_contexts:
            return []

        # Phase 2: execute confirmed evolutions in parallel
        results = await self._execute_contexts(confirmed_contexts, "metric_monitor")
        return results

    async def _execute_contexts(
        self,
        contexts: List[EvolutionContext],
        trigger_label: str,
    ) -> List[SkillRecord]:
        """Execute a list of evolution contexts in parallel (throttled).

        Used by all three triggers after building/confirming contexts.
        """
        async def _throttled(c: EvolutionContext) -> Optional[SkillRecord]:
            async with self._semaphore:
                return await self.evolve(c)

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

        Used by the caller (``OpenSpace._maybe_evolve_quality``) when
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

    # LLM confirmation for Trigger 2/3
    async def _llm_confirm_evolution(
        self,
        *,
        skill_record: SkillRecord,
        skill_content: str,
        proposed_type: EvolutionType,
        proposed_direction: str,
        trigger_context: str,
        recent_analyses: List[ExecutionAnalysis],
    ) -> bool:
        """Ask LLM to confirm whether a rule-based evolution candidate
        truly needs evolution.

        Returns True if LLM agrees, False otherwise.
        This prevents false positives from rigid threshold-based rules.

        The confirmation prompt and response are recorded to
        ``conversations.jsonl`` under agent_name="SkillEvolver.confirm".
        """
        try:
            from gdpval_bench.token_tracker import set_call_source, reset_call_source
            _src_tok = set_call_source("evolver")
        except ImportError:
            _src_tok = None

        from openspace.recording import RecordingManager

        analysis_ctx = self._format_analysis_context(recent_analyses)

        prompt = SkillEnginePrompts.evolution_confirm(
            skill_id=skill_record.skill_id,
            skill_content=_truncate(skill_content, _SKILL_CONTENT_MAX_CHARS // 2),
            proposed_type=proposed_type.value,
            proposed_direction=proposed_direction,
            trigger_context=trigger_context,
            recent_analyses=analysis_ctx,
        )

        confirm_messages = [{"role": "user", "content": prompt}]

        # Record confirmation setup
        await RecordingManager.record_conversation_setup(
            setup_messages=copy.deepcopy(confirm_messages),
            agent_name="SkillEvolver.confirm",
            extra={
                "skill_id": skill_record.skill_id,
                "proposed_type": proposed_type.value,
                "trigger_context": trigger_context[:200],
            },
        )

        model = self._model or self._llm_client.model
        try:
            result = await self._llm_client.complete(
                messages=confirm_messages,
                model=model,
            )
            content = result["message"].get("content", "").strip().lower()
            confirmed = self._parse_confirmation(content)

            # Record confirmation response
            await RecordingManager.record_iteration_context(
                iteration=1,
                delta_messages=[{"role": "assistant", "content": content}],
                response_metadata={
                    "has_tool_calls": False,
                    "confirmed": confirmed,
                },
                agent_name="SkillEvolver.confirm",
            )

            return confirmed
        except Exception as e:
            logger.warning(f"LLM confirmation failed, defaulting to skip: {e}")
            return False
        finally:
            if _src_tok is not None:
                reset_call_source(_src_tok)

    @staticmethod
    def _parse_confirmation(response: str) -> bool:
        """Parse LLM confirmation response (expects JSON with 'proceed' field)."""
        # Try JSON parse first
        try:
            # Strip markdown fences
            cleaned = response.strip()
            if cleaned.startswith("```"):
                cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
                cleaned = re.sub(r"\n?```\s*$", "", cleaned)
            data = json.loads(cleaned)
            if isinstance(data, dict):
                return bool(data.get("proceed", False))
        except (json.JSONDecodeError, ValueError):
            pass
        # Fallback: look for keywords.
        # - yes/no use strict word boundaries to avoid false positives
        #   (e.g. "know" matching "no").
        # - confirm/reject/skip use stem-style matching so that common
        #   LLM variants like "confirmed", "rejected", "skipping" still
        #   parse correctly.
        _wb = re.search  # shorthand
        if any(w in response for w in ("\"proceed\": true", "proceed: true")) \
                or _wb(r"\byes\b", response) \
                or _wb(r"\bconfirm\w*\b", response):
            return True
        if any(w in response for w in ("\"proceed\": false", "proceed: false")) \
                or _wb(r"\bno\b", response) \
                or _wb(r"\breject\w*\b", response) \
                or _wb(r"\bskip\w*\b", response):
            return False
        # Default: skip — ambiguous response should not trigger costly evolution
        logger.debug("LLM confirmation response was ambiguous, defaulting to skip")
        return False

    async def _evolve_fix(self, ctx: EvolutionContext) -> Optional[SkillRecord]:
        """In-place fix: same name, same directory, new version record.

        Uses agent loop for information gathering + apply-retry cycle.
        """
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
            tool_issue_summary=ctx.tool_issue_summary,
            metric_summary=ctx.metric_summary,
        )

        # Agent loop: LLM can gather information via tools before generating edits
        new_content = await self._run_evolution_loop(prompt, ctx)
        if not new_content:
            return None

        # Extract change_summary from LLM output (first line if prefixed)
        new_content, change_summary = _extract_change_summary(new_content)

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

        from .registry import SkillMeta
        new_meta = SkillMeta(
            skill_id=new_id,
            name=fixed_name,
            description=fixed_desc,
            path=Path(parent.path),
        )
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
            metric_summary=ctx.metric_summary,
        )

        # Agent loop
        new_content = await self._run_evolution_loop(prompt, ctx)
        if not new_content:
            return None

        new_content, change_summary = _extract_change_summary(new_content)

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

        # Stamp skill_id sidecar so discover() uses this ID on restart
        write_skill_id(target_dir, new_id)

        # Register the new skill so it's immediately available for selection
        from .registry import SkillMeta
        new_meta = SkillMeta(
            skill_id=new_id,
            name=new_name,
            description=new_desc,
            path=target_dir / SKILL_FILENAME,
        )
        self._registry.add_skill(new_meta)

        parent_names = " + ".join(r.name for r in ctx.skill_records)
        logger.info(f"DERIVED: {parent_names} → {new_name} [{new_id}]")
        return new_record

    async def _evolve_captured(self, ctx: EvolutionContext) -> Optional[SkillRecord]:
        """Capture a novel pattern as a brand-new skill.

        Uses agent loop for information gathering + apply-retry cycle.
        """
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
        new_content = await self._run_evolution_loop(prompt, ctx)
        if not new_content:
            return None

        new_content, change_summary = _extract_change_summary(new_content)

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
        skill_dirs = self._registry._skill_dirs
        if not skill_dirs:
            logger.warning("CAPTURED: no skill directories configured")
            return None

        # Directory name always matches the skill name
        base_dir = skill_dirs[0]  # Primary user skill directory
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

        # Stamp skill_id sidecar so discover() uses this ID on restart
        write_skill_id(target_dir, new_id)

        # Register the new skill so it's immediately available
        from .registry import SkillMeta
        new_meta = SkillMeta(
            skill_id=new_id,
            name=new_name,
            description=new_desc or new_name,
            path=target_dir / SKILL_FILENAME,
        )
        self._registry.add_skill(new_meta)

        logger.info(f"CAPTURED: {new_name} [{new_id}]")
        return new_record

    async def _run_evolution_loop(
        self,
        prompt: str,
        ctx: EvolutionContext,
    ) -> Optional[str]:
        """Run evolution as a token-driven agent loop.

        Modeled after ``GroundingAgent.process()`` — the loop continues
        until the LLM outputs an explicit completion/failure token, NOT
        based on whether tools were called.

        Termination signals (checked every iteration, regardless of tool use):
          - ``EVOLUTION_COMPLETE`` in assistant content → success, return edit.
          - ``EVOLUTION_FAILED``   in assistant content → failure, return None.

        Tool availability:
          - Iterations 1 … N-1: tools enabled (LLM may gather information).
          - Iteration N (final): tools disabled, LLM must output a decision.

        Each non-final iteration without a token gets a nudge message
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

        for iteration in range(_MAX_EVOLUTION_ITERATIONS):
            is_last = iteration == _MAX_EVOLUTION_ITERATIONS - 1

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
                        f"You MUST output the skill edit content now based on "
                        f"all information gathered so far. Follow the output "
                        f"format specified in the original instructions. "
                        f"End with {EVOLUTION_COMPLETE} if the edit is satisfactory, "
                        f"or {EVOLUTION_FAILED} with a reason if you cannot produce one."
                    ),
                })

            try:
                result = await self._llm_client.complete(
                    messages=messages,
                    tools=evolution_tools if (evolution_tools and not is_last) else None,
                    execute_tools=True,
                    model=model,
                )
            except Exception as e:
                logger.error(f"Evolution LLM call failed (iter {iteration + 1}): {e}")
                return None

            content = result["message"].get("content", "")
            updated_messages = result["messages"]
            has_tool_calls = result.get("has_tool_calls", False)

            # Record iteration delta
            delta = updated_messages[msg_count_before:]
            await RecordingManager.record_iteration_context(
                iteration=iteration + 1,
                delta_messages=copy.deepcopy(delta),
                response_metadata={
                    "has_tool_calls": has_tool_calls,
                    "tool_calls_count": len(result.get("tool_results", [])),
                    "has_completion_token": bool(
                        content and (EVOLUTION_COMPLETE in content or EVOLUTION_FAILED in content)
                    ),
                },
                agent_name="SkillEvolver",
            )

            messages = updated_messages

            # ── Token check (every iteration, regardless of tool calls) ──
            if content and (EVOLUTION_COMPLETE in content or EVOLUTION_FAILED in content):
                edit_content, failure_reason = self._parse_evolution_output(content)
                if failure_reason is not None:
                    targets = "+".join(ctx.suggestion.target_skill_ids) or "(new)"
                    logger.warning(
                        f"Evolution LLM signalled failure "
                        f"[{ctx.suggestion.evolution_type.value}] "
                        f"target={targets}: {failure_reason}"
                    )
                    return None
                return edit_content

            # No token found
            if is_last:
                # Final round exhausted without a decision
                logger.warning(
                    f"Evolution agent finished {_MAX_EVOLUTION_ITERATIONS} iterations "
                    f"without signalling {EVOLUTION_COMPLETE} or {EVOLUTION_FAILED}"
                )
                return None

            if has_tool_calls:
                logger.debug(
                    f"Evolution agent used tools "
                    f"(iter {iteration + 1}/{_MAX_EVOLUTION_ITERATIONS})"
                )
            else:
                # No tools, no token — nudge the LLM
                logger.debug(
                    f"Evolution agent produced content without token or tools "
                    f"(iter {iteration + 1}/{_MAX_EVOLUTION_ITERATIONS})"
                )

            # Iteration guidance
            remaining = _MAX_EVOLUTION_ITERATIONS - iteration - 1
            messages.append({
                "role": "system",
                "content": (
                    f"Iteration {iteration + 1}/{_MAX_EVOLUTION_ITERATIONS} complete "
                    f"({remaining} remaining). "
                    f"If your edit is ready, output it and include {EVOLUTION_COMPLETE} "
                    f"at the end. "
                    f"If you cannot complete this evolution, output {EVOLUTION_FAILED} "
                    f"with a reason. "
                    f"Otherwise, continue gathering information with tools."
                ),
            })

        # Should never reach here (is_last handles the final iteration)
        return None

    @staticmethod
    def _parse_evolution_output(content: str) -> tuple[Optional[str], Optional[str]]:
        """Extract edit content or failure reason from LLM output.

        MUST only be called when ``EVOLUTION_COMPLETE`` or
        ``EVOLUTION_FAILED`` is present in *content*.

        Returns ``(clean_content, failure_reason)``:
          - ``(content, None)`` — ``EVOLUTION_COMPLETE`` found.
          - ``(None, reason)``  — ``EVOLUTION_FAILED`` found.
        """
        stripped = content.strip()

        # Failure takes priority (if both tokens appear, treat as failure)
        if EVOLUTION_FAILED in stripped:
            idx = stripped.index(EVOLUTION_FAILED)
            reason_part = stripped[idx + len(EVOLUTION_FAILED):].strip()
            if reason_part.lower().startswith("reason:"):
                reason_part = reason_part[len("reason:"):].strip()
            reason = reason_part[:500] if reason_part else "LLM declined to produce edit (no reason given)"
            return None, reason

        if EVOLUTION_COMPLETE in stripped:
            clean = stripped.replace(EVOLUTION_COMPLETE, "").strip()
            clean = _strip_markdown_fences(clean)
            return clean, None

        # Caller guarantees a token is present; defensive fallback
        return None, "No completion token found (unexpected)"

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
            model = self._model or self._llm_client.model
            try:
                result = await self._llm_client.complete(
                    messages=msg_history,
                    model=model,
                )
                new_content = result["message"].get("content", "")
                if not new_content:
                    logger.warning("Retry LLM returned empty content")
                    continue

                new_content = _strip_markdown_fences(new_content)
                # Strip evolution tokens that the LLM may include in retry responses
                new_content = new_content.replace(EVOLUTION_COMPLETE, "").replace(EVOLUTION_FAILED, "").strip()
                new_content, _ = _extract_change_summary(new_content)
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
    ) -> Optional[EvolutionContext]:
        """Build EvolutionContext from a single analysis suggestion.

        Loads all target skills referenced by ``suggestion.target_skill_ids``.
        For FIX: exactly 1 parent required.
        For DERIVED: 1+ parents (multi-parent = merge).
        For CAPTURED: parents list is empty.
        """
        records: List[SkillRecord] = []
        contents: List[str] = []
        dirs: List[Path] = []

        if suggestion.evolution_type in (EvolutionType.FIX, EvolutionType.DERIVED):
            if not suggestion.target_skill_ids:
                logger.warning("FIX/DERIVED suggestion missing target_skill_ids")
                return None

            for target_id in suggestion.target_skill_ids:
                rec = self._store.load_record(target_id)
                if not rec:
                    logger.warning(f"Target skill not found: {target_id}")
                    return None
                content = self._load_skill_content(rec)
                if not content:
                    logger.warning(f"Cannot load content for skill: {target_id}")
                    return None
                skill_dir = Path(rec.path).parent if rec.path else None

                records.append(rec)
                contents.append(content)
                if skill_dir:
                    dirs.append(skill_dir)

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
        )

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

    @staticmethod
    def _diagnose_skill_health(
        record: SkillRecord,
    ) -> tuple[Optional[EvolutionType], str]:
        """Diagnose what type of evolution a skill needs based on metrics.

        Returns (None, "") if the skill appears healthy.
        Thresholds are intentionally relaxed — the LLM confirmation step
        filters out false positives.
        """
        # High fallback rate → skill frequently selected but not used → FIX candidate
        if record.fallback_rate > _FALLBACK_THRESHOLD:
            return EvolutionType.FIX, (
                f"High fallback rate ({record.fallback_rate:.0%}): "
                f"skill is frequently selected but not applied, "
                f"suggesting instructions are unclear or outdated."
            )

        # Applied often but rarely completes → instructions are wrong → FIX candidate
        if (record.applied_rate > _HIGH_APPLIED_FOR_FIX
                and record.completion_rate < _LOW_COMPLETION_THRESHOLD):
            return EvolutionType.FIX, (
                f"Low completion rate ({record.completion_rate:.0%}) despite "
                f"high applied rate ({record.applied_rate:.0%}): "
                f"skill instructions may be incorrect or incomplete."
            )

        # Moderate effectiveness → could be better → DERIVED candidate
        if (record.effective_rate < _MODERATE_EFFECTIVE_THRESHOLD
                and record.applied_rate > _MIN_APPLIED_FOR_DERIVED):
            return EvolutionType.DERIVED, (
                f"Moderate effectiveness ({record.effective_rate:.0%}): "
                f"skill works sometimes but could be enhanced with "
                f"better error handling or alternative approaches."
            )

        return None, ""