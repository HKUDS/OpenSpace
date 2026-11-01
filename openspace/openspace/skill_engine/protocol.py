"""OpenSpace Skill Protocol surfaces for OpenSpace.

This module owns the model-visible protocol:
- ``skill_listing`` lightweight attachments list names/descriptions only.
- ``DiscoverSkills`` searches candidates and never returns SKILL.md bodies.
- ``Skill`` is the only default path that loads full skill content.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import shlex
import socket
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence, TYPE_CHECKING

from openspace.grounding.core.permissions.types import (
    AddRulesUpdate,
    DecisionReasonOther,
    DecisionReasonRule,
    PermissionAllow,
    PermissionAsk,
    PermissionDeny,
    PermissionRuleValue,
    ToolPermissionContext,
    normalize_tool_name_for_rule,
    parse_rule_value,
)
from openspace.grounding.core.tool.local_tool import LocalTool
from openspace.grounding.core.types import BackendType, ToolResult, ToolStatus
from openspace.services.conversation.attachments import create_attachment_message
from openspace.services.conversation.content_blocks import extract_text_from_content
from openspace.services.tooling.context import (
    SkillInvocationRecord,
    SkillInvocationScope,
    ToolUseContext,
)
from openspace.utils.logging import Logger

if TYPE_CHECKING:
    from openspace.skill_engine.registry import SkillMeta, SkillRegistry
    from openspace.skill_engine.store import SkillStore

logger = Logger.get_logger(__name__)


SKILL_TOOL_NAME = "Skill"
DISCOVER_SKILLS_TOOL_NAME = "DiscoverSkills"
SKILL_BUDGET_CONTEXT_PERCENT = 0.01
CHARS_PER_TOKEN = 4
DEFAULT_CHAR_BUDGET = 8_000
MAX_LISTING_DESC_CHARS = 250
FILTERED_LISTING_MAX = 30
LIST_ALL_SKILLS_MAX = 30
LARGE_SKILL_LIST_THRESHOLD = 100
DIRECT_MATCH_SCORE = 100.0
PREFIX_MATCH_SCORE = 80.0
KEYWORD_HIGH_CONFIDENCE_MIN_SCORE = 6.0
KEYWORD_HIGH_CONFIDENCE_RATIO = 1.5
HYBRID_HIGH_CONFIDENCE_MIN_SCORE = 0.25
HYBRID_HIGH_CONFIDENCE_MIN_MARGIN = 0.05
HYBRID_HIGH_CONFIDENCE_RATIO = 1.15
MIN_DESC_LENGTH = 20
PROMPT_SHELL_BLOCK_RE = re.compile(r"```!\s*\n?([\s\S]*?)\n?```")
PROMPT_SHELL_INLINE_RE = re.compile(r"(^|\s)!`([^`]+)`", re.MULTILINE)
DEFAULT_PROMPT_HOOK_TIMEOUT_SECONDS = 30.0


def _disabled_skill_ids(store: Any | None) -> set[str]:
    if store is None:
        return set()
    try:
        return {
            str(row.get("skill_id") or "")
            for row in store.get_summary(active_only=True)
            if not bool(row.get("enabled", True))
        }
    except Exception:
        logger.debug("Skill enabled-state lookup failed", exc_info=True)
        return set()


def tool_matches_name(tool: Any, name: str) -> bool:
    if getattr(tool, "name", None) == name:
        return True
    return name in set(getattr(tool, "aliases", []) or ())


def has_skill_tool(tools: Sequence[Any]) -> bool:
    return any(tool_matches_name(tool, SKILL_TOOL_NAME) for tool in tools)


def get_char_budget(
    context_window_tokens: int | None = None,
    *,
    context_percent: float = SKILL_BUDGET_CONTEXT_PERCENT,
) -> int:
    if context_window_tokens:
        return int(context_window_tokens * CHARS_PER_TOKEN * context_percent)
    return DEFAULT_CHAR_BUDGET


def _entry_description(
    skill: "SkillMeta",
    *,
    max_description_chars: int = MAX_LISTING_DESC_CHARS,
) -> str:
    description = skill.description or skill.name
    if skill.when_to_use:
        description = f"{description} - {skill.when_to_use}"
    if len(description) > max_description_chars:
        return description[: max_description_chars - 1] + "..."
    return description


def _format_entry(
    skill: "SkillMeta",
    *,
    max_desc_len: int | None = None,
    max_description_chars: int = MAX_LISTING_DESC_CHARS,
) -> str:
    description = _entry_description(
        skill,
        max_description_chars=max_description_chars,
    )
    if max_desc_len is not None and len(description) > max_desc_len:
        if max_desc_len < 4:
            return f"- {skill.name}"
        description = description[: max_desc_len - 3] + "..."
    return f"- {skill.name}: {description}"


def format_skills_within_budget(
    skills: Sequence["SkillMeta"],
    *,
    context_window_tokens: int | None = None,
    context_percent: float = SKILL_BUDGET_CONTEXT_PERCENT,
    max_description_chars: int = MAX_LISTING_DESC_CHARS,
) -> str:
    if not skills:
        return ""

    budget = get_char_budget(context_window_tokens, context_percent=context_percent)
    full_entries = [
        _format_entry(skill, max_description_chars=max_description_chars)
        for skill in skills
    ]
    full_total = sum(len(entry) for entry in full_entries) + max(0, len(full_entries) - 1)
    if full_total <= budget:
        return "\n".join(full_entries)

    bundled_indices = {
        idx
        for idx, skill in enumerate(skills)
        if skill.loaded_from == "bundled" or skill.source == "bundled"
    }
    rest = [skill for idx, skill in enumerate(skills) if idx not in bundled_indices]
    bundled_chars = sum(len(full_entries[idx]) + 1 for idx in bundled_indices)
    if not rest:
        return "\n".join(full_entries)

    remaining_budget = budget - bundled_chars
    rest_name_overhead = sum(len(skill.name) + 4 for skill in rest) + max(0, len(rest) - 1)
    max_desc_len = (remaining_budget - rest_name_overhead) // max(1, len(rest))
    if max_desc_len < MIN_DESC_LENGTH:
        return "\n".join(
            full_entries[idx] if idx in bundled_indices else f"- {skill.name}"
            for idx, skill in enumerate(skills)
        )

    return "\n".join(
        full_entries[idx]
        if idx in bundled_indices
        else _format_entry(
            skill,
            max_desc_len=max_desc_len,
            max_description_chars=max_description_chars,
        )
        for idx, skill in enumerate(skills)
    )


def filter_to_bundled_and_mcp(skills: Sequence["SkillMeta"]) -> list["SkillMeta"]:
    filtered = [
        skill
        for skill in skills
        if skill.loaded_from in {"bundled", "mcp"} or skill.source in {"bundled", "mcp"}
    ]
    if len(filtered) > FILTERED_LISTING_MAX:
        return [
            skill
            for skill in filtered
            if skill.loaded_from == "bundled" or skill.source == "bundled"
        ]
    return filtered


def _prioritize_large_skill_listing(
    skills: Sequence["SkillMeta"],
    context: ToolUseContext,
) -> list["SkillMeta"]:
    """Keep the initial listing useful when the local skill universe is huge."""

    dynamic_names = set(context.discovered_skill_names)
    activated_names = set(context.path_activated_skill_names)
    priority: list["SkillMeta"] = []
    remainder: list["SkillMeta"] = []
    for skill in skills:
        is_priority = (
            skill.loaded_from in {"bundled", "mcp"}
            or skill.source in {"bundled", "mcp"}
            or skill.name in dynamic_names
            or skill.name in activated_names
        )
        if is_priority:
            priority.append(skill)
        else:
            remainder.append(skill)

    if len(priority) >= FILTERED_LISTING_MAX:
        return priority[:FILTERED_LISTING_MAX]
    return priority + remainder[: FILTERED_LISTING_MAX - len(priority)]


def _is_forced_skill_select_query(query: str) -> bool:
    return str(query or "").strip().lower().startswith("select:")


def _context_window_tokens_for_listing(context: ToolUseContext) -> int | None:
    try:
        from openspace.services.conversation.compact import get_effective_context_window_size

        model = str(getattr(context, "model", "") or "")
        return int(get_effective_context_window_size(model)) if model else None
    except Exception:
        return None


class SkillListingService:
    """Build OpenSpace per-agent skill_listing deltas."""

    def __init__(
        self,
        registry: "SkillRegistry",
        *,
        discovery_enabled: bool = True,
        store: "SkillStore | None" = None,
        listing_budget_context_percent: float = SKILL_BUDGET_CONTEXT_PERCENT,
        listing_max_description_chars: int = MAX_LISTING_DESC_CHARS,
    ) -> None:
        self._registry = registry
        self._discovery_enabled = discovery_enabled
        self._store = store
        self._listing_budget_context_percent = max(
            0.0,
            float(listing_budget_context_percent),
        )
        self._listing_max_description_chars = max(
            MIN_DESC_LENGTH,
            int(listing_max_description_chars),
        )

    def get_listing_delta(
        self,
        context: ToolUseContext,
        tools: Sequence[Any],
    ) -> list[dict[str, Any]]:
        if not has_skill_tool(tools):
            return []

        disabled = _disabled_skill_ids(self._store)
        skills = [
            skill
            for skill in self._registry.list_skills()
            if (
                not skill.disable_model_invocation
                and _skill_visible_to_context(skill, context)
                and skill.skill_id not in disabled
            )
        ]
        total_visible_skills = len(skills)
        if self._discovery_enabled and len(skills) > LARGE_SKILL_LIST_THRESHOLD:
            skills = _prioritize_large_skill_listing(skills, context)
        if not skills:
            return []

        agent_key = context.agent_id or "primary"
        sent = context.sent_skill_names_by_agent.setdefault(agent_key, set())

        if context.skill_listing_suppressed_once:
            context.skill_listing_suppressed_once = False
            if not any(skill.name not in sent for skill in skills):
                return []

        new_skills = [skill for skill in skills if skill.name not in sent]
        if not new_skills:
            return []

        is_initial = not sent
        sent.update(skill.name for skill in new_skills)
        content = format_skills_within_budget(
            new_skills,
            context_window_tokens=_context_window_tokens_for_listing(context),
            context_percent=self._listing_budget_context_percent,
            max_description_chars=self._listing_max_description_chars,
        )
        omitted_count = max(0, total_visible_skills - len(skills))
        if omitted_count:
            content = (
                f"{content}\n\n"
                f"{omitted_count} additional skills are available. "
                "Call DiscoverSkills with a short workflow query, or use "
                "select:<skill_name> when you already know the exact skill."
            )
        if self._store is not None:
            for skill in new_skills:
                try:
                    self._store.record_skill_event_now(
                        skill.skill_id,
                        "listed",
                        source="skill_listing",
                        **_skill_event_context_kwargs(context, skill),
                        metadata={"is_initial": is_initial},
                    )
                except Exception:
                    logger.debug("Skill listed event record failed", exc_info=True)
        return [
            {
                "type": "skill_listing",
                "content": content,
                "skillCount": len(new_skills),
                "totalSkillCount": total_visible_skills,
                "omittedSkillCount": omitted_count,
                "skillNames": [skill.name for skill in new_skills],
                "skillIds": [skill.skill_id for skill in new_skills],
                "isInitial": is_initial,
            }
        ]


@dataclass(slots=True)
class SkillDiscoveryHit:
    name: str
    description: str
    skill_id: str
    source: str
    score: float


class SkillDiscoveryService:
    """OpenSpace-backed discovery with legacy-compatible output semantics."""

    def __init__(
        self,
        registry: "SkillRegistry",
        *,
        store: "SkillStore | None" = None,
        llm_client: Any | None = None,
    ) -> None:
        self._registry = registry
        self._store = store
        self._llm_client = llm_client

    @property
    def store(self) -> "SkillStore | None":
        return self._store

    def _direct_hits(
        self,
        query: str,
        *,
        context: ToolUseContext | None,
        max_results: int,
    ) -> list[SkillDiscoveryHit]:
        normalized = str(query or "").strip()
        if not normalized:
            return []

        selection_query = normalized
        forced_select = False
        if normalized.lower().startswith("select:"):
            forced_select = True
            selection_query = normalized.split(":", 1)[1].strip()
        selection_query = _normalize_skill_name(selection_query)
        if not selection_query:
            return []

        candidates = self._candidate_skills(context, include_seen=True)
        exact = [
            skill for skill in candidates
            if _skill_matches_exact(skill, selection_query)
        ]
        if exact:
            return [
                _hit_for_skill(skill, DIRECT_MATCH_SCORE)
                for skill in exact[: max(1, max_results)]
            ]

        prefix_matches = [
            skill for skill in candidates
            if _skill_matches_prefix(skill, selection_query)
        ]
        if len(prefix_matches) == 1:
            return [_hit_for_skill(prefix_matches[0], PREFIX_MATCH_SCORE)]

        # `select:` is an exact instruction. If it did not resolve, do not
        # reinterpret it as a broad semantic query.
        if forced_select:
            return []

        return []

    @staticmethod
    def _high_confidence_keyword_hits(
        hits: Sequence[SkillDiscoveryHit],
    ) -> bool:
        if not hits:
            return False
        first = hits[0].score
        if first < KEYWORD_HIGH_CONFIDENCE_MIN_SCORE:
            return False
        if len(hits) == 1:
            return True
        second = max(hits[1].score, 0.0001)
        return first / second >= KEYWORD_HIGH_CONFIDENCE_RATIO

    @staticmethod
    def _high_confidence_hybrid_hits(
        hits: Sequence[SkillDiscoveryHit],
    ) -> bool:
        if not hits:
            return False
        first = hits[0].score
        if first < HYBRID_HIGH_CONFIDENCE_MIN_SCORE:
            return False
        if len(hits) == 1:
            return True
        second = hits[1].score
        if second <= 0:
            return True
        return (
            first - second >= HYBRID_HIGH_CONFIDENCE_MIN_MARGIN
            and first / second >= HYBRID_HIGH_CONFIDENCE_RATIO
        )

    def _hybrid_search(
        self,
        query: str,
        candidates: Sequence["SkillMeta"],
        *,
        max_results: int,
        fallback_hits: Sequence[SkillDiscoveryHit] = (),
    ) -> list[SkillDiscoveryHit]:
        try:
            from openspace.skill_engine.skill_ranker import SkillCandidate

            rank_candidates: list[SkillCandidate] = []
            for skill in candidates:
                body = self._registry.load_skill_content(skill.skill_id) or ""
                rank_candidates.append(
                    SkillCandidate(
                        skill_id=skill.skill_id,
                        name=skill.name,
                        description=skill.description,
                        body=body,
                    )
                )

            ranked = self._registry.ranker.hybrid_rank(
                query,
                rank_candidates,
                top_k=max(1, max_results),
            )
            by_id = {skill.skill_id: skill for skill in candidates}
            hits: list[SkillDiscoveryHit] = []
            for item in ranked:
                skill = by_id.get(item.skill_id)
                if skill is None:
                    continue
                score = item.score or item.vector_score or item.bm25_score
                hits.append(_hit_for_skill(skill, float(score)))
            if hits and all(hit.score <= 0 for hit in hits):
                return list(fallback_hits)
            return hits
        except Exception:
            logger.debug("Skill discovery hybrid ranking failed", exc_info=True)
            return list(fallback_hits)

    def search(
        self,
        query: str,
        *,
        context: ToolUseContext | None = None,
        max_results: int = 5,
    ) -> list[SkillDiscoveryHit]:
        normalized = str(query or "").strip()
        if not normalized:
            return []

        direct_hits = self._direct_hits(
            normalized,
            context=context,
            max_results=max_results,
        )
        if direct_hits:
            return direct_hits
        if _is_forced_skill_select_query(normalized):
            return []

        candidates = self._candidate_skills(context)
        if not candidates:
            return []

        keyword_hits = self._token_overlap_search(
            normalized,
            context=context,
            max_results=max_results,
            candidates=candidates,
        )
        if self._high_confidence_keyword_hits(keyword_hits):
            return keyword_hits

        return self._hybrid_search(
            normalized,
            candidates,
            max_results=max_results,
            fallback_hits=keyword_hits,
        )

    async def asearch(
        self,
        query: str,
        *,
        context: ToolUseContext | None = None,
        max_results: int = 5,
        model: str | None = None,
    ) -> list[SkillDiscoveryHit]:
        """Run discovery through OpenSpace's legacy selector when an LLM exists."""

        normalized = str(query or "").strip()
        if not normalized:
            return []

        direct_hits = self._direct_hits(
            normalized,
            context=context,
            max_results=max_results,
        )
        if direct_hits:
            return direct_hits
        if _is_forced_skill_select_query(normalized):
            return []

        candidates = self._candidate_skills(context)
        if not candidates:
            return []

        keyword_hits = self._token_overlap_search(
            normalized,
            context=context,
            max_results=max_results,
            candidates=candidates,
        )
        if self._high_confidence_keyword_hits(keyword_hits):
            return keyword_hits

        hybrid_hits = self._hybrid_search(
            normalized,
            candidates,
            max_results=max_results,
            fallback_hits=keyword_hits,
        )
        if self._high_confidence_hybrid_hits(hybrid_hits):
            return hybrid_hits

        if self._llm_client is None:
            return hybrid_hits

        try:
            selected, record = await self._registry.select_skills_with_llm(
                normalized,
                llm_client=self._llm_client,
                max_skills=max(1, max_results),
                model=model,
                skill_quality=self._quality_by_skill_id(),
                candidate_skills=candidates,
            )
        except Exception:
            logger.debug("Skill discovery LLM selector failed", exc_info=True)
            return hybrid_hits

        if record and record.get("method") == "llm_failed":
            return hybrid_hits

        hits: list[SkillDiscoveryHit] = []
        for index, skill in enumerate(selected[: max(1, max_results)]):
            hits.append(
                SkillDiscoveryHit(
                    name=skill.name,
                    description=skill.description,
                    skill_id=skill.skill_id,
                    source=skill.source,
                    score=float(max_results - index),
                )
            )
        return hits or hybrid_hits

    def _candidate_skills(
        self,
        context: ToolUseContext | None,
        *,
        include_seen: bool = False,
    ) -> list["SkillMeta"]:
        visible = set()
        invoked = set()
        if context is not None:
            agent_key = context.agent_id or "primary"
            visible.update(context.sent_skill_names_by_agent.get(agent_key, set()))
            visible.update(context.discovered_skill_names)
            for records in context.invoked_skills_by_agent.values():
                invoked.update(record.name for record in records)

        candidates: list["SkillMeta"] = []
        disabled = _disabled_skill_ids(self._store)
        for skill in self._registry.list_skills():
            if skill.disable_model_invocation:
                continue
            if skill.skill_id in disabled:
                continue
            if not _skill_visible_to_context(skill, context):
                continue
            if not include_seen and (skill.name in visible or skill.name in invoked):
                continue
            candidates.append(skill)
        return candidates

    def _token_overlap_search(
        self,
        query: str,
        *,
        context: ToolUseContext | None,
        max_results: int,
        candidates: Sequence["SkillMeta"] | None = None,
    ) -> list[SkillDiscoveryHit]:
        quality = self._quality_by_skill_id()
        tokens = _tokenize(query)
        hits: list[SkillDiscoveryHit] = []
        search_candidates = (
            list(candidates)
            if candidates is not None
            else self._candidate_skills(context)
        )
        metadata_only = bool(
            getattr(context, "skill_metadata_only_discovery", False)
            if context is not None
            else False
        )
        for skill in search_candidates:
            body = (
                ""
                if metadata_only
                else self._registry.load_skill_content(skill.skill_id) or ""
            )
            score = _score_skill(tokens, skill.name, skill.description, skill.when_to_use, body)
            if score <= 0:
                continue
            q = quality.get(skill.skill_id)
            if q:
                score += min(float(q.get("total_completions", 0)), 5.0) * 0.05
                score -= min(float(q.get("total_fallbacks", 0)), 5.0) * 0.05
            hits.append(
                SkillDiscoveryHit(
                    name=skill.name,
                    description=skill.description,
                    skill_id=skill.skill_id,
                    source=skill.source,
                    score=score,
                )
            )

        hits.sort(key=lambda item: item.score, reverse=True)
        return hits[: max(1, max_results)]

    def build_attachment(
        self,
        hits: Sequence[SkillDiscoveryHit],
        *,
        signal: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "type": "skill_discovery",
            "skills": [
                {
                    "name": hit.name,
                    "description": hit.description,
                    "skill_id": hit.skill_id,
                    "source": hit.source,
                }
                for hit in hits
            ],
            "signal": dict(signal or {}),
            "source": "openspace",
        }

    def _quality_by_skill_id(self) -> dict[str, dict[str, Any]]:
        if self._store is None:
            return {}
        try:
            rows = self._store.get_summary(active_only=True)
            return {
                row["skill_id"]: {
                    "total_selections": row.get("total_selections", 0),
                    "total_applied": row.get("total_applied", 0),
                    "total_completions": row.get("total_completions", 0),
                    "total_fallbacks": row.get("total_fallbacks", 0),
                    "enabled": row.get("enabled", True),
                    "trust_state": row.get("trust_state", "trusted"),
                    "trust_successes": row.get("trust_successes", 0),
                    "trust_failures": row.get("trust_failures", 0),
                }
                for row in rows
            }
        except Exception:
            return {}


