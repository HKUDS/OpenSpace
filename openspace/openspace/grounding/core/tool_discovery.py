from __future__ import annotations

import re
from typing import Any

from openspace.grounding.core.tool import BaseTool
from openspace.grounding.core.types import BackendType, ToolResult, ToolStatus


TOOL_DISCOVERY_TOOL_NAME = "tool_search"
TOOL_SEARCH_TOOL_NAME = TOOL_DISCOVERY_TOOL_NAME
LOW_CONFIDENCE_MIN_SCORE = 6
LARGE_CANDIDATE_THRESHOLD = 80
PRESELECTOR_MIN_CANDIDATES = 20
PRESELECTOR_CANDIDATE_MULTIPLIER = 4


class ToolSearchTool(BaseTool):
    """Discover deferred tools and load their schemas on the next model turn."""

    _name = TOOL_DISCOVERY_TOOL_NAME
    _description = (
        "Search deferred tools by name or capability and load matching tool "
        "schemas for the next model turn."
    )
    backend_type = BackendType.META
    _is_read_only = True
    _is_concurrency_safe = True
    always_load = True
    search_hint = "discover deferred tools by name or capability"
    parameter_descriptions = {
        "query": (
            "Search query. Use select:<tool_name> for exact tool loading, "
            "or keywords such as 'github issue create'."
        ),
        "max_results": "Maximum number of deferred tools to load.",
    }

    def __init__(self, all_tools: list[BaseTool] | None = None):
        self._all_tools = list(all_tools or [])
        self._current_context: Any | None = None
        super().__init__()

    def set_context(self, context: Any) -> None:
        self._current_context = context

    def get_prompt(self, context: Any | None = None) -> str:
        return (
            "Search deferred tools and make matching tool schemas available on "
            "the next model turn. Use this when a needed MCP/GUI/domain tool is "
            "listed as deferred but is not currently callable. Prefer "
            "`select:<exact_tool_name>` when you know the tool name."
        )

    async def _arun(self, query: str, max_results: int = 5) -> ToolResult:
        ctx = self._current_context
        all_tools = list(getattr(ctx, "all_tools", None) or self._all_tools)
        active_names = {tool.name for tool in getattr(ctx, "tools", []) or []}
        discovered_names = set(getattr(ctx, "discovered_tool_names", set()) or set())
        deferred_names = set(getattr(ctx, "deferred_tool_names", set()) or set())
        deferred_tools = [
            tool for tool in all_tools
            if (tool.is_deferred or tool.name in deferred_names)
            and tool.name not in discovered_names
        ]

        ranked_matches, search_metadata = await _rank_with_optional_fallback(
            query,
            deferred_tools,
            max_results=max_results,
            context=ctx,
        )
        matches = [tool for tool, _ in ranked_matches]
        if query.lower().strip().startswith("select:") and not matches:
            # If the requested tool is already active, return a helpful no-op.
            requested = _parse_select_names(query)
            already_loaded = sorted(name for name in requested if name in active_names)
            if already_loaded:
                return ToolResult(
                    status=ToolStatus.SUCCESS,
                    content=(
                        "Requested tool(s) are already loaded: "
                        + ", ".join(already_loaded)
                    ),
                    metadata={"matches": already_loaded, "already_loaded": True},
                )

        matched_names = [tool.name for tool in matches]
        if ctx is not None and hasattr(ctx, "mark_tools_discovered"):
            ctx.mark_tools_discovered(matched_names)

        payload = {
            "query": query,
            "matches": matched_names,
            "loaded_next_turn": matched_names,
            "total_deferred_tools": len(deferred_tools),
            **search_metadata,
        }
        if not matched_names:
            pending_mcp_servers = _get_pending_mcp_servers(ctx)
            if pending_mcp_servers:
                payload["pending_mcp_servers"] = pending_mcp_servers
            content = "No matching deferred tools found"
            if pending_mcp_servers:
                content += (
                    ". Some MCP servers are still connecting: "
                    + ", ".join(pending_mcp_servers)
                    + ". Their tools will become available shortly - try searching again."
                )
        else:
            content = (
                "Loaded deferred tool schema(s) for the next model turn:\n"
                + "\n".join(f"- {name}" for name in matched_names)
                + "\n\nYou can call these tools after the next assistant turn begins."
            )
        return ToolResult(
            status=ToolStatus.SUCCESS,
            content=content,
            metadata=payload,
        )


