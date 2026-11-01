"""RetrieveSkillTool — mid-iteration skill retrieval for GroundingAgent.

Registered as an internal tool so the LLM can pull in skill guidance
during execution when the initial skill set is insufficient.

Reuses the same pipeline as initial skill selection:
  quality filter → BM25+embedding pre-filter → LLM plan-then-select.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional

from openspace.grounding.core.tool.local_tool import LocalTool
from openspace.grounding.core.types import BackendType
from openspace.utils.logging import Logger

if TYPE_CHECKING:
    from openspace.llm import LLMClient
    from openspace.skill_engine import SkillRegistry
    from openspace.skill_engine.store import SkillStore

logger = Logger.get_logger(__name__)


class RetrieveSkillTool(LocalTool):
    """Internal tool: mid-iteration skill retrieval.

    Reuses ``SkillRegistry.select_skills_with_llm()`` so the same
    quality filter, BM25+embedding pre-filter, and plan-then-select
    LLM prompt are applied consistently.
    """

    _name = "retrieve_skill"
    _description = (
        "Search for specialized skill guidance when the current approach "
        "isn't working or the task requires domain-specific knowledge. "
        "Returns step-by-step instructions if a relevant skill is found."
    )
    backend_type = BackendType.SYSTEM

    def __init__(
        self,
        skill_registry: "SkillRegistry",
        backends: Optional[List[str]] = None,
        llm_client: Optional["LLMClient"] = None,
        skill_store: Optional["SkillStore"] = None,
    ):
        super().__init__()
        self._skill_registry = skill_registry
        self._backends = backends
        self._llm_client = llm_client
        self._skill_store = skill_store

    def _load_skill_quality(self) -> Optional[Dict[str, Dict[str, Any]]]:
        if not self._skill_store:
            return None
        try:
            rows = self._skill_store.get_summary(active_only=True)
            return {
                r["skill_id"]: {
                    "total_selections": r.get("total_selections", 0),
                    "total_applied": r.get("total_applied", 0),
                    "total_completions": r.get("total_completions", 0),
                    "total_fallbacks": r.get("total_fallbacks", 0),
                }
                for r in rows
            }
        except Exception:
            return None

    async def _arun(self, query: str) -> str:
        if self._llm_client:
            # Full pipeline: quality filter → BM25+embedding → LLM plan-then-select
            quality = self._load_skill_quality()
            selected, record = await self._skill_registry.select_skills_with_llm(
                query,
                llm_client=self._llm_client,
                max_skills=1,
                skill_quality=quality,
            )
            if record:
                plan = record.get("brief_plan", "")
                if plan:
                    logger.info(f"retrieve_skill plan: {plan}")
        else:
            # Fallback: BM25+embedding only (no LLM available)
            from openspace.cloud.search import hybrid_search_skills

            results = await hybrid_search_skills(
                query=query,
                local_skills=self._skill_registry.list_skills(),
                source="local",
                limit=1,
            )
            if not results:
                return "No relevant skills found for this query."

            hit_ids = {r["skill_id"] for r in results}
            selected = [
                s for s in self._skill_registry.list_skills()
                if s.skill_id in hit_ids
            ]

        if not selected:
            return "No relevant skills found for this query."

        logger.info(f"retrieve_skill matched: {[s.skill_id for s in selected]}")
        return self._skill_registry.build_context_injection(
            selected, backends=self._backends,
        )