class SkillTool(LocalTool):
    """Load a single SKILL.md into the conversation."""

    _name = SKILL_TOOL_NAME
    _description = "Execute a skill within the main conversation"
    backend_type = BackendType.META
    _is_read_only = True
    _is_concurrency_safe = False
    always_load = True
    max_result_size_chars = 100_000
    search_hint = "invoke a slash-command skill"
    parameter_descriptions = {
        "skill": 'The skill name, such as "commit", "review-pr", or "pdf".',
        "args": "Optional arguments for the skill.",
    }

    def __init__(
        self,
        registry: "SkillRegistry",
        *,
        skill_store: "SkillStore | None" = None,
    ) -> None:
        self._registry = registry
        self._skill_store = skill_store
        self._current_context: ToolUseContext | None = None
        self._current_tool_use_id: str = ""
        super().__init__(verbose=False, handle_errors=False)

    def set_context(self, context: ToolUseContext) -> None:
        self._current_context = context

    def set_current_tool_use(self, *, tool_use_id: str, tool_name: str = "") -> None:
        self._current_tool_use_id = str(tool_use_id or "")

    def get_prompt(self, context: Any = None) -> str:
        return (
            "Execute a skill within the main conversation.\n\n"
            "When users ask you to perform tasks, check whether any available "
            "skill matches. Skills provide specialized capabilities and domain "
            "knowledge.\n\n"
            "When a user references a slash command like `/commit`, call this "
            "tool with `skill: \"commit\"`.\n\n"
            "Important:\n"
            "- Available skills are listed in system-reminder messages.\n"
            "- When a skill matches the user's request, call this Skill tool "
            "before responding about the task.\n"
            "- Never mention a skill without actually calling this tool.\n"
            "- If a <command-name> tag for the skill is already visible in the "
            "current turn, follow those instructions instead of calling again."
        )

    async def validate_input(
        self,
        input: dict[str, Any],
        context: Any = None,
    ) -> str | None:
        raw_skill = str(input.get("skill") or "")
        skill_name = _normalize_skill_name(raw_skill)
        if not skill_name:
            return f"Invalid skill format: {raw_skill}"
        skill = self._registry.resolve_skill_for_model(skill_name)
        if skill is None:
            return f"Unknown skill: {skill_name}"
        if skill.skill_id in _disabled_skill_ids(self._skill_store):
            return f"Skill {skill_name} is disabled"
        if not _skill_visible_to_context(skill, self._current_context or context):
            return f"Skill {skill_name} is not available until a matching path is touched"
        if skill.disable_model_invocation:
            return (
                f"Skill {skill_name} cannot be used with {SKILL_TOOL_NAME} "
                "tool due to disable-model-invocation"
            )
        return None

    async def check_permissions(
        self,
        input: dict[str, Any],
        context: Any = None,
    ):
        raw_skill = str(input.get("skill") or "")
        skill_name = _normalize_skill_name(raw_skill)
        meta = self._registry.resolve_skill_for_model(skill_name)
        event_context = context or self._current_context
        if meta is None:
            return PermissionDeny(
                message=f"Unknown skill: {skill_name}",
                decision_reason=DecisionReasonOther(reason="unknown skill"),
            )
        if meta.skill_id in _disabled_skill_ids(self._skill_store):
            return PermissionDeny(
                message=f"Skill {skill_name} is disabled",
                decision_reason=DecisionReasonOther(reason="skill disabled"),
            )
        if not _skill_visible_to_context(meta, event_context):
            await _record_skill_event_for_context(
                self._skill_store,
                meta,
                "permission_denied",
                source="skill_tool_permission",
                context=event_context,
                metadata={"reason": "conditional skill not active"},
            )
            return PermissionDeny(
                message=f"Skill {skill_name} is not available until a matching path is touched",
                decision_reason=DecisionReasonOther(reason="conditional skill not active"),
            )
        if meta.disable_model_invocation:
            await _record_skill_event_for_context(
                self._skill_store,
                meta,
                "permission_denied",
                source="skill_tool_permission",
                context=event_context,
                metadata={"reason": "disable-model-invocation"},
            )
            return PermissionDeny(
                message=(
                    f"Skill {skill_name} cannot be used with {SKILL_TOOL_NAME} "
                    "tool due to disable-model-invocation"
                ),
                decision_reason=DecisionReasonOther(reason="disable-model-invocation"),
            )

        perm_ctx = getattr(event_context, "permission_context", None)
        if perm_ctx is not None:
            deny_rule = _matching_skill_rule(perm_ctx, meta.name, "deny")
            if deny_rule is not None:
                await _record_skill_event_for_context(
                    self._skill_store,
                    meta,
                    "permission_denied",
                    source="skill_tool_permission",
                    context=event_context,
                    metadata={
                        "reason": "permission rule deny",
                        "rule": str(deny_rule),
                    },
                )
                return PermissionDeny(
                    message="Skill execution blocked by permission rules",
                    decision_reason=DecisionReasonRule(rule=deny_rule),
                )
            allow_rule = _matching_skill_rule(perm_ctx, meta.name, "allow")
            if allow_rule is not None:
                await _record_skill_event_for_context(
                    self._skill_store,
                    meta,
                    "permission_granted",
                    source="skill_tool_permission",
                    context=event_context,
                    metadata={
                        "reason": "permission rule allow",
                        "rule": str(allow_rule),
                    },
                )
                return PermissionAllow(
                    updated_input=input,
                    decision_reason=DecisionReasonRule(rule=allow_rule),
                )
            ask_rule = _matching_skill_rule(perm_ctx, meta.name, "ask")
            if ask_rule is not None:
                await _record_skill_event_for_context(
                    self._skill_store,
                    meta,
                    "permission_requested",
                    source="skill_tool_permission",
                    context=event_context,
                    metadata={
                        "reason": "permission rule ask",
                        "rule": str(ask_rule),
                    },
                )
                return PermissionAsk(
                    message=_skill_permission_ask_message(meta),
                    updated_input=input,
                    suggestions=(
                        AddRulesUpdate(
                            destination="localSettings",
                            rules=(PermissionRuleValue(SKILL_TOOL_NAME, meta.name),),
                            behavior="allow",
                        ),
                        AddRulesUpdate(
                            destination="localSettings",
                            rules=(PermissionRuleValue(SKILL_TOOL_NAME, f"{meta.name}:*"),),
                            behavior="allow",
                        ),
                    ),
                    decision_reason=DecisionReasonRule(rule=ask_rule),
                )

        if _skill_has_only_safe_properties(meta):
            await _record_skill_event_for_context(
                self._skill_store,
                meta,
                "permission_granted",
                source="skill_tool_permission",
                context=event_context,
                metadata={"reason": "safe skill properties"},
            )
            return PermissionAllow(updated_input=input)

        return PermissionAsk(
            message=_skill_permission_ask_message(meta),
            updated_input=input,
            suggestions=(
                AddRulesUpdate(
                    destination="localSettings",
                    rules=(PermissionRuleValue(SKILL_TOOL_NAME, meta.name),),
                    behavior="allow",
                ),
                AddRulesUpdate(
                    destination="localSettings",
                    rules=(PermissionRuleValue(SKILL_TOOL_NAME, f"{meta.name}:*"),),
                    behavior="allow",
                ),
            ),
            decision_reason=DecisionReasonOther(reason="skill has unsafe runtime fields"),
        )

    async def _arun(self, skill: str, args: str | None = None) -> ToolResult:
        skill_name = _normalize_skill_name(skill)
        meta = self._registry.resolve_skill_for_model(skill_name)
        context = self._current_context
        if meta is None:
            return ToolResult(
                status=ToolStatus.ERROR,
                content=f"Unknown skill: {skill_name}",
                error=f"Unknown skill: {skill_name}",
            )
        if not _skill_visible_to_context(meta, context):
            await _record_skill_event_for_context(
                self._skill_store,
                meta,
                "load_failed",
                source="skill_tool",
                context=context,
                metadata={"reason": "conditional skill not active"},
            )
            return ToolResult(
                status=ToolStatus.ERROR,
                content=f"Skill {skill_name} is not available until a matching path is touched.",
                error="conditional skill not active",
            )
        if meta.disable_model_invocation:
            await _record_skill_event_for_context(
                self._skill_store,
                meta,
                "load_failed",
                source="skill_tool",
                context=context,
                metadata={"reason": "disable-model-invocation"},
            )
            return ToolResult(
                status=ToolStatus.ERROR,
                content=(
                    f"Skill {skill_name} cannot be used with {SKILL_TOOL_NAME} "
                    "tool due to disable-model-invocation"
                ),
                error="disable-model-invocation",
            )

        content = self._registry.build_skill_invocation_context(meta, args=args or "")
        if not content:
            await _record_skill_event_for_context(
                self._skill_store,
                meta,
                "load_failed",
                source="skill_tool",
                context=context,
                metadata={"reason": "skill content unavailable"},
            )
            return ToolResult(
                status=ToolStatus.ERROR,
                content=f"Skill {meta.name} could not be loaded.",
                error="skill content unavailable",
            )
        try:
            content = await _execute_skill_prompt_shell_expansions(
                content,
                meta=meta,
                context=context,
            )
        except Exception as exc:
            await _record_skill_event_for_context(
                self._skill_store,
                meta,
                "load_failed",
                source="skill_tool",
                context=context,
                metadata={
                    "reason": "skill shell expansion error",
                    "error": str(exc),
                },
            )
            return ToolResult(
                status=ToolStatus.ERROR,
                content=f"Skill {meta.name} could not be loaded: {exc}",
                error=str(exc),
                metadata={
                    "tool": self.name,
                    "skill_id": meta.skill_id,
                    "skill_name": meta.name,
                    "error_type": "skill_shell_expansion_error",
                },
            )

        invocation_args = args or ""
        skill_scope_id = f"skill_{uuid.uuid4().hex[:12]}"
        invocation_tool_use_id = self._current_tool_use_id
        invocation_tool_event_ref_id = _tool_event_ref_id_for_context(
            context,
            invocation_tool_use_id,
        )
        skill_event_ref_id: str | None = None
        if context is not None:
            context.record_invoked_skill(
                SkillInvocationRecord(
                    skill_id=meta.skill_id,
                    name=meta.name,
                    path=str(meta.path),
                    content=content,
                    args=invocation_args,
                    allowed_tools=list(meta.allowed_tools),
                    model=meta.model,
                    effort=meta.effort,
                    execution_context=meta.execution_context or "inline",
                )
            )
            context.mark_skills_discovered([meta.name])
        if self._skill_store is not None:
            try:
                await self._skill_store.sync_from_registry([meta])
                skill_event_metadata = {
                    "skill_name": meta.name,
                    "execution_context": meta.execution_context or "inline",
                    "skill_scope_id": skill_scope_id,
                    "skill_invocation_scope_id": skill_scope_id,
                    "allowed_tools": list(meta.allowed_tools),
                }
                if context is not None and getattr(context, "session_id", None):
                    skill_event_metadata["session_id"] = str(context.session_id)
                if invocation_tool_use_id:
                    skill_event_metadata["invocation_tool_use_id"] = invocation_tool_use_id
                if invocation_tool_event_ref_id:
                    skill_event_metadata["raw_backrefs"] = [invocation_tool_event_ref_id]
                    skill_event_metadata["invocation_tool_event_ref_id"] = (
                        invocation_tool_event_ref_id
                    )
                event_row = await self._skill_store.record_skill_event(
                    meta.skill_id,
                    "invoked",
                    source="skill_tool",
                    **_skill_event_context_kwargs(context, meta),
                    metadata=skill_event_metadata,
                )
                if isinstance(event_row, Mapping) and event_row.get("row_id"):
                    skill_event_ref_id = (
                        f"skill_event:{meta.skill_id}:invoked:{event_row['row_id']}"
                    )
            except Exception as exc:
                logger.debug("Skill invocation quality record failed: %s", exc)

        modifier = _build_skill_context_modifier(
            meta,
            invocation_args,
            scope_id=skill_scope_id,
            invocation_tool_use_id=invocation_tool_use_id or None,
            skill_event_ref_id=skill_event_ref_id,
        )

        if meta.execution_context == "fork":
            return await _execute_forked_skill(
                meta=meta,
                content=content,
                args=invocation_args,
                context=context,
                context_modifier=modifier,
            )

        attachment = create_attachment_message(
            {
                "type": "invoked_skill_content",
                "name": meta.name,
                "skill_id": meta.skill_id,
                "path": str(meta.path),
                "content": content,
                "agent_id": str(getattr(context, "agent_id", "") or ""),
                "allowed_tools": list(meta.allowed_tools),
                "skill_scope_id": skill_scope_id,
                "skill_event_ref_id": skill_event_ref_id,
                "model": meta.model,
                "effort": meta.effort,
                "execution_context": meta.execution_context or "inline",
            }
        )
        result = ToolResult(
            status=ToolStatus.SUCCESS,
            content=f"Launching skill: {meta.name}",
            metadata={
                "tool": self.name,
                "skill_id": meta.skill_id,
                "skill_name": meta.name,
                "skill_scope_id": skill_scope_id,
                "skill_event_ref_id": skill_event_ref_id,
                "allowed_tools": list(meta.allowed_tools),
                "model": meta.model,
                "effort": meta.effort,
                "execution_context": meta.execution_context or "inline",
            },
        )
        setattr(result, "additional_messages", [attachment])
        setattr(result, "context_modifier", modifier)
        return result