def search_tools_by_keyword(
    query: str,
    deferred_tools: list[BaseTool],
    *,
    max_results: int = 5,
) -> list[BaseTool]:
    """OpenSpace keyword search over tool names, descriptions and hints."""

    return [tool for tool, _ in rank_tools_by_keyword(
        query,
        deferred_tools,
        max_results=max_results,
    )]


async def _rank_with_optional_fallback(
    query: str,
    deferred_tools: list[BaseTool],
    *,
    max_results: int,
    context: Any | None,
) -> tuple[list[tuple[BaseTool, int]], dict[str, Any]]:
    """Rank deferred tools, falling back to the system-side preselector.

    ``tool_search`` is primarily a schema-hydration handshake.  The OpenSpace
    keyword scorer should win when it has a clear lexical match.  The older
    OpenSpace preselector stays as a recall enhancer for two cases:
      1. very large candidate sets, where it can narrow the pool; and
      2. no/low-confidence keyword results.
    """

    query_kind = _query_kind(query, deferred_tools)
    ranked_all = rank_tools_by_keyword(
        query,
        deferred_tools,
        max_results=max_results,
    )
    ranked = ranked_all
    confidence = _keyword_confidence(ranked_all)
    metadata: dict[str, Any] = {
        "selection_method": query_kind,
        "keyword_top_score": confidence["top_score"],
        "keyword_confidence": confidence,
        "preselector_used": False,
        "preselector_reason": None,
    }

    if query_kind != "keyword":
        return ranked, metadata

    large_candidate_set = len(deferred_tools) > LARGE_CANDIDATE_THRESHOLD
    if large_candidate_set:
        narrowed_limit = max(
            PRESELECTOR_MIN_CANDIDATES,
            max_results * PRESELECTOR_CANDIDATE_MULTIPLIER,
        )
        narrowed = await _preselect_deferred_tools(
            query,
            deferred_tools,
            max_results=narrowed_limit,
            context=context,
        )
        if narrowed:
            ranked_narrowed = _rank_preselected_tools(
                query,
                narrowed,
                max_results=max_results,
            )
            if confidence["low_confidence"]:
                ranked = _merge_ranked(ranked_narrowed, ranked_all, max_results)
            else:
                ranked = _merge_keyword_with_preselector(
                    ranked_all,
                    ranked_narrowed,
                    max_results,
                )
            metadata.update({
                "selection_method": "preselector_narrowed_keyword",
                "preselector_used": True,
                "preselector_reason": "large_candidate_set",
                "preselector_candidate_count": len(narrowed),
            })

    if confidence["low_confidence"] and not metadata["preselector_used"]:
        fallback = await _preselect_deferred_tools(
            query,
            deferred_tools,
            max_results=max_results,
            context=context,
        )
        if fallback:
            fallback_ranked = _rank_preselected_tools(
                query,
                fallback,
                max_results=max_results,
            )
            ranked = _merge_ranked(fallback_ranked, ranked, max_results)
            metadata.update({
                "selection_method": "preselector_fallback",
                "preselector_used": True,
                "preselector_reason": confidence["reason"],
                "preselector_candidate_count": len(fallback),
            })

    return ranked, metadata


async def _preselect_deferred_tools(
    query: str,
    deferred_tools: list[BaseTool],
    *,
    max_results: int,
    context: Any | None,
) -> list[BaseTool]:
    if not deferred_tools:
        return []

    try:
        from .search_tools import ToolPreselector
    except Exception:
        return []

    llm_client = getattr(context, "llm_client", None)
    quality_manager = getattr(context, "quality_manager", None)

    try:
        preselector = ToolPreselector(
            max_tools=max_results,
            llm=llm_client,
            quality_manager=quality_manager,
        )
        if len(deferred_tools) <= max_results:
            return _rank_with_preselector_without_return_all(
                preselector,
                query,
                deferred_tools,
                max_results=max_results,
            )
        return await preselector._arun(
            task_prompt=query,
            candidate_tools=deferred_tools,
            max_tools=max_results,
        )
    except Exception:
        return []


