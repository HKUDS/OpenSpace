"""Skill-context listing, discovery, and attachment helpers."""

from __future__ import annotations

import asyncio
import html
import json
import re
from typing import Any, Iterable

from openspace.services.conversation.attachments import create_attachment_message
from openspace.services.tooling.context import ToolUseContext
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)


def bind_skill_tools_to_context(
    tools: Iterable[Any],
    tool_use_context: ToolUseContext,
) -> None:
    if getattr(tool_use_context, "skills_disabled", False):
        return
    from openspace.skill_engine.protocol import (
        DISCOVER_SKILLS_TOOL_NAME,
        SKILL_TOOL_NAME,
        tool_matches_name as skill_tool_matches_name,
    )

    for tool in tools:
        if not (
            skill_tool_matches_name(tool, SKILL_TOOL_NAME)
            or skill_tool_matches_name(tool, DISCOVER_SKILLS_TOOL_NAME)
        ):
            continue
        setter = getattr(tool, "set_context", None)
        if callable(setter):
            setter(tool_use_context)


def append_skill_listing_delta(
    agent: Any,
    messages: list[dict[str, Any]],
    tool_use_context: ToolUseContext,
) -> None:
    if getattr(tool_use_context, "skills_disabled", False):
        return
    registry = getattr(agent, "_skill_registry", None)
    if not registry:
        return
    if not getattr(agent, "_skill_listing_enabled", True):
        return
    from openspace.skill_engine.protocol import build_skill_listing_messages

    listing_messages = build_skill_listing_messages(
        tool_use_context,
        registry=registry,
        tools=tool_use_context.tools,
        discovery_enabled=getattr(agent, "_skill_discovery_enabled", True),
        store=getattr(agent, "_skill_store", None),
        listing_budget_context_percent=getattr(
            agent,
            "_skill_listing_budget_context_percent",
            0.01,
        ),
        listing_max_description_chars=getattr(
            agent,
            "_skill_listing_max_description_chars",
            250,
        ),
    )
    if not listing_messages:
        return
    messages.extend(listing_messages)
    tool_use_context.replace_messages(messages)


def append_skill_discovery_delta(
    agent: Any,
    messages: list[dict[str, Any]],
    tool_use_context: ToolUseContext,
    *,
    query: str,
    source: str,
) -> None:
    if getattr(tool_use_context, "skills_disabled", False):
        return
    registry = getattr(agent, "_skill_registry", None)
    if not registry or not getattr(agent, "_skill_discovery_enabled", True):
        return
    if not str(query or "").strip():
        return
    if not has_skill_tool(tool_use_context.tools):
        return
    from openspace.skill_engine.protocol import build_skill_discovery_messages

    discovery_messages = build_skill_discovery_messages(
        tool_use_context,
        registry=registry,
        query=query,
        max_results=getattr(agent, "_skill_discovery_max_results", 5),
        store=getattr(agent, "_skill_store", None),
        source=source,
    )
    if not discovery_messages:
        return
    messages.extend(discovery_messages)
    tool_use_context.replace_messages(messages)


async def append_skill_discovery_delta_async(
    agent: Any,
    messages: list[dict[str, Any]],
    tool_use_context: ToolUseContext,
    *,
    query: str,
    source: str,
) -> None:
    if getattr(tool_use_context, "skills_disabled", False):
        return
    registry = getattr(agent, "_skill_registry", None)
    if not registry or not getattr(agent, "_skill_discovery_enabled", True):
        return
    if not str(query or "").strip():
        return
    if not has_skill_tool(tool_use_context.tools):
        return
    if source == "turn0_prefetch" and not getattr(
        agent,
        "_enable_turn0_llm_skill_selector",
        True,
    ):
        append_skill_discovery_delta(
            agent,
            messages,
            tool_use_context,
            query=query,
            source=source,
        )
        return
    from openspace.skill_engine.protocol import build_skill_discovery_messages_async

    discovery_messages = await build_skill_discovery_messages_async(
        tool_use_context,
        registry=registry,
        query=query,
        max_results=getattr(agent, "_skill_discovery_max_results", 5),
        store=getattr(agent, "_skill_store", None),
        source=source,
        llm_client=(
            getattr(agent, "_skill_selection_llm", None)
            or getattr(agent, "_tool_retrieval_llm", None)
            or getattr(agent, "_llm_client", None)
        ),
    )
    if not discovery_messages:
        return
    messages.extend(discovery_messages)
    tool_use_context.replace_messages(messages)


def has_skill_tool(tools: Iterable[Any]) -> bool:
    try:
        from openspace.skill_engine.protocol import (
            SKILL_TOOL_NAME,
            tool_matches_name as skill_tool_matches_name,
        )
    except Exception:
        return False
    return any(skill_tool_matches_name(tool, SKILL_TOOL_NAME) for tool in tools)


def has_discover_skills_tool(tools: Iterable[Any]) -> bool:
    try:
        from openspace.skill_engine.protocol import (
            DISCOVER_SKILLS_TOOL_NAME,
            tool_matches_name as skill_tool_matches_name,
        )
    except Exception:
        return False
    return any(
        skill_tool_matches_name(tool, DISCOVER_SKILLS_TOOL_NAME)
        for tool in tools
    )