class DiscoverSkillsTool(LocalTool):
    """Search for skills without loading full skill content."""

    _name = DISCOVER_SKILLS_TOOL_NAME
    _description = "Discover relevant skills by name and description"
    backend_type = BackendType.META
    _is_read_only = True
    _is_concurrency_safe = True
    always_load = True
    search_hint = "find relevant skills"
    parameter_descriptions = {
        "query": (
            "A specific description of the workflow or next action. Use "
            "select:<skill_name> to choose an exact skill already shown in "
            "a listing."
        ),
        "max_results": "Maximum number of skill candidates to return.",
    }

    def __init__(self, discovery_service: SkillDiscoveryService) -> None:
        self._discovery_service = discovery_service
        self._current_context: ToolUseContext | None = None
        super().__init__(verbose=False, handle_errors=False)

    def set_context(self, context: ToolUseContext) -> None:
        self._current_context = context

    def get_prompt(self, context: Any = None) -> str:
        return (
            "Discover skills relevant to a planned action. This returns only "
            "candidate skill names and descriptions; use the Skill tool to load "
            "the full instructions before applying a skill. If you already know "
            "the exact skill name, query select:<skill_name>."
        )

    async def validate_input(
        self,
        input: dict[str, Any],
        context: Any = None,
    ) -> str | None:
        if not str(input.get("query") or "").strip():
            return "DiscoverSkills requires a non-empty query"
        return None

    async def _arun(self, query: str, max_results: int = 5) -> ToolResult:
        try:
            limit = max(1, min(int(max_results), 20))
        except (TypeError, ValueError):
            limit = 5
        hits = await self._discovery_service.asearch(
            query,
            context=self._current_context,
            max_results=limit,
        )
        if self._current_context is not None:
            self._current_context.mark_skills_discovered(hit.name for hit in hits)
        await _record_skill_selection_for_hits(
            self._discovery_service.store,
            self._current_context,
            hits,
            query=query,
            source=self.name,
            method="discover_skills_tool",
        )
        if self._discovery_service.store is not None:
            for hit in hits:
                try:
                    await self._discovery_service.store.record_skill_event(
                        hit.skill_id,
                        "discovered",
                        source=self.name,
                        **_skill_event_context_kwargs(self._current_context, hit),
                        query=query,
                        metadata={"query": query, "score": hit.score},
                    )
                except Exception:
                    logger.debug("Skill discovery event record failed", exc_info=True)
        if not hits:
            return ToolResult(
                status=ToolStatus.SUCCESS,
                content="No relevant skills found for this query.",
                metadata={"tool": self.name, "skills": []},
            )

        attachment = self._discovery_service.build_attachment(
            hits,
            signal={"query": query},
        )
        lines = [
            "Skills relevant to your task:",
            *[f"- {hit.name}: {hit.description}" for hit in hits],
            "",
            "Use the Skill tool with one of these names to load the full instructions.",
        ]
        result = ToolResult(
            status=ToolStatus.SUCCESS,
            content="\n".join(lines),
            metadata={
                "tool": self.name,
                "skills": [
                    {"name": hit.name, "skill_id": hit.skill_id}
                    for hit in hits
                ],
            },
        )
        setattr(result, "additional_messages", [create_attachment_message(attachment)])
        return result


def build_skill_listing_messages(
    context: ToolUseContext,
    *,
    registry: "SkillRegistry",
    tools: Sequence[Any],
    discovery_enabled: bool = True,
    store: "SkillStore | None" = None,
    listing_budget_context_percent: float = SKILL_BUDGET_CONTEXT_PERCENT,
    listing_max_description_chars: int = MAX_LISTING_DESC_CHARS,
) -> list[dict[str, Any]]:
    service = SkillListingService(
        registry,
        discovery_enabled=discovery_enabled,
        store=store,
        listing_budget_context_percent=listing_budget_context_percent,
        listing_max_description_chars=listing_max_description_chars,
    )
    return [
        create_attachment_message(attachment)
        for attachment in service.get_listing_delta(context, tools)
    ]


def build_skill_discovery_messages(
    context: ToolUseContext,
    *,
    registry: "SkillRegistry",
    query: str,
    max_results: int = 5,
    store: "SkillStore | None" = None,
    source: str = "skill_discovery_prefetch",
    tools: Sequence[Any] | None = None,
) -> list[dict[str, Any]]:
    """Build OpenSpace automatic ``skill_discovery`` attachment messages.

    This mirrors OpenSpace's feature-gated discovery surfacing: the backend may be
    OpenSpace's local ranker, but the model only receives candidates
    (name/description/source/id), never full ``SKILL.md`` bodies.
    """

    if getattr(context, "skills_disabled", False):
        return []
    active_tools = list(tools if tools is not None else getattr(context, "tools", []) or [])
    if not has_skill_tool(active_tools):
        return []

    service = SkillDiscoveryService(registry, store=store)
    hits = service.search(query, context=context, max_results=max_results)
    return _build_skill_discovery_messages_from_hits(
        context,
        service=service,
        hits=hits,
        query=query,
        store=store,
        source=source,
    )