def _rank_with_preselector_without_return_all(
    preselector: Any,
    query: str,
    deferred_tools: list[BaseTool],
    *,
    max_results: int,
) -> list[BaseTool]:
    mode = getattr(preselector, "_default_mode", "keyword")
    use_embedding_ranker = mode in {"semantic", "hybrid"}
    ranked = preselector._rank_tools(
        query,
        deferred_tools,
        top_k=max_results,
        mode=mode,
        use_embedding_ranker=use_embedding_ranker,
    )
    return [tool for tool, score in ranked if score > 0][:max_results]


def _query_kind(query: str, tools: list[BaseTool]) -> str:
    query_lower = (query or "").lower().strip()
    if query_lower.startswith("select:"):
        return "select"
    if any(tool.name.lower() == query_lower for tool in tools):
        return "exact"
    if query_lower.startswith("mcp__") and len(query_lower) > len("mcp__"):
        return "server_prefix"
    return "keyword"


def _keyword_confidence(
    ranked: list[tuple[BaseTool, int]],
) -> dict[str, Any]:
    if not ranked:
        return {
            "low_confidence": True,
            "reason": "no_keyword_matches",
            "top_score": 0,
            "min_score": LOW_CONFIDENCE_MIN_SCORE,
        }

    top_score = ranked[0][1]
    low_confidence = top_score < LOW_CONFIDENCE_MIN_SCORE
    return {
        "low_confidence": low_confidence,
        "reason": (
            "top_score_below_threshold"
            if low_confidence
            else "keyword_match"
        ),
        "top_score": top_score,
        "min_score": LOW_CONFIDENCE_MIN_SCORE,
    }


def _merge_ranked(
    primary: list[tuple[BaseTool, int]],
    secondary: list[tuple[BaseTool, int]],
    max_results: int,
) -> list[tuple[BaseTool, int]]:
    merged: list[tuple[BaseTool, int]] = []
    seen: set[str] = set()
    for tool, score in [*primary, *secondary]:
        if tool.name in seen:
            continue
        seen.add(tool.name)
        merged.append((tool, score))
        if len(merged) >= max_results:
            break
    return merged


def _rank_preselected_tools(
    query: str,
    tools: list[BaseTool],
    *,
    max_results: int,
) -> list[tuple[BaseTool, int]]:
    keyword_scores = {
        tool.name: score
        for tool, score in rank_tools_by_keyword(
            query,
            tools,
            max_results=len(tools),
        )
    }
    fallback_score = max(1, LOW_CONFIDENCE_MIN_SCORE - 1)
    ranked: list[tuple[BaseTool, int]] = []
    for tool in tools[:max_results]:
        ranked.append((tool, keyword_scores.get(tool.name, fallback_score)))
    return ranked