def skill_discovery_query_from_recent_messages(
    instruction: str,
    messages: list[dict[str, Any]],
) -> str:
    parts: list[str] = [str(instruction or "").strip()]
    for message in reversed(messages[-8:]):
        if message.get("role") not in {"assistant", "tool", "user"}:
            continue
        content = message.get("content")
        if isinstance(content, str):
            text = content.strip()
        else:
            text = json.dumps(content, ensure_ascii=False, default=str)
        if text:
            parts.append(text[:800])
        if len(parts) >= 5:
            break
    return "\n\n".join(part for part in parts if part)[:4000]


async def build_post_tool_skill_discovery_query(
    agent: Any,
    instruction: str,
    messages: list[dict[str, Any]],
    *,
    abort_event: asyncio.Event | None = None,
) -> str:
    fallback_query = skill_discovery_query_from_recent_messages(
        instruction,
        messages,
    )
    if not getattr(agent, "_post_tool_query_builder_enabled", False):
        return fallback_query

    llm_client = (
        getattr(agent, "_skill_selection_llm", None)
        or getattr(agent, "_tool_retrieval_llm", None)
        or getattr(agent, "_llm_client", None)
    )
    if llm_client is None:
        return fallback_query

    max_chars = getattr(agent, "_post_tool_query_builder_max_chars", 4000)
    evidence = fallback_query[:max_chars]
    if not evidence.strip():
        return fallback_query
    escaped_evidence = html.escape(evidence, quote=False)

    prompt = (
        "You are writing a search query for retrieving reusable workflow instructions.\n\n"
        "Given recent task evidence, produce one concise search query. Focus on "
        "the user's goal, file or domain type, framework and tool names, concrete "
        "error classes, and the next likely workflow. Do not summarize the "
        "conversation. Do not include irrelevant logs, assistant narration, "
        "secrets, full paths, IDs, or large literals.\n\n"
        "Return XML only:\n"
        "<query>...</query>\n\n"
        "Query rules:\n"
        "- Write one English declarative sentence, 12-40 words.\n"
        "- Preserve exact technical tokens such as pytest, FastAPI, React, XLSX, ImportError, ffmpeg.\n"
        "- Prefer reusable workflow terms over project-specific names.\n"
        "- If evidence is too vague, use the original user goal as the query.\n"
        "- Do not mention skill, retrieval, or instructions in the query.\n\n"
        "Evidence:\n"
        f"<evidence>\n{escaped_evidence}\n</evidence>"
    )
    try:
        kwargs: dict[str, Any] = {"messages": [{"role": "user", "content": prompt}]}
        model = getattr(agent, "_post_tool_query_builder_model", None)
        if model:
            kwargs["model"] = model
        if abort_event is not None:
            kwargs["abort_event"] = abort_event
        response = await llm_client.call_model(**kwargs)
        content = str(response.assistant_message.get("content", "") or "").strip()
        query = ""
        xml_match = re.search(
            r"<query>\s*(.*?)\s*</query>",
            content,
            re.DOTALL | re.IGNORECASE,
        )
        if xml_match:
            query = html.unescape(xml_match.group(1)).strip()
        if not query:
            try:
                data = json.loads(content)
                if isinstance(data, dict):
                    query = str(data.get("query") or "").strip()
            except json.JSONDecodeError:
                match = re.search(r"\{.*\}", content, re.DOTALL)
                if match:
                    try:
                        data = json.loads(match.group())
                        if isinstance(data, dict):
                            query = str(data.get("query") or "").strip()
                    except json.JSONDecodeError:
                        query = ""
                if not query:
                    query = content.strip()
        return query[:1000] if query else fallback_query
    except Exception:
        logger.debug("Post-tool skill discovery query builder failed", exc_info=True)
        return fallback_query


def append_agent_listing_delta(
    messages: list[dict[str, Any]],
    tool_use_context: ToolUseContext,
) -> None:
    from openspace.services.conversation.attachments import get_agent_listing_delta_attachment

    attachments = get_agent_listing_delta_attachment(tool_use_context, messages)
    if not attachments:
        return
    messages.extend(create_attachment_message(attachment) for attachment in attachments)
    tool_use_context.replace_messages(messages)


def extract_skill_ids_from_messages(messages: list[dict[str, Any]]) -> list[str]:
    skill_ids: list[str] = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        meta = message.get("_meta")
        if not isinstance(meta, dict):
            continue
        attachment = meta.get("attachment")
        if isinstance(attachment, dict):
            attachment_type = attachment.get("type")
            if attachment_type == "invoked_skill_content":
                skill_id = str(attachment.get("skill_id") or "").strip()
                if skill_id:
                    skill_ids.append(skill_id)
            elif attachment_type == "invoked_skills":
                for item in attachment.get("skills") or []:
                    if isinstance(item, dict):
                        skill_id = str(item.get("skill_id") or "").strip()
                        if skill_id:
                            skill_ids.append(skill_id)
        result_meta = meta.get("tool_result_metadata")
        if isinstance(result_meta, dict) and result_meta.get("tool") == "Skill":
            skill_id = str(result_meta.get("skill_id") or "").strip()
            if skill_id:
                skill_ids.append(skill_id)
    return list(dict.fromkeys(skill_ids))


__all__ = [
    "append_agent_listing_delta",
    "append_skill_discovery_delta",
    "append_skill_discovery_delta_async",
    "append_skill_listing_delta",
    "bind_skill_tools_to_context",
    "build_post_tool_skill_discovery_query",
    "extract_skill_ids_from_messages",
    "has_discover_skills_tool",
    "has_skill_tool",
    "skill_discovery_query_from_recent_messages",
]