async def build_skill_discovery_messages_async(
    context: ToolUseContext,
    *,
    registry: "SkillRegistry",
    query: str,
    max_results: int = 5,
    store: "SkillStore | None" = None,
    source: str = "skill_discovery_prefetch",
    tools: Sequence[Any] | None = None,
    llm_client: Any | None = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """Build automatic discovery messages using the legacy LLM selector."""

    if getattr(context, "skills_disabled", False):
        return []
    active_tools = list(tools if tools is not None else getattr(context, "tools", []) or [])
    if not has_skill_tool(active_tools):
        return []

    service = SkillDiscoveryService(registry, store=store, llm_client=llm_client)
    hits = await service.asearch(
        query,
        context=context,
        max_results=max_results,
        model=model,
    )
    return _build_skill_discovery_messages_from_hits(
        context,
        service=service,
        hits=hits,
        query=query,
        store=store,
        source=source,
    )


def _build_skill_discovery_messages_from_hits(
    context: ToolUseContext,
    *,
    service: SkillDiscoveryService,
    hits: Sequence[SkillDiscoveryHit],
    query: str,
    store: "SkillStore | None",
    source: str,
) -> list[dict[str, Any]]:
    if not hits:
        return []

    context.mark_skills_discovered(hit.name for hit in hits)
    _record_skill_selection_for_hits_now(
        store,
        context,
        hits,
        query=query,
        source=source,
        method="automatic_discovery",
    )
    if store is not None:
        for hit in hits:
            try:
                store.record_skill_event_now(
                    hit.skill_id,
                    "discovered",
                    source=source,
                    **_skill_event_context_kwargs(context, hit),
                    query=query,
                    metadata={"score": hit.score, "skill_name": hit.name},
                )
            except Exception:
                logger.debug("Automatic skill discovery event record failed", exc_info=True)

    return [
        create_attachment_message(
            service.build_attachment(
                hits,
                signal={"query": query, "source": source},
            )
        )
    ]


async def consume_dynamic_skill_triggers(context: ToolUseContext) -> list[dict[str, Any]]:
    """Consume touched paths and build OpenSpace ``dynamic_skill`` attachments."""

    registry = getattr(context, "skill_registry", None)
    triggers = getattr(context, "dynamic_skill_path_triggers", None)
    if registry is None or not isinstance(triggers, set) or not triggers:
        return []
    if getattr(context, "skills_disabled", False) or not has_skill_tool(
        getattr(context, "tools", []) or []
    ):
        triggers.clear()
        return []

    paths = sorted(str(path) for path in triggers if path)
    triggers.clear()
    grouped: dict[str, set[str]] = {}
    discovered_metas: dict[str, "SkillMeta"] = {}
    for path in paths:
        try:
            nested = registry.discover_skill_dirs_for_path(path, cwd=context.cwd)
            for skill_dir, skills in nested.items():
                names = grouped.setdefault(skill_dir, set())
                for skill in skills:
                    discovered_metas[skill.skill_id] = skill
                    names.add(skill.name)
                    context.mark_skills_discovered([skill.name])
                    context.mark_path_activated_skills([skill.name])

            conditional = registry.activate_conditional_skills_for_path(
                path,
                cwd=context.cwd,
            )
            for skill in conditional:
                discovered_metas[skill.skill_id] = skill
                key = str(skill.path.parent)
                grouped.setdefault(key, set()).add(skill.name)
                context.mark_skills_discovered([skill.name])
                context.mark_path_activated_skills([skill.name])
        except Exception:
            logger.debug("Dynamic skill trigger failed for %s", path, exc_info=True)

    store = getattr(context, "skill_store", None)
    if store is not None and discovered_metas:
        try:
            await store.sync_from_registry(list(discovered_metas.values()))
        except Exception:
            logger.debug("Dynamic skill store sync failed", exc_info=True)
        for meta in discovered_metas.values():
            try:
                await store.record_skill_event(
                    meta.skill_id,
                    "discovered",
                    source="dynamic_skill",
                    **_skill_event_context_kwargs(context, meta),
                    metadata={"trigger": "path"},
                )
            except Exception:
                logger.debug("Dynamic skill discovery event record failed", exc_info=True)

    sent = getattr(context, "sent_dynamic_skill_keys", set())
    attachments: list[dict[str, Any]] = []
    for skill_dir, names in sorted(grouped.items()):
        skill_names = sorted(names)
        if not skill_names:
            continue
        dedupe_key = f"{skill_dir}:{','.join(skill_names)}"
        if dedupe_key in sent:
            continue
        sent.add(dedupe_key)
        attachments.append(
            create_attachment_message(
                {
                    "type": "dynamic_skill",
                    "skillDir": skill_dir,
                    "displayPath": skill_dir,
                    "skillNames": skill_names,
                }
            )
        )
    context.sent_dynamic_skill_keys = sent
    return attachments


def restore_skill_state_from_messages(
    messages: Sequence[Mapping[str, Any]],
    context: ToolUseContext,
) -> dict[str, int]:
    """Restore OpenSpace skill protocol runtime state from transcript messages.

    OpenSpace's conversation recovery scans ``skill_listing`` and ``invoked_skills``
    attachments before resume.  OpenSpace keeps the same state on
    ``ToolUseContext`` so listing/discovery/invoked-skill retention does not
    duplicate work after compaction or external conversation history replay.
    """

    if getattr(context, "skills_disabled", False):
        return {"listed": 0, "discovered": 0, "invoked": 0}

    agent_key = context.agent_id or "primary"
    sent = context.sent_skill_names_by_agent.setdefault(agent_key, set())
    listed = 0
    discovered = 0
    invoked = 0

    for message in messages:
        attachment = _message_attachment(message)
        if not attachment:
            invoked += _restore_invoked_skill_from_tool_metadata(message, context)
            continue
        attachment_type = attachment.get("type")

        if attachment_type == "skill_listing":
            names = _string_items(attachment.get("skillNames"))
            if not names:
                names = _skill_names_from_listing_content(attachment.get("content"))
            if names:
                before = len(sent)
                sent.update(names)
                listed += len(sent) - before
            continue

        if attachment_type == "skill_discovery":
            names = []
            for item in attachment.get("skills") or []:
                if isinstance(item, Mapping):
                    name = str(item.get("name") or "").strip()
                    if name:
                        names.append(name)
            if names:
                before = len(context.discovered_skill_names)
                context.mark_skills_discovered(names)
                discovered += len(context.discovered_skill_names) - before
            continue

        if attachment_type == "dynamic_skill":
            names = _string_items(attachment.get("skillNames"))
            if names:
                before = len(context.discovered_skill_names)
                context.mark_skills_discovered(names)
                context.mark_path_activated_skills(names)
                discovered += len(context.discovered_skill_names) - before
                skill_dir = str(attachment.get("skillDir") or attachment.get("displayPath") or "")
                if skill_dir:
                    context.sent_dynamic_skill_keys.add(
                        f"{skill_dir}:{','.join(sorted(names))}"
                    )
            continue

        if attachment_type == "skill_state":
            listed += _restore_skill_state_attachment(attachment, context)
            discovered += _restore_skill_state_discovered_count(attachment, context)
            continue

        if attachment_type == "invoked_skill_content":
            record = _record_from_invoked_attachment(attachment)
            if record is not None:
                record_agent_id = str(attachment.get("agent_id") or "") or None
                context.record_invoked_skill(
                    record,
                    agent_id=record_agent_id,
                )
                _restore_invoked_skill_scope(record, context, agent_id=record_agent_id)
                context.mark_skills_discovered([record.name])
                invoked += 1
            continue

        if attachment_type == "invoked_skills":
            for item in attachment.get("skills") or []:
                if not isinstance(item, Mapping):
                    continue
                record = _record_from_invoked_attachment(item)
                if record is None:
                    continue
                record_agent_id = str(item.get("agent_id") or "") or None
                context.record_invoked_skill(
                    record,
                    agent_id=record_agent_id,
                )
                _restore_invoked_skill_scope(record, context, agent_id=record_agent_id)
                context.mark_skills_discovered([record.name])
                invoked += 1

        invoked += _restore_invoked_skill_from_tool_metadata(message, context)

    return {
        "listed": listed,
        "discovered": discovered,
        "invoked": invoked,
    }


def _restore_skill_state_attachment(
    attachment: Mapping[str, Any],
    context: ToolUseContext,
) -> int:
    listed = 0
    raw_sent = attachment.get("sentSkillNamesByAgent")
    if isinstance(raw_sent, Mapping):
        for agent, names in raw_sent.items():
            restored_names = _string_items(names)
            if not restored_names:
                continue
            sent = context.sent_skill_names_by_agent.setdefault(
                str(agent or "primary"),
                set(),
            )
            before = len(sent)
            sent.update(restored_names)
            listed += len(sent) - before

    for key in _string_items(attachment.get("sentDynamicSkillKeys")):
        context.sent_dynamic_skill_keys.add(key)
    context.mark_path_activated_skills(
        _string_items(attachment.get("pathActivatedSkillNames"))
    )
    return listed


def _restore_skill_state_discovered_count(
    attachment: Mapping[str, Any],
    context: ToolUseContext,
) -> int:
    before = len(context.discovered_skill_names)
    context.mark_skills_discovered(_string_items(attachment.get("discoveredSkillNames")))
    return len(context.discovered_skill_names) - before


def _normalize_skill_name(value: str) -> str:
    trimmed = str(value or "").strip()
    return trimmed[1:] if trimmed.startswith("/") else trimmed


def _canonical_skill_key(value: Any) -> str:
    return re.sub(r"[\s_/]+", "-", str(value or "").strip().lower())


def _skill_lookup_values(skill: "SkillMeta") -> set[str]:
    values = {
        str(getattr(skill, "name", "") or ""),
        str(getattr(skill, "skill_id", "") or ""),
        str(getattr(skill, "display_name", "") or ""),
    }
    return {value for value in values if value}


def _skill_matches_exact(skill: "SkillMeta", query: str) -> bool:
    normalized = _canonical_skill_key(query)
    if not normalized:
        return False
    return any(_canonical_skill_key(value) == normalized for value in _skill_lookup_values(skill))


def _skill_matches_prefix(skill: "SkillMeta", query: str) -> bool:
    normalized = _canonical_skill_key(query)
    if not normalized:
        return False
    return any(
        _canonical_skill_key(value).startswith(normalized)
        for value in _skill_lookup_values(skill)
    )


def _hit_for_skill(skill: "SkillMeta", score: float) -> SkillDiscoveryHit:
    return SkillDiscoveryHit(
        name=skill.name,
        description=skill.description,
        skill_id=skill.skill_id,
        source=skill.source,
        score=float(score),
    )


def _skill_selection_context_kwargs(context: Any | None) -> dict[str, str]:
    kwargs = _skill_event_context_kwargs(context, None)
    kwargs.pop("skill_name", None)
    return kwargs


def _skill_selection_record_from_hits(
    hits: Sequence[SkillDiscoveryHit],
    *,
    query: str,
    source: str,
    method: str,
) -> dict[str, Any]:
    selected = [hit.skill_id for hit in hits if hit.skill_id]
    return {
        "method": method,
        "source": source,
        "task": str(query or "")[:500],
        "available_skills": selected,
        "selected": selected,
        "scores": [
            {
                "skill_id": hit.skill_id,
                "name": hit.name,
                "score": hit.score,
            }
            for hit in hits
        ],
    }


async def _record_skill_selection_for_hits(
    store: Any | None,
    context: Any | None,
    hits: Sequence[SkillDiscoveryHit],
    *,
    query: str,
    source: str,
    method: str,
) -> None:
    skill_ids = [hit.skill_id for hit in hits if hit.skill_id]
    if not skill_ids:
        return
    context_kwargs = _skill_selection_context_kwargs(context)
    if store is not None:
        try:
            await store.record_skill_selection(
                skill_ids,
                source=source,
                **context_kwargs,
                query=query,
            )
        except Exception:
            logger.debug("Skill selection event record failed", exc_info=True)
    try:
        from openspace.recording import RecordingManager

        await RecordingManager.record_skill_selection(
            _skill_selection_record_from_hits(
                hits,
                query=query,
                source=source,
                method=method,
            )
        )
    except Exception:
        logger.debug("Skill selection metadata record failed", exc_info=True)


def _record_skill_selection_for_hits_now(
    store: Any | None,
    context: Any | None,
    hits: Sequence[SkillDiscoveryHit],
    *,
    query: str,
    source: str,
    method: str,
) -> None:
    skill_ids = [hit.skill_id for hit in hits if hit.skill_id]
    if not skill_ids:
        return
    context_kwargs = _skill_selection_context_kwargs(context)
    if store is not None:
        try:
            store.record_skill_selection_now(
                skill_ids,
                source=source,
                **context_kwargs,
                query=query,
            )
        except Exception:
            logger.debug("Automatic skill selection event record failed", exc_info=True)
    try:
        from openspace.recording import RecordingManager

        RecordingManager.record_skill_selection_now(
            _skill_selection_record_from_hits(
                hits,
                query=query,
                source=source,
                method=method,
            )
        )
    except Exception:
        logger.debug("Automatic skill selection metadata record failed", exc_info=True)


def _skill_event_context_kwargs(
    context: Any | None,
    skill: Any | None = None,
) -> dict[str, str]:
    """Build common skill_events attribution fields from ToolUseContext."""

    task_id = ""
    turn_id = ""
    agent_id = ""
    if context is not None:
        task_id = str(
            getattr(context, "task_id", None)
            or getattr(context, "parent_task_id", None)
            or ""
        )
        turn_value = getattr(context, "current_iteration", None)
        if turn_value is not None:
            turn_id = str(turn_value)
        agent_id = str(getattr(context, "agent_id", None) or "")

    return {
        "task_id": task_id,
        "turn_id": turn_id,
        "agent_id": agent_id,
        "skill_name": str(getattr(skill, "name", "") or ""),
    }


def _tool_event_ref_id_for_context(
    context: Any | None,
    tool_use_id: str | None,
) -> str | None:
    tool_use_id = str(tool_use_id or "").strip()
    if context is None or not tool_use_id:
        return None
    session_id = str(getattr(context, "session_id", None) or "none")
    task_id = str(
        getattr(context, "task_id", None)
        or getattr(context, "parent_task_id", None)
        or "none"
    )
    agent_id = str(getattr(context, "agent_id", None) or "primary")
    return f"tool_event:{session_id}:{task_id}:{agent_id}:{tool_use_id}"


async def _record_skill_event_for_context(
    store: Any | None,
    skill: Any | None,
    event_type: str,
    *,
    source: str,
    context: Any | None = None,
    query: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> None:
    if store is None or skill is None:
        return
    try:
        event_metadata = dict(metadata or {})
        if context is not None and getattr(context, "session_id", None):
            event_metadata.setdefault("session_id", str(context.session_id))
        await store.record_skill_event(
            skill.skill_id,
            event_type,
            source=source,
            **_skill_event_context_kwargs(context, skill),
            query=query,
            metadata=event_metadata,
        )
    except Exception:
        logger.debug("Skill event record failed", exc_info=True)


async def record_skill_permission_decision_for_context(
    context: Any | None,
    input: Mapping[str, Any] | None,
    event_type: str,
    *,
    source: str,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    """Record a Skill permission decision from the shared tool pipeline."""

    if context is None or input is None:
        return
    store = getattr(context, "skill_store", None)
    registry = getattr(context, "skill_registry", None)
    if store is None or registry is None:
        return
    skill_name = _normalize_skill_name(str(input.get("skill") or ""))
    if not skill_name:
        return
    try:
        meta = registry.resolve_skill_for_model(skill_name)
    except Exception:
        meta = None
    await _record_skill_event_for_context(
        store,
        meta,
        event_type,
        source=source,
        context=context,
        metadata=metadata,
    )


def _skill_rule_content_matches(rule_content: str | None, skill_name: str) -> bool:
    if rule_content is None:
        return True
    normalized_rule = str(rule_content).strip()
    if normalized_rule.startswith("/"):
        normalized_rule = normalized_rule[1:]
    normalized_skill = _normalize_skill_name(skill_name)
    if normalized_rule == normalized_skill:
        return True
    if normalized_rule.endswith(":*"):
        return normalized_skill.startswith(normalized_rule[:-2])
    return False


def _matching_skill_rule(
    context: ToolPermissionContext,
    skill_name: str,
    behavior: str,
):
    if behavior == "deny":
        from openspace.grounding.core.permissions import get_deny_rules as get_rules
    elif behavior == "allow":
        from openspace.grounding.core.permissions import get_allow_rules as get_rules
    else:
        from openspace.grounding.core.permissions import get_ask_rules as get_rules

    for rule in get_rules(context):
        value = rule.rule_value
        if value.tool_name != SKILL_TOOL_NAME:
            continue
        if _skill_rule_content_matches(value.rule_content, skill_name):
            return rule
    return None


def _skill_has_only_safe_properties(skill: "SkillMeta") -> bool:
    """Return whether a skill can load without an interactive permission ask.

    Matches OpenSpace's safe-property intent while keeping OS high-risk runtime fields
    explicit: ``allowed-tools``, ``hooks``, ``shell`` and unknown meaningful
    frontmatter require ask unless an allow rule exists.
    """

    return not (
        skill.allowed_tools
        or skill.hooks
        or skill.shell
        or skill.model
        or skill.effort
        or skill.execution_context
        or skill.agent
        or skill.unknown_fields
    )


def _skill_permission_ask_message(skill: "SkillMeta") -> str:
    details: list[str] = []
    if skill.allowed_tools:
        details.append("allowed-tools=" + ", ".join(skill.allowed_tools))
    if skill.hooks:
        hook_events = ", ".join(str(key) for key in skill.hooks.keys())
        http_urls = _skill_http_hook_urls(skill.hooks)
        hook_text = f"hooks={hook_events or 'configured'}"
        if http_urls:
            hook_text += "; http=" + ", ".join(http_urls)
        details.append(hook_text)
    if skill.shell:
        details.append(f"shell={skill.shell}")
    if skill.model:
        details.append(f"model={skill.model}")
    if skill.effort:
        details.append(f"effort={skill.effort}")
    if skill.execution_context:
        details.append(f"context={skill.execution_context}")
    if skill.agent:
        details.append(f"agent={skill.agent}")
    if skill.unknown_fields:
        details.append("unknown-fields=" + ", ".join(sorted(skill.unknown_fields)))
    suffix = f" ({'; '.join(details)})" if details else ""
    return f"Execute skill: {skill.name}{suffix}"


def _skill_http_hook_urls(hooks: Mapping[str, Any]) -> list[str]:
    urls: list[str] = []
    for matchers in hooks.values():
        if not isinstance(matchers, Sequence) or isinstance(matchers, (str, bytes, bytearray)):
            continue
        for matcher in matchers:
            if not isinstance(matcher, Mapping):
                continue
            for hook in matcher.get("hooks") or []:
                if isinstance(hook, Mapping) and str(hook.get("type") or "") == "http":
                    url = str(hook.get("url") or "").strip()
                    if url:
                        urls.append(url)
    return urls


def _message_attachment(message: Mapping[str, Any]) -> Mapping[str, Any] | None:
    meta = message.get("_meta")
    if not isinstance(meta, Mapping):
        return None
    attachment = meta.get("attachment")
    return attachment if isinstance(attachment, Mapping) else None


def _string_items(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _skill_names_from_listing_content(value: Any) -> list[str]:
    if not isinstance(value, str):
        return []
    names: list[str] = []
    for line in value.splitlines():
        match = re.match(r"\s*-\s*([^:\n]+?)(?::|\s*$)", line)
        if match:
            name = match.group(1).strip()
            if name:
                names.append(name)
    return names


def _record_from_invoked_attachment(item: Mapping[str, Any]) -> SkillInvocationRecord | None:
    name = str(item.get("name") or "").strip()
    content = str(item.get("content") or "").strip()
    if not name or not content:
        return None
    return SkillInvocationRecord(
        skill_id=str(item.get("skill_id") or item.get("skillId") or name),
        name=name,
        path=str(item.get("path") or ""),
        content=content,
        allowed_tools=_string_items(item.get("allowed_tools") or item.get("allowedTools")),
        model=str(item.get("model")).strip() if item.get("model") else None,
        effort=str(item.get("effort")).strip() if item.get("effort") else None,
        execution_context=str(
            item.get("execution_context") or item.get("executionContext") or "inline"
        ),
    )


def _restore_invoked_skill_scope(
    record: SkillInvocationRecord,
    context: ToolUseContext,
    *,
    agent_id: str | None = None,
) -> None:
    """Restore task-local runtime modifiers for a retained inline skill."""

    if record.execution_context == "fork":
        return
    if not (record.allowed_tools or record.model or record.effort):
        return
    agent_key = agent_id or context.agent_id or "primary"
    scope = SkillInvocationScope(
        scope_id=f"restored:{agent_key}:{record.skill_id}",
        skill_id=record.skill_id,
        name=record.name,
        args=record.args,
        execution_mode=record.execution_context or "inline",
        allowed_tools_delta=list(record.allowed_tools),
        model_override=record.model,
        effort_override=record.effort,
        created_turn=int(getattr(context, "current_iteration", 0) or 0),
        permission_decision="restored",
    )
    context.activate_skill_scope(scope)


def _restore_invoked_skill_from_tool_metadata(
    message: Mapping[str, Any],
    context: ToolUseContext,
) -> int:
    meta = message.get("_meta")
    if not isinstance(meta, Mapping):
        return 0
    result_meta = meta.get("tool_result_metadata")
    if not isinstance(result_meta, Mapping) or result_meta.get("tool") != SKILL_TOOL_NAME:
        return 0
    skill_id = str(result_meta.get("skill_id") or "").strip()
    if not skill_id:
        return 0

    registry = getattr(context, "skill_registry", None)
    if registry is None:
        return 0
    try:
        skill = registry.get_skill(skill_id)
        if skill is None:
            return 0
        content = registry.build_skill_invocation_context(skill, args="")
    except Exception:
        return 0
    if not content:
        return 0

    context.record_invoked_skill(
        SkillInvocationRecord(
            skill_id=skill.skill_id,
            name=skill.name,
            path=str(skill.path),
            content=content,
            allowed_tools=list(skill.allowed_tools),
            model=skill.model,
            effort=skill.effort,
            execution_context=skill.execution_context or "inline",
        )
    )
    context.mark_skills_discovered([skill.name])
    return 1


def _skill_visible_to_context(skill: "SkillMeta", context: Any | None) -> bool:
    """Return whether a conditional ``paths`` skill is model-visible now."""

    if not skill.conditional_paths:
        return True
    if context is None:
        return False
    if skill.name in getattr(context, "path_activated_skill_names", set()):
        return True
    for records in (getattr(context, "invoked_skills_by_agent", {}) or {}).values():
        for record in records:
            if (
                getattr(record, "skill_id", None) == skill.skill_id
                or getattr(record, "name", None) == skill.name
            ):
                return True
    return False


def _append_skill_allowed_tools(
    context: ToolUseContext,
    allowed_tools: Sequence[str],
) -> None:
    perm_ctx = getattr(context, "permission_context", None)
    if perm_ctx is None or not allowed_tools:
        return

    allow_rules = {
        source: list(rules)
        for source, rules in perm_ctx.always_allow_rules.items()
    }
    command_rules = allow_rules.setdefault("command", [])
    for rule in allowed_tools:
        raw = str(rule).strip()
        if raw and raw not in command_rules:
            command_rules.append(raw)

    context.permission_context = ToolPermissionContext(
        mode=perm_ctx.mode,
        additional_working_directories=perm_ctx.additional_working_directories,
        always_allow_rules={
            source: tuple(rules)
            for source, rules in allow_rules.items()
        },
        always_deny_rules=perm_ctx.always_deny_rules,
        always_ask_rules=perm_ctx.always_ask_rules,
        is_bypass_permissions_mode_available=perm_ctx.is_bypass_permissions_mode_available,
        stripped_dangerous_rules=perm_ctx.stripped_dangerous_rules,
        should_avoid_permission_prompts=perm_ctx.should_avoid_permission_prompts,
        await_automated_checks_before_dialog=perm_ctx.await_automated_checks_before_dialog,
        pre_plan_mode=perm_ctx.pre_plan_mode,
    )


def _build_skill_context_modifier(
    meta: "SkillMeta",
    args: str,
    *,
    scope_id: str | None = None,
    invocation_tool_use_id: str | None = None,
    skill_event_ref_id: str | None = None,
):
    def _modifier(context: ToolUseContext) -> ToolUseContext:
        scope = SkillInvocationScope(
            scope_id=scope_id or f"skill_{uuid.uuid4().hex[:12]}",
            skill_id=meta.skill_id,
            name=meta.name,
            args=args,
            source=meta.source,
            loaded_from=meta.loaded_from,
            execution_mode=meta.execution_context or "inline",
            allowed_tools_delta=list(meta.allowed_tools),
            model_override=meta.model,
            effort_override=meta.effort,
            agent_type=meta.agent,
            hooks_enabled=bool(meta.hooks),
            shell=meta.shell,
            invocation_tool_use_id=invocation_tool_use_id,
            skill_event_ref_id=skill_event_ref_id,
            created_turn=getattr(context, "current_iteration", 0),
        )
        context.activate_skill_scope(scope)
        _register_skill_hooks_for_scope(context, meta, scope)
        return context

    return _modifier


def _register_skill_hooks_for_scope(
    context: ToolUseContext,
    meta: "SkillMeta",
    scope: SkillInvocationScope,
) -> None:
    hooks = getattr(meta, "hooks", None)
    hook_registry = getattr(context, "hook_registry", None)
    if not isinstance(hooks, Mapping) or hook_registry is None:
        return

    try:
        from openspace.services.tooling.hooks import HookEvent
    except Exception:
        return

    registered = 0
    for event_name, matchers in hooks.items():
        try:
            event = HookEvent(str(event_name))
        except Exception:
            logger.debug("Skill hook event %r is not supported by OS", event_name)
            continue
        if not isinstance(matchers, Sequence) or isinstance(matchers, (str, bytes, bytearray)):
            continue
        for matcher in matchers:
            if not isinstance(matcher, Mapping):
                continue
            matcher_value = str(matcher.get("matcher") or "").strip()
            tool_name = _skill_hook_registration_tool_name(event.value, matcher_value)
            for hook in matcher.get("hooks") or []:
                if not isinstance(hook, Mapping):
                    continue
                hook_type = str(hook.get("type") or "command")
                if hook_type not in {"command", "prompt", "http", "agent"}:
                    logger.debug(
                        "Skill hook %s:%s uses unsupported hook type %r",
                        meta.name,
                        event.value,
                        hook.get("type"),
                    )
                    continue
                if not _skill_hook_has_payload(hook_type, hook):
                    continue
                callback = _build_skill_hook_callback(
                    meta=meta,
                    event=event.value,
                    matcher=matcher_value,
                    hook=dict(hook),
                    hook_registry=hook_registry,
                )
                try:
                    registration = hook_registry.register(
                        event,
                        callback,
                        tool_name=tool_name,
                        priority=90,
                        name=f"skill:{meta.name}:{event.value}:{registered}",
                        once=False,
                    )
                    setattr(callback, "_skill_hook_registration", registration)
                except Exception:
                    logger.debug("Skill hook registration failed", exc_info=True)
                    continue
                scope.hook_registrations.append(registration)
                registered += 1

    if registered:
        logger.info("Registered %d hook(s) from skill %s", registered, meta.name)


def _skill_hook_registration_tool_name(event: str, matcher: str) -> str | None:
    """Return an exact tool-name filter when the matcher is safe to prefilter.

    OpenSpace's matcher language allows wildcard, pipe-separated exact lists and regex.
    OS HookRegistry only supports exact tool-name prefiltering, so complex
    matchers register globally and are filtered inside the skill hook callback.
    """
    if event not in {
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "PermissionDenied",
        "PermissionRequest",
    }:
        return None
    if not matcher:
        return None
    raw = matcher.strip()
    if raw == "*" or "|" in raw:
        return None
    if re.search(r"[\\^$.*+?\[\]{}]", raw):
        return None
    try:
        rule = parse_rule_value(raw)
    except Exception:
        return None
    return rule.tool_name


def _skill_hook_has_payload(hook_type: str, hook: Mapping[str, Any]) -> bool:
    if hook_type == "command":
        return bool(str(hook.get("command") or "").strip())
    if hook_type == "prompt":
        return bool(str(hook.get("prompt") or "").strip())
    if hook_type == "http":
        return bool(str(hook.get("url") or "").strip())
    if hook_type == "agent":
        return bool(str(hook.get("prompt") or "").strip())
    return False


def _build_skill_hook_callback(
    *,
    meta: "SkillMeta",
    event: str,
    matcher: str,
    hook: Mapping[str, Any],
    hook_registry: Any,
):
    async def _finish(result):
        if (
            bool(hook.get("once"))
            and getattr(result, "outcome", "success") == "success"
            and getattr(result, "blocking_error", None) is None
        ):
            registration = getattr(_callback, "_skill_hook_registration", None)
            if registration is not None:
                try:
                    hook_registry.unregister(registration)
                except Exception:
                    logger.debug("Skill hook once unregister failed", exc_info=True)
        return result

    async def _callback(
        tool_name: str = "",
        tool_input: dict[str, Any] | None = None,
        tool_use_id: str = "",
        context: ToolUseContext | None = None,
        **kwargs: Any,
    ):
        from openspace.services.tooling.hooks import HookBlockingError, HookResult

        hook_input = tool_input or {}
        if not _skill_hook_matcher_matches(
            matcher,
            event=event,
            tool_name=tool_name,
            tool_input=hook_input,
            kwargs=kwargs,
        ):
            return HookResult()
        if hook.get("if") and not _skill_hook_condition_matches(
            hook.get("if"),
            tool_name,
            hook_input,
        ):
            return HookResult()
        if context is None:
            return HookResult(
                blocking_error=HookBlockingError(
                    "Skill hook requires ToolUseContext",
                    command=str(hook.get("command") or hook.get("prompt") or hook.get("url") or ""),
                ),
                outcome="blocking",
            )

        hook_type = str(hook.get("type") or "command")
        if hook_type == "prompt":
            return await _finish(await _execute_skill_prompt_hook(
                meta=meta,
                event=event,
                hook=hook,
                tool_name=tool_name,
                tool_input=hook_input,
                tool_use_id=tool_use_id,
                context=context,
                kwargs=kwargs,
            ))
        if hook_type == "http":
            return await _finish(await _execute_skill_http_hook(
                meta=meta,
                event=event,
                hook=hook,
                tool_name=tool_name,
                tool_input=hook_input,
                tool_use_id=tool_use_id,
                context=context,
                kwargs=kwargs,
            ))
        if hook_type == "agent":
            return await _finish(await _execute_skill_agent_hook(
                meta=meta,
                event=event,
                hook=hook,
                tool_name=tool_name,
                tool_input=hook_input,
                tool_use_id=tool_use_id,
                context=context,
                kwargs=kwargs,
            ))

        shell_tool = _find_skill_prompt_shell_tool(
            context,
            str(hook.get("shell") or meta.shell or "bash").strip().lower() or None,
        )
        command = _render_skill_hook_command(
            str(hook.get("command") or ""),
            meta=meta,
            event=event,
            tool_name=tool_name,
            tool_input=hook_input,
            tool_use_id=tool_use_id,
            context=context,
            kwargs=kwargs,
        )
        if shell_tool is None:
            return HookResult(
                blocking_error=HookBlockingError(
                    "Skill command hook requires bash/powershell tool",
                    command=command,
                ),
                outcome="blocking",
            )
        if hook.get("async") is True or hook.get("asyncRewake") is True:
            return await _finish(
                _schedule_skill_async_command_hook(
                    meta=meta,
                    event=event,
                    hook=hook,
                    shell_tool=shell_tool,
                    command=command,
                    context=context,
                )
            )

        result = await _execute_skill_hook_command_via_runtime(
            shell_tool=shell_tool,
            command=command,
            context=context,
            timeout=int(hook.get("timeout") or 30),
            description=str(hook.get("statusMessage") or f"Skill hook: {meta.name}"),
        )
        output = extract_text_from_content(result.content).strip()
        if result.status != ToolStatus.SUCCESS:
            return HookResult(
                blocking_error=HookBlockingError(
                    output or result.error or "Skill hook command failed",
                    command=command,
                ),
                outcome="blocking",
            )
        if _looks_like_hook_json(output):
            return await _finish(_hook_result_from_ok_json(
                output,
                hook_label=f"Skill command hook `{meta.name}` ({event})",
                command=command,
                event=event,
            ))
        if output:
            return await _finish(HookResult(
                system_message=(
                    f"Skill hook `{meta.name}` ({event}) output:\n{output}"
                )
            ))
        return await _finish(HookResult())

    return _callback


def _schedule_skill_async_command_hook(
    *,
    meta: "SkillMeta",
    event: str,
    hook: Mapping[str, Any],
    shell_tool: Any,
    command: str,
    context: ToolUseContext,
):
    from openspace.services.tooling.hooks import HookResult

    async def _runner() -> None:
        hook_name = f"skill:{meta.name}:{event}:async"
        try:
            await context.emit_event(
                "hook_async_start",
                {
                    "hook_name": hook_name,
                    "hook_event": event,
                    "skill_id": meta.skill_id,
                    "skill_name": meta.name,
                    "command": command,
                    "async_rewake": bool(hook.get("asyncRewake")),
                },
            )
            result = await _execute_skill_hook_command_via_runtime(
                shell_tool=shell_tool,
                command=command,
                context=context,
                timeout=int(hook.get("timeout") or 30),
                description=str(
                    hook.get("statusMessage") or f"Skill hook: {meta.name}"
                ),
                run_in_background=True,
            )
            output = extract_text_from_content(result.content).strip()
            await _maybe_queue_async_rewake_notification(
                meta=meta,
                event=event,
                hook=hook,
                command=command,
                output=output,
                result=result,
                context=context,
            )
            await context.emit_event(
                "hook_async_complete",
                {
                    "hook_name": hook_name,
                    "hook_event": event,
                    "skill_id": meta.skill_id,
                    "skill_name": meta.name,
                    "success": result.status == ToolStatus.SUCCESS,
                    "output": output,
                    "error": result.error,
                },
            )
        except Exception as exc:
            logger.debug("Skill async command hook failed", exc_info=True)
            await context.emit_event(
                "hook_async_complete",
                {
                    "hook_name": f"skill:{meta.name}:{event}:async",
                    "hook_event": event,
                    "skill_id": meta.skill_id,
                    "skill_name": meta.name,
                    "success": False,
                    "error": str(exc),
                },
            )

    task = asyncio.create_task(_runner())
    tasks = getattr(context, "background_hook_tasks", None)
    if isinstance(tasks, set):
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    return HookResult(
        system_message=(
            f"Skill async hook `{meta.name}` ({event}) started in background."
        )
    )


def _skill_hook_tool_runtime_context(context: ToolUseContext) -> ToolUseContext:
    from openspace.services.tooling.hooks import HookRegistry, setup_default_hooks

    hook_registry = HookRegistry()
    setup_default_hooks(hook_registry)
    return replace(
        context,
        hook_registry=hook_registry,
        agent_id=f"{context.agent_id}:skill_hook",
    )


async def _execute_skill_hook_command_via_runtime(
    *,
    shell_tool: Any,
    command: str,
    context: ToolUseContext,
    timeout: int,
    description: str,
    run_in_background: bool = False,
) -> ToolResult:
    from openspace.tool_runtime.pipeline.execution import (
        run_tool_use,
        tool_call_result_to_tool_result,
    )

    tool_name = str(getattr(shell_tool, "name", "") or "")
    if not tool_name:
        return ToolResult(
            status=ToolStatus.ERROR,
            content="Skill command hook requires a named shell tool",
            error="missing shell tool name",
            metadata={"hook_command": True},
        )

    hook_context = _skill_hook_tool_runtime_context(context)
    tool_input: dict[str, Any] = {
        "command": command,
        "timeout": timeout,
        "description": description,
        "_skill_hook_command": True,
    }
    if run_in_background:
        tool_input["run_in_background"] = True

    previous_context = getattr(shell_tool, "_current_context", None)
    try:
        result = await run_tool_use(
            {
                "id": f"skill_hook_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": tool_input,
                },
            },
            {tool_name: shell_tool},
            hook_context,
        )
    finally:
        if hasattr(shell_tool, "set_context"):
            shell_tool.set_context(previous_context)

    tool_result = tool_call_result_to_tool_result(result)
    tool_result.metadata = {
        **(tool_result.metadata or {}),
        "hook_command": True,
        "runtime_hook_command": True,
    }
    return tool_result


async def _maybe_queue_async_rewake_notification(
    *,
    meta: "SkillMeta",
    event: str,
    hook: Mapping[str, Any],
    command: str,
    output: str,
    result: ToolResult,
    context: ToolUseContext,
) -> None:
    if hook.get("asyncRewake") is not True:
        return
    metadata = getattr(result, "metadata", None) or {}
    try:
        exit_code = int(metadata.get("exit_code"))
    except (TypeError, ValueError):
        exit_code = None
    if exit_code != 2:
        return

    queue = getattr(context, "async_rewake_queue", None)
    hook_name = f"skill:{meta.name}:{event}:asyncRewake"
    message_text = (
        f"Stop hook blocking error from command \"{hook_name}\": "
        f"{output or getattr(result, 'error', None) or command}"
    )
    if queue is not None:
        from openspace.services.conversation.messages import build_agent_injection_message

        await queue.put(
            build_agent_injection_message(
                from_agent=hook_name,
                content=message_text,
                message_type="task-notification",
            )
        )
    await context.emit_event(
        "hook_async_rewake_queued",
        {
            "hook_name": hook_name,
            "hook_event": event,
            "skill_id": meta.skill_id,
            "skill_name": meta.name,
            "queued": queue is not None,
            "exit_code": exit_code,
        },
    )


def _render_skill_hook_command(
    command: str,
    *,
    meta: "SkillMeta",
    event: str,
    tool_name: str,
    tool_input: Mapping[str, Any],
    tool_use_id: str,
    context: ToolUseContext,
    kwargs: Mapping[str, Any],
) -> str:
    payload = _skill_hook_payload(
        event=event,
        tool_name=tool_name,
        tool_input=tool_input,
        tool_use_id=tool_use_id,
        context=context,
        kwargs=kwargs,
    )
    rendered = command.replace("$ARGUMENTS", shlex.quote(json.dumps(payload, ensure_ascii=False)))
    skill_root = str(getattr(meta.path, "parent", "") or "").strip()
    if skill_root:
        rendered = (
            f"OPENSPACE_SKILL_ROOT={shlex.quote(skill_root)} "
            f"{rendered}"
        )
    return rendered


async def _execute_skill_prompt_hook(
    *,
    meta: "SkillMeta",
    event: str,
    hook: Mapping[str, Any],
    tool_name: str,
    tool_input: Mapping[str, Any],
    tool_use_id: str,
    context: ToolUseContext,
    kwargs: Mapping[str, Any],
):
    from openspace.services.tooling.hooks import HookBlockingError, HookResult

    llm_client = getattr(context, "llm_client", None)
    if llm_client is None or not hasattr(llm_client, "call_model"):
        return HookResult(
            blocking_error=HookBlockingError(
                "Skill prompt hook requires an LLM client",
                command=str(hook.get("prompt") or ""),
            ),
            outcome="blocking",
        )
    prompt = _render_skill_hook_text(
        str(hook.get("prompt") or ""),
        meta=meta,
        event=event,
        tool_name=tool_name,
        tool_input=tool_input,
        tool_use_id=tool_use_id,
        context=context,
        kwargs=kwargs,
    )
    timeout_seconds = _hook_timeout_seconds(
        hook, default=DEFAULT_PROMPT_HOOK_TIMEOUT_SECONDS
    )
    try:
        response = await asyncio.wait_for(
            llm_client.call_model(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are evaluating an OpenSpace skill hook. "
                            "Respond only with JSON: {\"ok\": true} or "
                            "{\"ok\": false, \"reason\": \"...\"}."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                tools=[],
                model=str(hook.get("model") or context.model),
                tool_choice="none",
                abort_event=getattr(context, "abort_event", None),
            ),
            timeout=timeout_seconds,
        )
        text = extract_text_from_content(
            getattr(response, "assistant_message", {}).get("content", "")
        )
    except TimeoutError:
        return HookResult(outcome="cancelled")
    except Exception as exc:
        return HookResult(
            blocking_error=HookBlockingError(
                f"Skill prompt hook failed: {exc}",
                command=prompt,
            ),
            outcome="blocking",
        )
    return _hook_result_from_ok_json(
        text,
        hook_label=f"Skill prompt hook `{meta.name}` ({event})",
        command=prompt,
        event=event,
    )


def _hook_timeout_seconds(
    hook: Mapping[str, Any],
    *,
    default: float,
) -> float:
    raw = hook.get("timeout")
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    return value


async def _execute_skill_http_hook(
    *,
    meta: "SkillMeta",
    event: str,
    hook: Mapping[str, Any],
    tool_name: str,
    tool_input: Mapping[str, Any],
    tool_use_id: str,
    context: ToolUseContext,
    kwargs: Mapping[str, Any],
):
    from openspace.services.tooling.hooks import HookBlockingError, HookResult

    url = str(hook.get("url") or "").strip()
    policy_error = await _validate_http_hook_url(url, context)
    if policy_error:
        return HookResult(
            blocking_error=HookBlockingError(policy_error, command=url),
            outcome="blocking",
        )
    payload = _skill_hook_payload(
        event=event,
        tool_name=tool_name,
        tool_input=tool_input,
        tool_use_id=tool_use_id,
        context=context,
        kwargs=kwargs,
    )
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    allowed_env_vars = _effective_http_hook_env_vars(hook, context)
    for key, value in (hook.get("headers") or {}).items():
        headers[str(key)] = _interpolate_hook_header_value(str(value), allowed_env_vars)

    def _post() -> tuple[int, str]:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=float(hook.get("timeout") or 30)) as resp:
            return int(resp.status), resp.read().decode("utf-8", errors="replace")

    try:
        status, text = await asyncio.to_thread(_post)
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        return HookResult(
            blocking_error=HookBlockingError(
                f"Skill HTTP hook returned {exc.code}: {text}",
                command=url,
            ),
            outcome="blocking",
        )
    except Exception as exc:
        return HookResult(
            blocking_error=HookBlockingError(
                f"Skill HTTP hook failed: {exc}",
                command=url,
            ),
            outcome="blocking",
        )
    if status < 200 or status >= 300:
        return HookResult(
            blocking_error=HookBlockingError(
                f"Skill HTTP hook returned {status}: {text}",
                command=url,
            ),
            outcome="blocking",
        )
    parsed = _hook_result_from_ok_json(
        text,
        hook_label=f"Skill HTTP hook `{meta.name}` ({event})",
        command=url,
        event=event,
        allow_plain_success=True,
    )
    return parsed


async def _execute_skill_agent_hook(
    *,
    meta: "SkillMeta",
    event: str,
    hook: Mapping[str, Any],
    tool_name: str,
    tool_input: Mapping[str, Any],
    tool_use_id: str,
    context: ToolUseContext,
    kwargs: Mapping[str, Any],
):
    from openspace.services.tooling.hooks import HookBlockingError, HookResult

    if getattr(context, "llm_client", None) is None:
        return HookResult(
            blocking_error=HookBlockingError(
                "Skill agent hook requires an LLM client",
                command=str(hook.get("prompt") or ""),
            ),
            outcome="blocking",
        )

    from openspace.agents.agent_definitions import AgentDefinition, AgentSource
    from openspace.agents.agent_tool import run_agent

    prompt = _render_skill_hook_text(
        str(hook.get("prompt") or ""),
        meta=meta,
        event=event,
        tool_name=tool_name,
        tool_input=tool_input,
        tool_use_id=tool_use_id,
        context=context,
        kwargs=kwargs,
    )
    agent_def = AgentDefinition(
        agent_type=f"skill-hook-{meta.name}",
        when_to_use=f"Evaluate hook for skill {meta.name}",
        get_system_prompt=(
            "You are evaluating an OpenSpace skill hook. Return only JSON "
            "{\"ok\": true} or {\"ok\": false, \"reason\": \"...\"}."
        ),
        source=AgentSource.CUSTOM,
        tools="*",
        model=str(hook.get("model") or meta.model or context.model),
        description=f"Hook agent for {meta.name}",
        max_turns=max(1, int(hook.get("timeout") or 5)),
    )
    try:
        result = await run_agent(
            agent_def=agent_def,
            prompt=prompt,
            filtered_tools=_filter_tools_for_skill(context, meta.allowed_tools),
            allowed_agent_types=getattr(context, "allowed_agent_types", None),
            parent_context=context,
            parent_agent=None,
            grounding_client=None,
            llm_client=context.llm_client,
            resolved_model=str(hook.get("model") or meta.model or context.model),
            task_description=f"Skill hook: {meta.name}",
        )
    except Exception as exc:
        return HookResult(
            blocking_error=HookBlockingError(
                f"Skill agent hook failed: {exc}",
                command=prompt,
            ),
            outcome="blocking",
        )
    text = getattr(result, "text", None)
    if text is None:
        text = "\n".join(
            str(item.get("text") or item.get("content") or "")
            if isinstance(item, Mapping)
            else str(item)
            for item in (getattr(result, "content", []) or [])
        )
    return _hook_result_from_ok_json(
        text,
        hook_label=f"Skill agent hook `{meta.name}` ({event})",
        command=prompt,
        event=event,
    )


def _render_skill_hook_text(
    template: str,
    *,
    meta: "SkillMeta",
    event: str,
    tool_name: str,
    tool_input: Mapping[str, Any],
    tool_use_id: str,
    context: ToolUseContext,
    kwargs: Mapping[str, Any],
) -> str:
    payload = json.dumps(
        _skill_hook_payload(
            event=event,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_use_id=tool_use_id,
            context=context,
            kwargs=kwargs,
        ),
        ensure_ascii=False,
    )
    return template.replace("$ARGUMENTS", payload)


def _skill_hook_payload(
    *,
    event: str,
    tool_name: str,
    tool_input: Mapping[str, Any],
    tool_use_id: str,
    context: ToolUseContext,
    kwargs: Mapping[str, Any],
) -> dict[str, Any]:
    transcript_path = (
        getattr(context, "transcript_path", None)
        or getattr(getattr(context, "recording_manager", None), "transcript_path", None)
        or ""
    )
    payload: dict[str, Any] = {
        "session_id": str(getattr(context, "session_id", None) or ""),
        "transcript_path": str(transcript_path),
        "cwd": str(getattr(context, "cwd", "") or ""),
        "hook_event_name": event,
        # Backward-compatible OS field kept for existing hooks/tests.
        "event": event,
    }
    permission_mode = getattr(context, "permission_mode", None)
    if permission_mode:
        payload["permission_mode"] = str(permission_mode)
    agent_id = str(getattr(context, "agent_id", None) or "")
    if agent_id and agent_id not in {"primary", "main"}:
        payload["agent_id"] = str(agent_id)
    agent_type = getattr(context, "agent_type", None)
    if agent_type:
        payload["agent_type"] = str(agent_type)

    if event in {
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "PermissionDenied",
        "PermissionRequest",
    }:
        payload["tool_name"] = tool_name
        payload["tool_input"] = _json_safe_hook_value(dict(tool_input))
        if tool_use_id:
            payload["tool_use_id"] = tool_use_id

    if event == "PostToolUse" and "tool_result" in kwargs:
        payload["tool_response"] = _json_safe_hook_value(kwargs.get("tool_result"))
    elif event == "PostToolUseFailure":
        payload["error"] = str(kwargs.get("error") or "")
        if "is_interrupt" in kwargs:
            payload["is_interrupt"] = bool(kwargs.get("is_interrupt"))
    elif event == "PermissionDenied":
        payload["reason"] = str(kwargs.get("reason") or "")
    elif event == "PermissionRequest" and "permission_suggestions" in kwargs:
        payload["permission_suggestions"] = _json_safe_hook_value(
            kwargs.get("permission_suggestions")
        )
    elif event == "Notification":
        payload["message"] = str(kwargs.get("message") or "")
        title = kwargs.get("title")
        if title is not None:
            payload["title"] = str(title)
        payload["notification_type"] = str(kwargs.get("notification_type") or "info")
    elif event == "UserPromptSubmit":
        payload["prompt"] = str(kwargs.get("prompt") or "")
    elif event == "SessionStart":
        payload["source"] = str(kwargs.get("source") or "")
        model = kwargs.get("model")
        if model is not None:
            payload["model"] = str(model)
    elif event == "SessionEnd":
        payload["reason"] = str(kwargs.get("reason") or "")
    elif event in {"Stop", "SubagentStop"}:
        payload["stop_hook_active"] = bool(kwargs.get("stop_hook_active"))
        last_message = kwargs.get("last_assistant_message")
        if last_message is not None:
            payload["last_assistant_message"] = str(last_message)
        if event == "SubagentStop":
            if kwargs.get("agent_transcript_path") is not None:
                payload["agent_transcript_path"] = str(kwargs.get("agent_transcript_path"))
            if kwargs.get("agent_type") is not None:
                payload["agent_type"] = str(kwargs.get("agent_type"))
    elif event == "StopFailure":
        if "error" in kwargs:
            payload["error"] = _json_safe_hook_value(kwargs.get("error"))
        if kwargs.get("error_details") is not None:
            payload["error_details"] = str(kwargs.get("error_details"))
        if kwargs.get("last_assistant_message") is not None:
            payload["last_assistant_message"] = str(kwargs.get("last_assistant_message"))
    elif event in {"PreCompact", "PostCompact"}:
        compact_data = kwargs.get("compact_data")
        if isinstance(compact_data, Mapping):
            trigger = compact_data.get("trigger")
            if trigger is not None:
                payload["trigger"] = str(trigger)
            if "custom_instructions" in compact_data:
                payload["custom_instructions"] = compact_data.get("custom_instructions")

    if kwargs:
        payload["hook"] = {
            key: _json_safe_hook_value(value)
            for key, value in kwargs.items()
            if key not in {"context"}
        }
    return payload


def _json_safe_hook_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe_hook_value(val) for key, val in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe_hook_value(item) for item in value]
    if hasattr(value, "status") or hasattr(value, "content") or hasattr(value, "error"):
        data: dict[str, Any] = {}
        for attr in ("status", "content", "error", "metadata"):
            if hasattr(value, attr):
                raw = getattr(value, attr)
                if attr == "status" and hasattr(raw, "value"):
                    raw = raw.value
                data[attr] = _json_safe_hook_value(raw)
        return data
    return str(value)


def _hook_result_from_ok_json(
    text: str,
    *,
    hook_label: str,
    command: str,
    event: str = "",
    allow_plain_success: bool = False,
):
    from openspace.services.tooling.hooks import HookBlockingError, HookResult

    cleaned = _strip_json_fence(str(text or "").strip())
    data: Any = None
    if cleaned:
        try:
            data = json.loads(cleaned)
        except Exception:
            data = None
    if isinstance(data, Mapping) and "ok" in data:
        if bool(data.get("ok")):
            return HookResult(system_message=f"{hook_label} passed.")
        reason = str(data.get("reason") or "condition was not met")
        return HookResult(
            blocking_error=HookBlockingError(reason, command=command),
            prevent_continuation=True,
            stop_reason=reason,
            outcome="blocking",
        )
    if isinstance(data, Mapping):
        return _hook_result_from_hook_json(
            data,
            hook_label=hook_label,
            command=command,
            event=event,
        )
    if allow_plain_success:
        return HookResult(
            system_message=f"{hook_label} completed." + (f"\n{text}" if text else "")
        )
    return HookResult(
        blocking_error=HookBlockingError(
            f"{hook_label} did not return valid hook JSON",
            command=command,
        ),
        outcome="blocking",
    )


def _hook_result_from_hook_json(
    data: Mapping[str, Any],
    *,
    hook_label: str,
    command: str,
    event: str,
):
    from openspace.services.tooling.hooks import HookBlockingError, HookResult

    if data.get("async") is True:
        return HookResult(
            system_message=f"{hook_label} returned async hook output."
        )

    kwargs: dict[str, Any] = {}
    recognized_keys = {
        "async",
        "continue",
        "stopReason",
        "systemMessage",
        "decision",
        "reason",
        "hookSpecificOutput",
    }
    saw_supported_field = any(key in data for key in recognized_keys)
    if data.get("continue") is False:
        reason = str(data.get("stopReason") or "Execution stopped by hook")
        kwargs["prevent_continuation"] = True
        kwargs["stop_reason"] = reason

    if data.get("systemMessage"):
        kwargs["system_message"] = str(data.get("systemMessage"))

    decision = data.get("decision")
    if decision == "approve":
        kwargs["permission_behavior"] = "allow"
    elif decision == "block":
        reason = str(data.get("reason") or "Blocked by hook")
        kwargs["permission_behavior"] = "deny"
        kwargs["hook_permission_decision_reason"] = reason
        kwargs["blocking_error"] = HookBlockingError(reason, command=command)
        kwargs["outcome"] = "blocking"
    elif decision not in (None, ""):
        reason = f"Unknown hook decision type: {decision}"
        kwargs["blocking_error"] = HookBlockingError(reason, command=command)
        kwargs["outcome"] = "blocking"

    hook_specific = data.get("hookSpecificOutput")
    if isinstance(hook_specific, Mapping):
        hook_event_name = str(hook_specific.get("hookEventName") or "")
        if event and hook_event_name and hook_event_name != event:
            reason = (
                f"Hook returned incorrect event name: expected {event}, "
                f"got {hook_event_name}"
            )
            kwargs["blocking_error"] = HookBlockingError(reason, command=command)
            kwargs["outcome"] = "blocking"
            return HookResult(**kwargs)

        permission_decision = hook_specific.get("permissionDecision")
        permission_reason = hook_specific.get("permissionDecisionReason")
        if (
            hook_event_name == "PreToolUse"
            and permission_decision in {"allow", "ask", "deny"}
        ):
            kwargs["permission_behavior"] = permission_decision
            if permission_reason:
                kwargs["hook_permission_decision_reason"] = str(permission_reason)
            if permission_decision == "deny":
                reason = str(
                    permission_reason or data.get("reason") or "Blocked by hook"
                )
                kwargs["blocking_error"] = HookBlockingError(reason, command=command)
                kwargs["outcome"] = "blocking"
        elif (
            hook_event_name == "PreToolUse"
            and permission_decision not in (None, "")
        ):
            reason = f"Unknown hook permissionDecision type: {permission_decision}"
            kwargs["blocking_error"] = HookBlockingError(reason, command=command)
            kwargs["outcome"] = "blocking"

        additional_context = hook_specific.get("additionalContext")
        additional_context_events = {
            "PreToolUse",
            "UserPromptSubmit",
            "SessionStart",
            "Setup",
            "SubagentStart",
            "PostToolUse",
            "PostToolUseFailure",
            "Notification",
        }
        if (
            hook_event_name in additional_context_events
            and isinstance(additional_context, str)
            and additional_context
        ):
            kwargs["additional_context"] = additional_context

        if hook_event_name == "PreToolUse":
            updated_input = hook_specific.get("updatedInput")
            if isinstance(updated_input, Mapping):
                kwargs["updated_input"] = dict(updated_input)

        if hook_event_name == "SessionStart":
            initial_user_message = hook_specific.get("initialUserMessage")
            if isinstance(initial_user_message, str) and initial_user_message:
                kwargs["initial_user_message"] = initial_user_message

        if hook_event_name in {"SessionStart", "CwdChanged", "FileChanged"}:
            watch_paths = hook_specific.get("watchPaths")
            if isinstance(watch_paths, list) and all(
                isinstance(path, str) for path in watch_paths
            ):
                kwargs["watch_paths"] = list(watch_paths)

        if (
            hook_event_name == "PostToolUse"
            and hook_specific.get("updatedMCPToolOutput")
        ):
            kwargs["updated_tool_output"] = hook_specific.get("updatedMCPToolOutput")

        if hook_event_name == "PermissionDenied" and "retry" in hook_specific:
            kwargs["retry"] = bool(hook_specific.get("retry"))

        permission_request = hook_specific.get("decision")
        if (
            hook_event_name == "PermissionRequest"
            and isinstance(permission_request, Mapping)
        ):
            behavior = permission_request.get("behavior")
            if behavior == "allow":
                kwargs["permission_behavior"] = "allow"
                updated = permission_request.get("updatedInput")
                if isinstance(updated, Mapping):
                    kwargs["updated_input"] = dict(updated)
                updated_permissions = permission_request.get("updatedPermissions")
                if isinstance(updated_permissions, list):
                    kwargs["updated_permissions"] = list(updated_permissions)
            elif behavior == "deny":
                reason = str(
                    permission_request.get("message")
                    or data.get("reason")
                    or "Denied by hook"
                )
                kwargs["permission_behavior"] = "deny"
                kwargs["hook_permission_decision_reason"] = reason
                kwargs["blocking_error"] = HookBlockingError(reason, command=command)
                kwargs["outcome"] = "blocking"
                if permission_request.get("interrupt") is True:
                    kwargs["prevent_continuation"] = True
                    kwargs["stop_reason"] = reason

        if hook_event_name == "Elicitation" and hook_specific.get("action"):
            action = str(hook_specific.get("action"))
            kwargs["elicitation_response"] = {
                "action": action,
                "content": hook_specific.get("content"),
            }
            if action == "decline":
                reason = str(data.get("reason") or "Elicitation denied by hook")
                kwargs["blocking_error"] = HookBlockingError(reason, command=command)
                kwargs["outcome"] = "blocking"

        if hook_event_name == "ElicitationResult" and hook_specific.get("action"):
            action = str(hook_specific.get("action"))
            kwargs["elicitation_result_response"] = {
                "action": action,
                "content": hook_specific.get("content"),
            }
            if action == "decline":
                reason = str(
                    data.get("reason") or "Elicitation result blocked by hook"
                )
                kwargs["blocking_error"] = HookBlockingError(reason, command=command)
                kwargs["outcome"] = "blocking"

    if kwargs:
        return HookResult(**kwargs)
    if not saw_supported_field:
        reason = (
            f"{hook_label} returned unsupported hook JSON fields: "
            + ", ".join(sorted(str(key) for key in data.keys()))
        )
        return HookResult(
            blocking_error=HookBlockingError(reason, command=command),
            outcome="blocking",
        )
    return HookResult(system_message=f"{hook_label} completed.")


def _looks_like_hook_json(text: str) -> bool:
    cleaned = _strip_json_fence(str(text or "").strip())
    return cleaned.startswith("{") and cleaned.endswith("}")


def _strip_json_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip()


def _interpolate_hook_header_value(value: str, allowed_env_vars: set[str]) -> str:
    def _replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2) or ""
        if name not in allowed_env_vars:
            return ""
        return os.environ.get(name, "")

    return re.sub(
        r"\$\{([A-Z_][A-Z0-9_]*)\}|\$([A-Z_][A-Z0-9_]*)",
        _replace,
        value,
    ).replace("\r", "").replace("\n", "").replace("\x00", "")