def _merge_keyword_with_preselector(
    keyword_ranked: list[tuple[BaseTool, int]],
    preselector_ranked: list[tuple[BaseTool, int]],
    max_results: int,
) -> list[tuple[BaseTool, int]]:
    if not keyword_ranked:
        return preselector_ranked[:max_results]
    if max_results <= 1:
        return keyword_ranked[:1]

    reserved_count = min(len(preselector_ranked), max(1, max_results // 2))
    primary = [
        *keyword_ranked[:1],
        *preselector_ranked[:reserved_count],
    ]
    secondary = [
        *keyword_ranked[1:],
        *preselector_ranked[reserved_count:],
    ]
    return _merge_ranked(primary, secondary, max_results)


def rank_tools_by_keyword(
    query: str,
    tools: list[BaseTool],
    *,
    max_results: int = 5,
) -> list[tuple[BaseTool, int]]:
    """Rank tools with the lightweight OpenSpace ToolSearch keyword scoring rules."""

    query_lower = (query or "").lower().strip()
    if not query_lower or not tools:
        return []

    # Fast path: exact name.
    for tool in tools:
        if tool.name.lower() == query_lower:
            return [(tool, 10_000)]

    # Fast path: mcp__server prefix.
    if query_lower.startswith("mcp__") and len(query_lower) > len("mcp__"):
        matches = [
            tool
            for tool in tools
            if tool.name.lower().startswith(query_lower)
        ]
        if matches:
            return [(tool, 9_000 - index) for index, tool in enumerate(matches[:max_results])]

    # Fast path: select:tool_a,tool_b.
    if query_lower.startswith("select:"):
        requested = _parse_select_names(query)
        by_name = {tool.name: tool for tool in tools}
        return [(by_name[name], 10_000) for name in requested if name in by_name][:max_results]

    raw_terms = [term for term in re.split(r"\s+", query_lower) if term]
    required_terms = [term[1:] for term in raw_terms if term.startswith("+") and len(term) > 1]
    optional_terms = [term for term in raw_terms if not term.startswith("+")]
    scoring_terms = required_terms + optional_terms if required_terms else raw_terms
    if not scoring_terms:
        return []

    term_patterns = {
        term: re.compile(r"\b" + re.escape(term) + r"\b")
        for term in scoring_terms
    }

    candidates = tools
    if required_terms:
        candidates = [
            tool for tool in tools
            if _matches_all_required_terms(tool, required_terms, term_patterns)
        ]

    scored: list[tuple[BaseTool, int]] = []
    for tool in candidates:
        score = _score_tool(tool, scoring_terms, term_patterns)
        if score > 0:
            scored.append((tool, score))
    scored.sort(key=lambda item: (-item[1], item[0].name))
    return scored[:max_results]


def _parse_select_names(query: str) -> list[str]:
    raw = query.split(":", 1)[1] if ":" in query else query
    return [name.strip() for name in raw.split(",") if name.strip()]


def _get_pending_mcp_servers(context: Any | None) -> list[str]:
    if context is None:
        return []
    for attr in ("pending_mcp_servers", "pending_mcp_server_names"):
        value = getattr(context, attr, None)
        if isinstance(value, (list, tuple, set)):
            return sorted(str(item) for item in value if item)
    return []


def _matches_all_required_terms(
    tool: BaseTool,
    required_terms: list[str],
    term_patterns: dict[str, re.Pattern[str]],
) -> bool:
    parsed = _parse_tool_name(tool.name)
    desc = (tool.description or "").lower()
    hint = (getattr(tool, "search_hint", "") or "").lower()
    return all(
        term in parsed["parts"]
        or any(term in part for part in parsed["parts"])
        or term_patterns[term].search(desc)
        or (hint and term_patterns[term].search(hint))
        for term in required_terms
    )


def _score_tool(
    tool: BaseTool,
    query_terms: list[str],
    term_patterns: dict[str, re.Pattern[str]],
) -> int:
    parsed = _parse_tool_name(tool.name)
    desc = (tool.description or "").lower()
    hint = (getattr(tool, "search_hint", "") or "").lower()
    score = 0
    for term in query_terms:
        pattern = term_patterns[term]
        name_matched = False
        if term in parsed["parts"]:
            score += 12 if parsed["is_mcp"] else 10
            name_matched = True
        elif any(term in part for part in parsed["parts"]):
            score += 6 if parsed["is_mcp"] else 5
            name_matched = True
        if not name_matched and term in parsed["full"]:
            score += 3
        if hint and pattern.search(hint):
            score += 4
        if pattern.search(desc):
            score += 2
    return score


def _parse_tool_name(name: str) -> dict[str, Any]:
    if name.startswith("mcp__"):
        without_prefix = name[5:].lower()
        parts: list[str] = []
        for segment in without_prefix.split("__"):
            parts.extend(segment.split("_"))
        parts = [part for part in parts if part]
        return {
            "parts": parts,
            "full": without_prefix.replace("__", " ").replace("_", " "),
            "is_mcp": True,
        }
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    spaced = spaced.replace("_", " ").lower()
    parts = [part for part in spaced.split() if part]
    return {"parts": parts, "full": " ".join(parts), "is_mcp": False}