def _effective_http_hook_env_vars(
    hook: Mapping[str, Any],
    context: ToolUseContext,
) -> set[str]:
    hook_vars = {str(item) for item in (hook.get("allowedEnvVars") or [])}
    policy_vars = getattr(context, "http_hook_allowed_env_vars", None)
    if policy_vars is None:
        raw_policy = os.environ.get("OPENSPACE_HTTP_HOOK_ALLOWED_ENV_VARS")
        if raw_policy is not None:
            policy_vars = [item.strip() for item in raw_policy.split(",")]
    if policy_vars is None:
        return set()
    allowed = {str(item) for item in policy_vars if str(item)}
    return hook_vars.intersection(allowed)


async def _validate_http_hook_url(url: str, context: ToolUseContext) -> str | None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return f"HTTP hook blocked: unsupported URL {url!r}"

    allowed_urls = _http_hook_allowed_urls(context)
    if not allowed_urls:
        return (
            "HTTP hook blocked: no allowed HTTP hook URL patterns configured "
            "(set context.http_hook_allowed_urls or OPENSPACE_ALLOWED_HTTP_HOOK_URLS)"
        )
    if not any(
        _url_matches_pattern(url, pattern) for pattern in allowed_urls
    ):
        return (
            f"HTTP hook blocked: {url} does not match any configured "
            "allowed HTTP hook URL pattern"
        )

    return await asyncio.to_thread(_ssrf_guard_http_hook_url, parsed)


def _http_hook_allowed_urls(context: ToolUseContext) -> list[str] | None:
    configured = getattr(context, "http_hook_allowed_urls", None)
    if configured is not None:
        return [str(item) for item in configured]
    raw = os.environ.get("OPENSPACE_ALLOWED_HTTP_HOOK_URLS")
    if raw is None:
        return None
    return [item.strip() for item in raw.split(",") if item.strip()]


def _url_matches_pattern(url: str, pattern: str) -> bool:
    try:
        import fnmatch

        return fnmatch.fnmatch(url, pattern)
    except Exception:
        return url == pattern


def _ssrf_guard_http_hook_url(parsed: urllib.parse.ParseResult) -> str | None:
    host = parsed.hostname
    if not host:
        return "HTTP hook blocked: URL has no host"
    try:
        ips = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, parsed.port or _default_url_port(parsed))
        except Exception as exc:
            return f"HTTP hook blocked: could not resolve {host}: {exc}"
        ips = []
        for info in infos:
            try:
                ips.append(ipaddress.ip_address(info[4][0]))
            except Exception:
                continue
    for ip in ips:
        if ip.is_loopback:
            continue
        if (
            ip.is_private
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return f"HTTP hook blocked: {host} resolves to restricted address {ip}"
    return None


def _default_url_port(parsed: urllib.parse.ParseResult) -> int:
    return 443 if parsed.scheme == "https" else 80


_TOOL_MATCH_ALIASES: dict[str, tuple[str, ...]] = {
    "bash": ("Bash",),
    "powershell": ("PowerShell",),
    "read": ("Read",),
    "edit": ("Edit",),
    "write": ("Write",),
    "grep": ("Grep",),
    "glob": ("Glob",),
    "ls": ("LS",),
    "web_search": ("WebSearch",),
    "web_fetch": ("WebFetch",),
    "ask_user_question": ("AskUserQuestion",),
    "task": ("Task",),
    "todo_write": ("TodoWrite",),
    "notebook_edit": ("NotebookEdit",),
}


def _skill_hook_matcher_matches(
    matcher: Any,
    *,
    event: str,
    tool_name: str,
    tool_input: Mapping[str, Any],
    kwargs: Mapping[str, Any],
) -> bool:
    raw = str(matcher or "").strip()
    if not raw or raw == "*":
        return True

    actual_tool = normalize_tool_name_for_rule(tool_name)
    if event in {
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "PermissionDenied",
        "PermissionRequest",
    }:
        try:
            rule = parse_rule_value(raw)
        except Exception:
            rule = None
        if rule is not None and "(" in raw and raw.endswith(")"):
            if rule.tool_name != actual_tool:
                return False
            return _skill_hook_rule_content_matches(
                rule.tool_name,
                rule.rule_content,
                tool_input,
            )

    match_query = _skill_hook_match_query(event, tool_name, kwargs)
    if match_query is None:
        # Events without a match query match all configured hooks.
        return True

    candidates = _skill_hook_match_candidates(match_query)
    if re.fullmatch(r"[A-Za-z0-9_|_]+", raw):
        expected = [normalize_tool_name_for_rule(part.strip()) for part in raw.split("|")]
        return any(candidate in expected for candidate in candidates)

    try:
        pattern = re.compile(raw)
    except re.error:
        logger.debug("Invalid skill hook matcher regex: %r", raw)
        return False
    return any(pattern.search(candidate) for candidate in candidates)


def _skill_hook_match_query(
    event: str,
    tool_name: str,
    kwargs: Mapping[str, Any],
) -> str | None:
    if event in {
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "PermissionDenied",
        "PermissionRequest",
    }:
        return normalize_tool_name_for_rule(tool_name)
    if event == "SessionStart":
        return str(kwargs.get("source") or "")
    if event in {"PreCompact", "PostCompact"}:
        compact_data = kwargs.get("compact_data")
        if isinstance(compact_data, Mapping):
            return str(compact_data.get("trigger") or "")
        return str(kwargs.get("trigger") or "")
    if event == "Notification":
        return str(kwargs.get("notification_type") or "")
    if event == "SessionEnd":
        return str(kwargs.get("reason") or "")
    if event == "StopFailure":
        return str(kwargs.get("error") or "")
    if event in {"SubagentStart", "SubagentStop"}:
        return str(kwargs.get("agent_type") or "")
    return None


def _skill_hook_match_candidates(match_query: str) -> list[str]:
    normalized = normalize_tool_name_for_rule(match_query)
    candidates = [normalized, match_query]
    candidates.extend(_TOOL_MATCH_ALIASES.get(normalized, ()))
    seen: set[str] = set()
    deduped: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            deduped.append(candidate)
    return deduped


def _skill_hook_condition_matches(
    condition: Any,
    tool_name: str,
    tool_input: Mapping[str, Any],
) -> bool:
    raw = str(condition or "").strip()
    if not raw:
        return True
    try:
        expected_tool = parse_rule_value(raw).tool_name
    except Exception:
        expected_tool = None
    actual_tool = normalize_tool_name_for_rule(tool_name)
    if expected_tool and expected_tool != actual_tool:
        return False
    try:
        rule = parse_rule_value(raw)
    except Exception:
        rule = None
    if rule is not None and "(" in raw and raw.endswith(")"):
        if rule.tool_name != actual_tool:
            return False
        return _skill_hook_rule_content_matches(
            rule.tool_name,
            rule.rule_content,
            tool_input,
        )
    if "(" not in raw or not raw.endswith(")"):
        return True
    pattern = raw.split("(", 1)[1][:-1].strip()
    if not pattern or pattern == "*":
        return True
    haystack = json.dumps(dict(tool_input), ensure_ascii=False)
    try:
        import fnmatch

        return fnmatch.fnmatch(haystack, pattern) or pattern in haystack
    except Exception:
        return pattern in haystack


def _skill_hook_rule_content_matches(
    tool_name: str,
    rule_content: str | None,
    tool_input: Mapping[str, Any],
) -> bool:
    if rule_content in (None, "", "*"):
        return True
    pattern = str(rule_content)
    candidates: list[str] = []
    if tool_name == "bash":
        candidates.append(str(tool_input.get("command") or ""))
    for key in ("file_path", "path", "notebook_path", "url"):
        if tool_input.get(key) is not None:
            candidates.append(str(tool_input.get(key)))
    if not candidates:
        candidates.append(json.dumps(dict(tool_input), ensure_ascii=False))
    try:
        import fnmatch

        return any(
            candidate == pattern
            or fnmatch.fnmatch(candidate, pattern)
            or pattern in candidate
            for candidate in candidates
        )
    except Exception:
        return any(candidate == pattern or pattern in candidate for candidate in candidates)


async def _execute_skill_prompt_shell_expansions(
    content: str,
    *,
    meta: "SkillMeta",
    context: ToolUseContext | None,
) -> str:
    """Execute OpenSpace prompt-shell blocks in loaded skill markdown.

    OpenSpace supports inline ``!`cmd` `` and fenced `````!`` blocks in skill markdown
    for local prompt generation.  OS mirrors the behavior only for local skills;
    MCP/remote skills are left untouched because their markdown is not trusted
    to execute local commands.
    """

    if context is None:
        return content
    if meta.loaded_from == "mcp":
        return content
    if "```!" not in content and "!`" not in content:
        return content

    matches: list[tuple[int, int, str, str]] = []
    for match in PROMPT_SHELL_BLOCK_RE.finditer(content):
        command = (match.group(1) or "").strip()
        if command:
            matches.append((match.start(), match.end(), match.group(0), command))
    if "!`" in content:
        for match in PROMPT_SHELL_INLINE_RE.finditer(content):
            command = (match.group(2) or "").strip()
            if command:
                replacement_target = match.group(0)
                matches.append((
                    match.start(),
                    match.end(),
                    replacement_target,
                    command,
                ))

    if not matches:
        return content

    shell_tool = _find_skill_prompt_shell_tool(context, meta.shell)
    if shell_tool is None:
        raise RuntimeError("skill prompt shell expansion requires the bash tool")

    old_permission_context = context.permission_context
    _append_skill_allowed_tools(context, meta.allowed_tools)
    try:
        from openspace.tool_runtime.pipeline.execution import (
            run_tool_use,
            tool_call_result_to_tool_result,
        )

        shell_tool_name = shell_tool.schema.name
        shell_tool_map = {shell_tool_name: shell_tool}
        replacements: list[tuple[int, int, str]] = []
        for start, end, matched_text, command in matches:
            tool_call = {
                "id": f"skill-prompt-shell-{len(replacements)}-{start}",
                "type": "function",
                "function": {
                    "name": shell_tool_name,
                    "arguments": {
                        "command": command,
                        "description": f"Skill prompt expansion: {meta.name}",
                    },
                },
            }
            result = tool_call_result_to_tool_result(
                await run_tool_use(tool_call, shell_tool_map, context)
            )
            if result.status != ToolStatus.SUCCESS:
                error = result.error or extract_text_from_content(result.content)
                raise RuntimeError(
                    f"Shell command failed for pattern {matched_text!r}: {error}"
                )
            output = extract_text_from_content(result.content).strip()
            if start < end and matched_text.startswith((" !`", "\t!`", "\n!`")):
                output = matched_text[0] + output
            replacements.append((start, end, output))
    finally:
        context.permission_context = old_permission_context

    updated = content
    for start, end, output in sorted(replacements, reverse=True):
        updated = updated[:start] + output + updated[end:]
    return updated


def _find_skill_prompt_shell_tool(
    context: ToolUseContext,
    shell: str | None,
) -> Any | None:
    candidates = list(getattr(context, "all_tools", None) or []) + list(
        getattr(context, "tools", None) or []
    )
    preferred = "powershell" if shell == "powershell" else "bash"
    for name in (preferred, "bash"):
        for tool in candidates:
            tool_name = str(getattr(tool, "name", "") or "")
            aliases = {str(alias) for alias in (getattr(tool, "aliases", []) or [])}
            if tool_name == name or name in aliases:
                return tool
    return None


def _filter_tools_for_skill(context: ToolUseContext, allowed_tools: Sequence[str]) -> list[Any]:
    tools = list(getattr(context, "all_tools", None) or getattr(context, "tools", []))
    if not allowed_tools:
        return tools
    allowed_names = {
        _allowed_tool_name_for_filter(item)
        for item in allowed_tools
        if str(item).strip()
    }
    allowed_names.discard("")
    if not allowed_names:
        return tools
    return [
        tool
        for tool in tools
        if getattr(tool, "name", "") in allowed_names
        or allowed_names.intersection(set(getattr(tool, "aliases", []) or ()))
    ]


def _allowed_tool_name_for_filter(raw: Any) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    try:
        return parse_rule_value(value).tool_name
    except Exception:
        return normalize_tool_name_for_rule(value.split("(", 1)[0].strip())


async def _execute_forked_skill(
    *,
    meta: "SkillMeta",
    content: str,
    args: str,
    context: ToolUseContext | None,
    context_modifier,
) -> ToolResult:
    if context is None or getattr(context, "llm_client", None) is None:
        result = ToolResult(
            status=ToolStatus.ERROR,
            content="Error: forked skill requires an active ToolUseContext and LLM client.",
            error="missing ToolUseContext or LLM client",
            metadata={
                "tool": SKILL_TOOL_NAME,
                "skill_id": meta.skill_id,
                "skill_name": meta.name,
                "execution_context": "fork",
            },
        )
        return result

    from openspace.agents.agent_definitions import AgentDefinition, AgentSource
    from openspace.agents.agent_tool import run_agent

    agent_type = meta.agent or "general-purpose"
    agent_def = _resolve_skill_agent_definition(context, agent_type, meta)
    if agent_def is None:
        agent_def = AgentDefinition(
            agent_type=agent_type,
            when_to_use=f"Execute skill {meta.name}",
            get_system_prompt=(
                "You are executing a single OpenSpace skill in an isolated "
                "forked context. Follow the skill instructions exactly and "
                "return a concise result for the parent agent."
            ),
            source=AgentSource.CUSTOM,
            tools="*",
            model=meta.model,
            effort=meta.effort,
            description=f"Forked skill runner for {meta.name}",
        )

    result = await run_agent(
        agent_def=agent_def,
        prompt=content,
        filtered_tools=_filter_tools_for_skill(context, meta.allowed_tools),
        allowed_agent_types=getattr(context, "allowed_agent_types", None),
        parent_context=context,
        parent_agent=None,
        grounding_client=None,
        llm_client=context.llm_client,
        resolved_model=meta.model or context.model,
        task_description=f"Skill: {meta.name}",
        child_context_modifier=context_modifier,
    )
    text = getattr(result, "text", None)
    if text is None:
        content_items = getattr(result, "content", []) or []
        text_parts = []
        for item in content_items:
            if isinstance(item, Mapping):
                text_parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                text_parts.append(str(item))
        text = "\n".join(part for part in text_parts if part).strip()
    tool_result = ToolResult(
        status=ToolStatus.SUCCESS if getattr(result, "status", "") == "completed" else ToolStatus.ERROR,
        content=(
            f"Skill \"{meta.name}\" completed (forked execution).\n\n"
            f"Result:\n{text or getattr(result, 'status', 'unknown')}"
        ),
        error=None if getattr(result, "status", "") == "completed" else text,
        metadata={
            "tool": SKILL_TOOL_NAME,
            "skill_id": meta.skill_id,
            "skill_name": meta.name,
            "execution_context": "fork",
            "agent_id": getattr(result, "agent_id", None),
            "agent_type": getattr(result, "agent_type", agent_type),
        },
    )
    attachment = create_attachment_message(
        {
            "type": "invoked_skill_content",
            "name": meta.name,
            "skill_id": meta.skill_id,
            "path": str(meta.path),
            "content": content,
            "agent_id": str(getattr(result, "agent_id", None) or ""),
            "allowed_tools": list(meta.allowed_tools),
            "model": meta.model,
            "effort": meta.effort,
            "execution_context": "fork",
        }
    )
    setattr(tool_result, "additional_messages", [attachment])
    return tool_result


def _resolve_skill_agent_definition(
    context: ToolUseContext,
    agent_type: str,
    meta: "SkillMeta",
):
    agent_result = getattr(context, "agent_definitions", None)
    active = getattr(agent_result, "active_agents", None)
    if active:
        for agent in active:
            if getattr(agent, "agent_type", None) == agent_type:
                if meta.model or meta.effort:
                    from dataclasses import replace

                    updates = {}
                    if meta.model:
                        updates["model"] = meta.model
                    if meta.effort:
                        updates["effort"] = meta.effort
                    return replace(agent, **updates)
                return agent
    return None


_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


def _score_skill(
    query_tokens: set[str],
    name: str,
    description: str,
    when_to_use: str | None,
    body: str,
) -> float:
    if not query_tokens:
        return 0.0
    name_tokens = _tokenize(name)
    desc_tokens = _tokenize(description)
    when_tokens = _tokenize(when_to_use or "")
    body_tokens = _tokenize(body[:4000])
    score = 0.0
    score += 4.0 * len(query_tokens & name_tokens)
    score += 2.0 * len(query_tokens & desc_tokens)
    score += 2.0 * len(query_tokens & when_tokens)
    score += 0.5 * len(query_tokens & body_tokens)
    phrase = " ".join(sorted(query_tokens))
    haystack = f"{name} {description} {when_to_use or ''}".lower()
    if phrase and phrase in haystack:
        score += 2.0
    return score


__all__ = [
    "DISCOVER_SKILLS_TOOL_NAME",
    "DiscoverSkillsTool",
    "SKILL_TOOL_NAME",
    "SkillDiscoveryHit",
    "SkillDiscoveryService",
    "SkillListingService",
    "SkillTool",
    "build_skill_discovery_messages",
    "build_skill_discovery_messages_async",
    "build_skill_listing_messages",
    "consume_dynamic_skill_triggers",
    "filter_to_bundled_and_mcp",
    "format_skills_within_budget",
    "has_skill_tool",
    "restore_skill_state_from_messages",
]
