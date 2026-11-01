"""LLM-based conversation compaction (context compression).

This module estimates token usage, builds compact prompts, summarizes older
conversation turns, preserves required post-compact attachments, runs compact
hooks, and applies time-based cleanup of old tool results. Compact calls use a
plain text model request with thinking disabled and emit lifecycle events through
the runtime context when available.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from openspace.llm.types import (
    TokenUsage,
)
from openspace.services.conversation.messages import (
    annotate_boundary_with_preserved_segment,
    build_compact_boundary_message,
    build_compact_summary_message,
    extract_discovered_tool_names,
    get_assistant_message_text,
    get_message_uuid,
    get_messages_after_compact_boundary,
    ensure_message_uuid,
    is_compact_boundary_message,
    normalize_messages_for_api,
    strip_old_thinking_blocks,
)
from openspace.services.conversation.attachments import (
    create_post_compact_attachments,
)
from openspace.llm.thinking import ThinkingConfig

if TYPE_CHECKING:
    from openspace.llm.client import LLMClient
    from openspace.services.tooling.hooks import HookRegistry
    from openspace.services.tooling.context import ToolUseContext

logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════════════
# Constants
# ════════════════════════════════════════════════════════════════════════

AUTOCOMPACT_BUFFER_TOKENS: int = 13_000
"""Buffer between effective window and auto-compact trigger."""

WARNING_THRESHOLD_BUFFER_TOKENS: int = 20_000
ERROR_THRESHOLD_BUFFER_TOKENS: int = 20_000
MANUAL_COMPACT_BUFFER_TOKENS: int = 3_000

MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES: int = 3
"""Circuit breaker after consecutive auto-compact failures."""

COMPACT_MAX_OUTPUT_TOKENS: int = 20_000
"""Maximum output token budget for a compact call."""

MAX_OUTPUT_TOKENS_FOR_SUMMARY: int = 20_000
"""Reserved tokens for compact summary output."""

# Post-compact file restoration limits
POST_COMPACT_MAX_FILES_TO_RESTORE: int = 5
POST_COMPACT_TOKEN_BUDGET: int = 50_000
POST_COMPACT_MAX_TOKENS_PER_FILE: int = 5_000
POST_COMPACT_MAX_TOKENS_PER_SKILL: int = 5_000
POST_COMPACT_SKILLS_TOKEN_BUDGET: int = 25_000

# Default context window when model info unavailable
_DEFAULT_CONTEXT_WINDOW: int = 200_000
_DEFAULT_MAX_OUTPUT_TOKENS: int = 8_192

# Error messages
ERROR_MESSAGE_NOT_ENOUGH_MESSAGES: str = "Not enough messages to compact."
ERROR_MESSAGE_PROMPT_TOO_LONG: str = (
    "Conversation too long. Press esc twice to go up a few messages and try again."
)
ERROR_MESSAGE_USER_ABORT: str = "API Error: Request was aborted."
ERROR_MESSAGE_INCOMPLETE_RESPONSE: str = (
    "Compaction interrupted · This may be due to network issues — please try again."
)

IMAGE_MAX_TOKEN_SIZE: int = 2000
"""Fixed token estimate for image/document blocks."""


# ════════════════════════════════════════════════════════════════════════
# Token estimation
# ════════════════════════════════════════════════════════════════════════

def rough_token_estimation(content: str, bytes_per_token: int = 4) -> int:
    """Estimate tokens from UTF-8 byte length.

    Python ``len(str)`` returns Unicode code points, which underestimates CJK
    text. UTF-8 byte length gives a safer rough estimate.
    """
    if not content:
        return 0
    return round(len(content.encode("utf-8")) / bytes_per_token)


def rough_token_estimation_for_block(block: Any) -> int:
    """Estimate token usage for one message content block.

    Handles text, image, document, tool_result, tool_use, thinking, etc.
    """
    if isinstance(block, str):
        return rough_token_estimation(block)
    if not isinstance(block, dict):
        return rough_token_estimation(json.dumps(block, ensure_ascii=False))

    btype = block.get("type", "")

    if btype == "text":
        return rough_token_estimation(block.get("text", ""))
    if btype in ("image", "image_url", "document"):
        return IMAGE_MAX_TOKEN_SIZE
    if btype == "tool_result":
        content = block.get("content")
        if content is None:
            return 0
        return rough_token_estimation_for_content(content)
    if btype == "tool_use":
        name = block.get("name", "")
        inp = block.get("input", {})
        return rough_token_estimation(name + json.dumps(inp, ensure_ascii=False))
    if btype == "thinking":
        return rough_token_estimation(block.get("thinking", ""))
    if btype == "redacted_thinking":
        return rough_token_estimation(block.get("data", ""))

    return rough_token_estimation(json.dumps(block, ensure_ascii=False))


def rough_token_estimation_for_content(content: Any) -> int:
    """Estimate token usage for arbitrary message content."""
    if content is None:
        return 0
    if isinstance(content, str):
        return rough_token_estimation(content)
    if isinstance(content, list):
        return sum(rough_token_estimation_for_block(b) for b in content)
    return rough_token_estimation(json.dumps(content, ensure_ascii=False))


def rough_token_estimation_for_message(message: Mapping[str, Any]) -> int:
    """Estimate token usage for a single message."""
    role = message.get("role", "")
    if role in ("assistant", "user", "system", "tool"):
        return rough_token_estimation_for_content(message.get("content"))
    return 0


def rough_token_estimation_for_messages(
    messages: Sequence[Mapping[str, Any]],
) -> int:
    """Estimate token usage for a sequence of messages.

    This function returns the raw sum; callers apply any desired safety factor.
    """
    return sum(rough_token_estimation_for_message(m) for m in messages)


def estimate_message_tokens(
    messages: Sequence[Mapping[str, Any]],
    model: str | None = None,
) -> int:
    """Estimate message tokens using LiteLLM first, with rough fallback.

    The fallback path applies a 4/3 safety factor over the byte-length rough
    estimate.
    """
    if model:
        try:
            import litellm
            api_messages = normalize_messages_for_api(list(messages))
            count = litellm.token_counter(model=model, messages=api_messages)
            if isinstance(count, int) and count > 0:
                return count
        except Exception:
            pass

    raw = rough_token_estimation_for_messages(messages)
    return math.ceil(raw * 4 / 3)


def _get_usage_from_message(message: Mapping[str, Any]) -> dict[str, int] | None:
    """Extract usage dict from an assistant message's _meta."""
    if message.get("role") != "assistant":
        return None
    meta = message.get("_meta")
    if not isinstance(meta, Mapping):
        return None
    return meta.get("usage")


def token_count_with_estimation(
    messages: Sequence[Mapping[str, Any]],
) -> int:
    """Measure context size using latest API usage plus rough new-message estimates."""
    for i in range(len(messages) - 1, -1, -1):
        usage = _get_usage_from_message(messages[i])
        if usage is not None:
            total = (
                usage.get("input_tokens", 0)
                + usage.get("cache_creation_input_tokens", 0)
                + usage.get("cache_read_input_tokens", 0)
                + usage.get("output_tokens", 0)
            )
            new_messages = messages[i + 1:]
            return total + rough_token_estimation_for_messages(new_messages)
    return rough_token_estimation_for_messages(messages)


def token_count_from_last_api_response(
    messages: Sequence[Mapping[str, Any]],
) -> int:
    """Return total token usage from the latest assistant usage metadata."""
    for i in range(len(messages) - 1, -1, -1):
        usage = _get_usage_from_message(messages[i])
        if usage is not None:
            return (
                usage.get("input_tokens", 0)
                + usage.get("cache_creation_input_tokens", 0)
                + usage.get("cache_read_input_tokens", 0)
                + usage.get("output_tokens", 0)
            )
    return 0


def message_token_count_from_last_api_response(
    messages: Sequence[Mapping[str, Any]],
) -> int:
    """Return the output token count from the latest assistant usage metadata.

    Only output_tokens — NOT for threshold comparisons.
    """
    for i in range(len(messages) - 1, -1, -1):
        usage = _get_usage_from_message(messages[i])
        if usage is not None:
            return usage.get("output_tokens", 0)
    return 0


def does_most_recent_assistant_exceed_200k(
    messages: Sequence[Mapping[str, Any]],
) -> bool:
    """Return whether the latest assistant message reports over 200k tokens."""
    threshold = 200_000
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            usage = _get_usage_from_message(msg)
            if usage is None:
                return False
            total = (
                usage.get("input_tokens", 0)
                + usage.get("cache_creation_input_tokens", 0)
                + usage.get("cache_read_input_tokens", 0)
                + usage.get("output_tokens", 0)
            )
            return total > threshold
    return False


def get_assistant_message_content_length(message: Mapping[str, Any]) -> int:
    """Return the approximate content length for one assistant message."""
    content = message.get("content")
    if isinstance(content, str):
        return len(content)
    if not isinstance(content, list):
        return 0
    length = 0
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type", "")
        if btype == "text":
            length += len(block.get("text", ""))
        elif btype == "thinking":
            length += len(block.get("thinking", ""))
        elif btype == "redacted_thinking":
            length += len(block.get("data", ""))
        elif btype == "tool_use":
            length += len(json.dumps(block.get("input", {}), ensure_ascii=False))
    return length


# ════════════════════════════════════════════════════════════════════════
# Message grouping
# ════════════════════════════════════════════════════════════════════════

def group_messages_by_api_round(
    messages: Sequence[Mapping[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Group messages at API-round boundaries.

    A boundary fires when a new
    assistant response begins (different _meta.response_id from the prior
    assistant). For well-formed conversations this is an API-safe split point.

    Falls back to treating every assistant message as a new round when
    response_id metadata is absent.
    """
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    last_assistant_id: str | None = None

    for msg in messages:
        msg_dict = dict(msg)
        role = msg_dict.get("role", "")
        meta = msg_dict.get("_meta")
        response_id = (
            meta.get("response_id")
            if isinstance(meta, Mapping)
            else None
        )

        if role == "assistant":
            effective_id = response_id or id(msg)
            if last_assistant_id is not None and effective_id != last_assistant_id and current:
                groups.append(current)
                current = [msg_dict]
            else:
                current.append(msg_dict)
            last_assistant_id = effective_id
        else:
            current.append(msg_dict)

    if current:
        groups.append(current)
    return groups


# ════════════════════════════════════════════════════════════════════════
# Compact prompts
# ════════════════════════════════════════════════════════════════════════

NO_TOOLS_PREAMBLE: str = """\
CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.

- Do NOT use read, bash, grep, glob, edit, write, web_search, web_fetch, or ANY other tool.
- You already have all the context you need in the conversation above.
- Tool calls will be REJECTED and will waste your only turn — you will fail the task.
- Your entire response must be plain text: an <analysis> block followed by a <summary> block.

"""

_DETAILED_ANALYSIS_INSTRUCTION_BASE: str = """\
Before providing your final summary, wrap your analysis in <analysis> tags to organize your thoughts and ensure you've covered all necessary points. In your analysis process:

1. Chronologically analyze each message and section of the conversation. For each section thoroughly identify:
   - The user's explicit requests and intents
   - Your approach to addressing the user's requests
   - Key decisions, technical concepts and code patterns
   - Specific details like:
     - file names
     - full code snippets
     - function signatures
     - file edits
   - Errors that you ran into and how you fixed them
   - Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.
2. Double-check for technical accuracy and completeness, addressing each required element thoroughly."""

_DETAILED_ANALYSIS_INSTRUCTION_PARTIAL: str = """\
Before providing your final summary, wrap your analysis in <analysis> tags to organize your thoughts and ensure you've covered all necessary points. In your analysis process:

1. Analyze the recent messages chronologically. For each section thoroughly identify:
   - The user's explicit requests and intents
   - Your approach to addressing the user's requests
   - Key decisions, technical concepts and code patterns
   - Specific details like:
     - file names
     - full code snippets
     - function signatures
     - file edits
   - Errors that you ran into and how you fixed them
   - Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.
2. Double-check for technical accuracy and completeness, addressing each required element thoroughly."""

BASE_COMPACT_PROMPT: str = f"""\
Your task is to create a detailed summary of the conversation so far, paying close attention to the user's explicit requests and your previous actions.
This summary should be thorough in capturing technical details, code patterns, and architectural decisions that would be essential for continuing development work without losing context.

{_DETAILED_ANALYSIS_INSTRUCTION_BASE}

Your summary should include the following sections:

1. Primary Request and Intent: Capture all of the user's explicit requests and intents in detail
2. Key Technical Concepts: List all important technical concepts, technologies, and frameworks discussed.
3. Files and Code Sections: Enumerate specific files and code sections examined, modified, or created. Pay special attention to the most recent messages and include full code snippets where applicable and include a summary of why this file read or edit is important.
4. Errors and fixes: List all errors that you ran into, and how you fixed them. Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.
5. Problem Solving: Document problems solved and any ongoing troubleshooting efforts.
6. All user messages: List ALL user messages that are not tool results. These are critical for understanding the users' feedback and changing intent.
7. Pending Tasks: Outline any pending tasks that you have explicitly been asked to work on.
8. Current Work: Describe in detail precisely what was being worked on immediately before this summary request, paying special attention to the most recent messages from both user and assistant. Include file names and code snippets where applicable.
9. Optional Next Step: List the next step that you will take that is related to the most recent work you were doing. IMPORTANT: ensure that this step is DIRECTLY in line with the user's most recent explicit requests, and the task you were working on immediately before this summary request. If your last task was concluded, then only list next steps if they are explicitly in line with the users request. Do not start on tangential requests or really old requests that were already completed without confirming with the user first. If there is a next step, include direct quotes from the most recent conversation showing exactly what task you were working on and where you left off. This should be verbatim to ensure there's no drift in task interpretation.

Here's an example of how your output should be structured:

<example>
<analysis>
[Your thought process, ensuring all points are covered thoroughly and accurately]
</analysis>

<summary>
1. Primary Request and Intent:
   [Detailed description]

2. Key Technical Concepts:
   - [Concept 1]
   - [Concept 2]
   - [...]

3. Files and Code Sections:
   - [File Name 1]
      - [Summary of why this file is important]
      - [Summary of the changes made to this file, if any]
      - [Important Code Snippet]
   - [File Name 2]
      - [Important Code Snippet]
   - [...]

4. Errors and fixes:
    - [Detailed description of error 1]:
      - [How you fixed the error]
      - [User feedback on the error if any]
    - [...]

5. Problem Solving:
   [Description of solved problems and ongoing troubleshooting]

6. All user messages: 
    - [Detailed non tool use user message]
    - [...]

7. Pending Tasks:
   - [Task 1]
   - [Task 2]
   - [...]

8. Current Work:
   [Precise description of current work]

9. Optional Next Step:
   [Optional Next step to take]

</summary>
</example>

Please provide your summary based on the conversation so far, following this structure and ensuring precision and thoroughness in your response. 

There may be additional summarization instructions provided in the included context. If so, remember to follow these instructions when creating the above summary. Examples of instructions include:
<example>
## Compact Instructions
When summarizing the conversation focus on typescript code changes and also remember the mistakes you made and how you fixed them.
</example>

<example>
# Summary instructions
When you are using compact - please focus on test output and code changes. Include file reads verbatim.
</example>
"""

PARTIAL_COMPACT_PROMPT: str = f"""\
Your task is to create a detailed summary of the RECENT portion of the conversation — the messages that follow earlier retained context. The earlier messages are being kept intact and do NOT need to be summarized. Focus your summary on what was discussed, learned, and accomplished in the recent messages only.

{_DETAILED_ANALYSIS_INSTRUCTION_PARTIAL}

Your summary should include the following sections:

1. Primary Request and Intent: Capture the user's explicit requests and intents from the recent messages
2. Key Technical Concepts: List important technical concepts, technologies, and frameworks discussed recently.
3. Files and Code Sections: Enumerate specific files and code sections examined, modified, or created. Include full code snippets where applicable and include a summary of why this file read or edit is important.
4. Errors and fixes: List errors encountered and how they were fixed.
5. Problem Solving: Document problems solved and any ongoing troubleshooting efforts.
6. All user messages: List ALL user messages from the recent portion that are not tool results.
7. Pending Tasks: Outline any pending tasks from the recent messages.
8. Current Work: Describe precisely what was being worked on immediately before this summary request.
9. Optional Next Step: List the next step related to the most recent work. Include direct quotes from the most recent conversation.

Here's an example of how your output should be structured:

<example>
<analysis>
[Your thought process, ensuring all points are covered thoroughly and accurately]
</analysis>

<summary>
1. Primary Request and Intent:
   [Detailed description]

2. Key Technical Concepts:
   - [Concept 1]
   - [Concept 2]

3. Files and Code Sections:
   - [File Name 1]
      - [Summary of why this file is important]
      - [Important Code Snippet]

4. Errors and fixes:
    - [Error description]:
      - [How you fixed it]

5. Problem Solving:
   [Description]

6. All user messages:
    - [Detailed non tool use user message]

7. Pending Tasks:
   - [Task 1]

8. Current Work:
   [Precise description of current work]

9. Optional Next Step:
   [Optional Next step to take]

</summary>
</example>

Please provide your summary based on the RECENT messages only (after the retained earlier context), following this structure and ensuring precision and thoroughness in your response.
"""

PARTIAL_COMPACT_UP_TO_PROMPT: str = f"""\
Your task is to create a detailed summary of this conversation. This summary will be placed at the start of a continuing session; newer messages that build on this context will follow after your summary (you do not see them here). Summarize thoroughly so that someone reading only your summary and then the newer messages can fully understand what happened and continue the work.

{_DETAILED_ANALYSIS_INSTRUCTION_BASE}

Your summary should include the following sections:

1. Primary Request and Intent: Capture the user's explicit requests and intents in detail
2. Key Technical Concepts: List important technical concepts, technologies, and frameworks discussed.
3. Files and Code Sections: Enumerate specific files and code sections examined, modified, or created. Include full code snippets where applicable and include a summary of why this file read or edit is important.
4. Errors and fixes: List errors encountered and how they were fixed.
5. Problem Solving: Document problems solved and any ongoing troubleshooting efforts.
6. All user messages: List ALL user messages that are not tool results.
7. Pending Tasks: Outline any pending tasks.
8. Work Completed: Describe what was accomplished by the end of this portion.
9. Context for Continuing Work: Summarize any context, decisions, or state that would be needed to understand and continue the work in subsequent messages.

Here's an example of how your output should be structured:

<example>
<analysis>
[Your thought process, ensuring all points are covered thoroughly and accurately]
</analysis>

<summary>
1. Primary Request and Intent:
   [Detailed description]

2. Key Technical Concepts:
   - [Concept 1]
   - [Concept 2]

3. Files and Code Sections:
   - [File Name 1]
      - [Summary of why this file is important]
      - [Important Code Snippet]

4. Errors and fixes:
    - [Error description]:
      - [How you fixed it]

5. Problem Solving:
   [Description]

6. All user messages:
    - [Detailed non tool use user message]

7. Pending Tasks:
   - [Task 1]

8. Work Completed:
   [Description of what was accomplished]

9. Context for Continuing Work:
   [Key context, decisions, or state needed to continue the work]

</summary>
</example>

Please provide your summary following this structure, ensuring precision and thoroughness in your response.
"""

NO_TOOLS_TRAILER: str = (
    "\n\nREMINDER: Do NOT call any tools. Respond with plain text only — "
    "an <analysis> block followed by a <summary> block. "
    "Tool calls will be rejected and you will fail the task."
)


def get_compact_prompt(custom_instructions: str | None = None) -> str:
    """Build the full compact prompt."""
    prompt = NO_TOOLS_PREAMBLE + BASE_COMPACT_PROMPT
    if custom_instructions and custom_instructions.strip():
        prompt += f"\n\nAdditional Instructions:\n{custom_instructions}"
    prompt += NO_TOOLS_TRAILER
    return prompt


def get_partial_compact_prompt(
    custom_instructions: str | None = None,
    direction: str = "from",
) -> str:
    """Build the partial compact prompt."""
    template = (
        PARTIAL_COMPACT_UP_TO_PROMPT if direction == "up_to"
        else PARTIAL_COMPACT_PROMPT
    )
    prompt = NO_TOOLS_PREAMBLE + template
    if custom_instructions and custom_instructions.strip():
        prompt += f"\n\nAdditional Instructions:\n{custom_instructions}"
    prompt += NO_TOOLS_TRAILER
    return prompt


def format_compact_summary(summary: str) -> str:
    """Strip analysis scratchpad and replace summary tags with headers."""
    formatted = re.sub(r"<analysis>[\s\S]*?</analysis>", "", summary)

    match = re.search(r"<summary>([\s\S]*?)</summary>", formatted)
    if match:
        content = (match.group(1) or "").strip()
        formatted = re.sub(
            r"<summary>[\s\S]*?</summary>",
            lambda _match: f"Summary:\n{content}",
            formatted,
        )

    formatted = re.sub(r"\n\n+", "\n\n", formatted)
    return formatted.strip()


def get_compact_user_summary_message(
    summary: str,
    suppress_follow_up_questions: bool = False,
    transcript_path: str | None = None,
    recent_messages_preserved: bool = False,
) -> str:
    """Build the user-facing summary continuation message."""
    formatted = format_compact_summary(summary)

    base = (
        "This session is being continued from a previous conversation that "
        "ran out of context. The summary below covers the earlier portion "
        f"of the conversation.\n\n{formatted}"
    )

    if transcript_path:
        base += (
            "\n\nIf you need specific details from before compaction "
            "(like exact code snippets, error messages, or content you "
            f"generated), read the full transcript at: {transcript_path}"
        )

    if recent_messages_preserved:
        base += "\n\nRecent messages are preserved verbatim."

    if suppress_follow_up_questions:
        return (
            f"{base}\n"
            "Continue the conversation from where it left off without "
            "asking the user any further questions. Resume directly — "
            "do not acknowledge the summary, do not recap what was "
            'happening, do not preface with "I\'ll continue" or similar. '
            "Pick up the last task as if the break never happened."
        )
    return base


# ════════════════════════════════════════════════════════════════════════
# Data types
# ════════════════════════════════════════════════════════════════════════

@dataclass
class AutoCompactTracking:
    """Cross-iteration compact tracking. Lifecycle = one process() call."""
    compacted: bool = False
    turn_counter: int = 0
    turn_id: str = ""
    consecutive_failures: int = 0


@dataclass
class TokenWarningState:
    """Auto-compact threshold state for current token usage."""
    percent_left: int = 100
    is_above_warning_threshold: bool = False
    is_above_error_threshold: bool = False
    is_above_auto_compact_threshold: bool = False
    is_at_blocking_limit: bool = False


@dataclass
class CompactionResult:
    """Returned by compact_conversation / partial_compact_conversation.

    Consumed by agent loop to replace message list.
    """
    boundary_marker: dict[str, Any]
    summary_messages: list[dict[str, Any]]
    attachments: list[dict[str, Any]] = field(default_factory=list)
    hook_results: list[dict[str, Any]] = field(default_factory=list)
    messages_to_keep: list[dict[str, Any]] | None = None
    user_display_message: str | None = None
    pre_compact_token_count: int | None = None
    post_compact_token_count: int | None = None
    true_post_compact_token_count: int | None = None
    compaction_usage: TokenUsage | None = None
    compact_source: str = "llm_compact"
    compact_memory_path: str | None = None
    compact_was_truncated: bool = False


@dataclass
class RecompactionInfo:
    """Metadata describing an existing compact chain."""
    is_recompaction_in_chain: bool = False
    turns_since_previous_compact: int = -1
    previous_compact_turn_id: str | None = None
    auto_compact_threshold: int = 0
    query_source: str | None = None


@dataclass
class AutoCompactResult:
    """Return type of auto_compact_if_needed."""
    was_compacted: bool = False
    compaction_result: CompactionResult | None = None
    consecutive_failures: int = 0
    reason: str | None = None


# ════════════════════════════════════════════════════════════════════════
# Model context window helpers
# ════════════════════════════════════════════════════════════════════════

def _get_context_window_for_model(model: str) -> int:
    """Get context window size for a model via LiteLLM model info."""
    try:
        import litellm
        info = litellm.get_model_info(model)
        if info and "max_input_tokens" in info:
            return info["max_input_tokens"]
        if info and "max_tokens" in info:
            return info["max_tokens"]
    except Exception:
        pass

    env_override = os.environ.get("OPENSPACE_CONTEXT_WINDOW")
    if env_override:
        try:
            return int(env_override)
        except ValueError:
            pass
    return _DEFAULT_CONTEXT_WINDOW


def _get_max_output_tokens_for_model(model: str) -> int:
    """Get max output tokens for a model via LiteLLM model info."""
    try:
        import litellm
        info = litellm.get_model_info(model)
        if info and "max_output_tokens" in info:
            return info["max_output_tokens"]
    except Exception:
        pass
    return _DEFAULT_MAX_OUTPUT_TOKENS


# ════════════════════════════════════════════════════════════════════════
# Auto-compact threshold logic
# ════════════════════════════════════════════════════════════════════════

def get_effective_context_window_size(model: str) -> int:
    """Return context window minus reserved compact summary output tokens."""
    reserved = min(
        _get_max_output_tokens_for_model(model),
        MAX_OUTPUT_TOKENS_FOR_SUMMARY,
    )
    context_window = _get_context_window_for_model(model)

    env_cap = os.environ.get("OPENSPACE_AUTO_COMPACT_WINDOW")
    if env_cap:
        try:
            parsed = int(env_cap)
            if parsed > 0:
                context_window = min(context_window, parsed)
        except ValueError:
            pass

    return context_window - reserved


def get_auto_compact_threshold(model: str) -> int:
    """Return the auto-compact trigger threshold for a model."""
    effective = get_effective_context_window_size(model)
    threshold = effective - AUTOCOMPACT_BUFFER_TOKENS

    env_pct = os.environ.get("OPENSPACE_AUTOCOMPACT_PCT_OVERRIDE")
    if env_pct:
        try:
            parsed = float(env_pct)
            if 0 < parsed <= 100:
                pct_threshold = int(effective * parsed / 100)
                return min(pct_threshold, threshold)
        except ValueError:
            pass

    return threshold


def calculate_token_warning_state(
    token_usage: int,
    model: str,
) -> TokenWarningState:
    """Calculate warning/error/auto-compact threshold state."""
    auto_threshold = get_auto_compact_threshold(model)
    threshold = (
        auto_threshold if is_auto_compact_enabled()
        else get_effective_context_window_size(model)
    )

    percent_left = max(0, round(((threshold - token_usage) / threshold) * 100)) if threshold > 0 else 0

    warning_threshold = threshold - WARNING_THRESHOLD_BUFFER_TOKENS
    error_threshold = threshold - ERROR_THRESHOLD_BUFFER_TOKENS

    actual_window = get_effective_context_window_size(model)
    blocking_limit = actual_window - MANUAL_COMPACT_BUFFER_TOKENS

    env_override = os.environ.get("OPENSPACE_BLOCKING_LIMIT_OVERRIDE")
    if env_override:
        try:
            parsed = int(env_override)
            if parsed > 0:
                blocking_limit = parsed
        except ValueError:
            pass

    return TokenWarningState(
        percent_left=percent_left,
        is_above_warning_threshold=token_usage >= warning_threshold,
        is_above_error_threshold=token_usage >= error_threshold,
        is_above_auto_compact_threshold=(
            is_auto_compact_enabled() and token_usage >= auto_threshold
        ),
        is_at_blocking_limit=token_usage >= blocking_limit,
    )


def is_auto_compact_enabled(cwd: str | Path | None = None) -> bool:
    """Return whether auto-compact is enabled for the current workspace."""
    if os.environ.get("DISABLE_COMPACT", "").lower() in ("1", "true", "yes"):
        return False
    if os.environ.get("DISABLE_AUTO_COMPACT", "").lower() in ("1", "true", "yes"):
        return False
    try:
        from openspace.services.runtime_support.settings import get_setting

        return bool(get_setting("autoCompactEnabled", True, cwd=cwd))
    except Exception:
        return True


def should_auto_compact(
    messages: Sequence[Mapping[str, Any]],
    model: str,
) -> bool:
    """Return True when estimated token count exceeds auto-compact threshold."""
    if not is_auto_compact_enabled():
        return False

    token_count = token_count_with_estimation(messages)
    state = calculate_token_warning_state(token_count, model)

    logger.debug(
        "autocompact check: tokens=%d threshold=%d effective=%d above=%s",
        token_count,
        get_auto_compact_threshold(model),
        get_effective_context_window_size(model),
        state.is_above_auto_compact_threshold,
    )

    return state.is_above_auto_compact_threshold


# ════════════════════════════════════════════════════════════════════════
# Compact helpers
# ════════════════════════════════════════════════════════════════════════

def strip_images_from_messages(
    messages: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Replace image/document blocks with text markers before compaction."""
    result: list[dict[str, Any]] = []
    for msg in messages:
        msg_dict = dict(msg)
        if msg_dict.get("role") not in {"user", "tool"}:
            result.append(msg_dict)
            continue

        content = msg_dict.get("content")
        if not isinstance(content, list):
            result.append(msg_dict)
            continue

        has_media = False
        new_content: list[Any] = []
        for block in content:
            if not isinstance(block, dict):
                new_content.append(block)
                continue

            btype = block.get("type", "")
            if btype == "image" or btype == "image_url":
                has_media = True
                new_content.append({"type": "text", "text": "[image]"})
            elif btype == "document":
                has_media = True
                new_content.append({"type": "text", "text": "[document]"})
            elif btype == "tool_result" and isinstance(block.get("content"), list):
                tool_has_media = False
                new_tool_content = []
                for item in block["content"]:
                    if isinstance(item, dict) and item.get("type") in {"image", "image_url"}:
                        tool_has_media = True
                        new_tool_content.append({"type": "text", "text": "[image]"})
                    elif isinstance(item, dict) and item.get("type") == "document":
                        tool_has_media = True
                        new_tool_content.append({"type": "text", "text": "[document]"})
                    else:
                        new_tool_content.append(item)
                if tool_has_media:
                    has_media = True
                    new_content.append({**block, "content": new_tool_content})
                else:
                    new_content.append(block)
            else:
                new_content.append(block)

        if has_media:
            result.append({**msg_dict, "content": new_content})
        else:
            result.append(msg_dict)

    return result


def build_post_compact_messages(result: CompactionResult) -> list[dict[str, Any]]:
    """Build compact result messages in append order."""
    msgs: list[dict[str, Any]] = [result.boundary_marker]
    msgs.extend(result.summary_messages)
    if result.messages_to_keep:
        msgs.extend(result.messages_to_keep)
    msgs.extend(result.attachments)
    msgs.extend(result.hook_results)
    return msgs


def merge_hook_instructions(
    user_instructions: str | None,
    hook_instructions: str | None,
) -> str | None:
    """Merge user compact instructions with hook-provided instructions."""
    if not hook_instructions:
        return user_instructions or None
    if not user_instructions:
        return hook_instructions
    return f"{user_instructions}\n\n{hook_instructions}"


async def _emit_context_event(
    context: "ToolUseContext | None",
    event_type: str,
    data: dict[str, Any],
) -> None:
    """Emit a ToolUseContext event and await async emitters.

    Some unit tests pass a synchronous mock context.  Production
    ToolUseContext.emit_event is async, so compact flows must await it to
    avoid dropped events and coroutine-not-awaited warnings.
    """

    if context is None:
        return
    emit_event = getattr(context, "emit_event", None)
    if emit_event is None:
        return
    try:
        result = emit_event(event_type, data)
        if inspect.isawaitable(result):
            await result
    except Exception:
        logger.debug("compact event emission failed for %s", event_type, exc_info=True)


def _emit_context_event_sync(
    context: "ToolUseContext | None",
    event_type: str,
    data: dict[str, Any],
) -> None:
    """Best-effort event bridge for legacy synchronous compact helpers."""

    if context is None:
        return
    emit_event = getattr(context, "emit_event", None)
    if emit_event is None:
        return
    try:
        result = emit_event(event_type, data)
        if inspect.isawaitable(result):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(result)
            else:
                loop.create_task(result)
    except Exception:
        logger.debug("compact event emission failed for %s", event_type, exc_info=True)


def _ensure_messages_have_storage_uuids(
    messages: Sequence[Mapping[str, Any]],
) -> None:
    for message in messages:
        if isinstance(message, dict):
            ensure_message_uuid(message)


def _get_last_message_uuid(messages: Sequence[Mapping[str, Any]]) -> str | None:
    for message in reversed(messages):
        if isinstance(message, Mapping):
            uuid = get_message_uuid(message)
            if uuid:
                return uuid
    return None


async def _reappend_session_metadata(context: "ToolUseContext | None") -> None:
    storage = getattr(context, "session_storage", None) if context is not None else None
    if storage is None:
        return
    reappend = getattr(storage, "reappend_session_metadata", None)
    if reappend is None:
        return
    try:
        result = reappend()
        if inspect.isawaitable(result):
            await result
    except Exception:
        logger.debug("session metadata reappend failed during compact", exc_info=True)


async def _write_session_transcript_segment(
    context: "ToolUseContext | None",
    messages: Sequence[Mapping[str, Any]],
    *,
    reason: str,
) -> dict[str, Any] | None:
    storage = getattr(context, "session_storage", None) if context is not None else None
    if storage is None:
        return None
    write_segment = getattr(storage, "write_session_transcript_segment", None)
    if write_segment is None:
        return None
    try:
        result = write_segment(
            messages,
            reason=reason,
            task_id=getattr(context, "task_id", None),
            parent_task_id=getattr(context, "parent_task_id", None),
            agent_id=getattr(context, "agent_id", None),
        )
        if inspect.isawaitable(result):
            result = await result
        return dict(result) if isinstance(result, Mapping) else None
    except Exception:
        logger.debug("session transcript segment write failed during compact", exc_info=True)
        return None


async def _record_compact_summary_ref(
    context: "ToolUseContext | None",
    summary_message: Mapping[str, Any],
    *,
    compact_source: str,
    segment_data: Mapping[str, Any] | None,
    memory_path: str | None = None,
    was_truncated: bool = False,
) -> None:
    storage = getattr(context, "session_storage", None) if context is not None else None
    if storage is None:
        return
    record = getattr(storage, "record_compact_summary", None)
    if record is None:
        return
    segment_ref_id = None
    if isinstance(segment_data, Mapping):
        segment_id = segment_data.get("segment_id")
        if segment_id:
            segment_ref_id = f"transcript_segment:{storage.session_id}:{segment_id}"
    try:
        result = record(
            summary_message_uuid=get_message_uuid(summary_message),
            compact_source=compact_source,
            segment_ref_id=segment_ref_id,
            memory_path=memory_path,
            was_truncated=was_truncated,
            task_id=getattr(context, "task_id", None),
            parent_task_id=getattr(context, "parent_task_id", None),
            agent_id=getattr(context, "agent_id", None),
        )
        if inspect.isawaitable(result):
            await result
    except Exception:
        logger.debug("compact summary evidence write failed", exc_info=True)


# ════════════════════════════════════════════════════════════════════════
# Core compact functions
# ════════════════════════════════════════════════════════════════════════

async def compact_conversation(
    messages: list[dict[str, Any]],
    llm_client: "LLMClient",
    context: "ToolUseContext | None" = None,
    *,
    suppress_follow_up_questions: bool = True,
    custom_instructions: str | None = None,
    is_auto_compact: bool = False,
    recompaction_info: RecompactionInfo | None = None,
    hook_registry: "HookRegistry | None" = None,
    model: str | None = None,
    emit_lifecycle_events: bool = True,
) -> CompactionResult:
    """Full conversation compaction."""
    if not messages:
        raise ValueError(ERROR_MESSAGE_NOT_ENOUGH_MESSAGES)

    _ensure_messages_have_storage_uuids(messages)
    pre_compact_token_count = token_count_with_estimation(messages)

    # ── Emit progress event ──
    if emit_lifecycle_events:
        await _emit_context_event(context, "compact_start", {
            "trigger": "auto" if is_auto_compact else "manual",
            "pre_compact_token_count": pre_compact_token_count,
        })

    # ── PreCompact hooks ──
    from openspace.services.tooling.hooks import run_pre_compact_hooks
    hook_result = await run_pre_compact_hooks(
        hook_registry,
        {
            "trigger": "auto" if is_auto_compact else "manual",
            "custom_instructions": custom_instructions,
        },
        context,
    )
    custom_instructions = merge_hook_instructions(
        custom_instructions, hook_result.new_custom_instructions
    )
    user_display_message = hook_result.user_display_message

    # ── Build compact prompt ──
    compact_prompt = get_compact_prompt(custom_instructions)

    from openspace.llm.errors import PromptTooLongError

    try:
        summary = await _call_compact_model(
            messages,
            compact_prompt,
            llm_client,
            context,
            model=model,
        )
    except PromptTooLongError as ptl_err:
        raise RuntimeError(ERROR_MESSAGE_PROMPT_TOO_LONG) from ptl_err

    if summary is None:
        raise RuntimeError(
            "Failed to generate conversation summary — response did "
            "not contain valid text content"
        )

    # ── Build boundary, summary and post-compact attachments ──
    effective_model = model or getattr(llm_client, "model", None)
    pre_compact_discovered = extract_discovered_tool_names(messages)
    if context is not None:
        pre_compact_discovered.update(getattr(context, "discovered_tool_names", set()) or set())
    boundary = build_compact_boundary_message(
        "auto" if is_auto_compact else "manual",
        pre_compact_token_count,
        last_pre_compact_message_uuid=_get_last_message_uuid(messages),
        pre_compact_discovered_tools=sorted(pre_compact_discovered),
    )
    summary_msg = build_compact_summary_message(
        get_compact_user_summary_message(
            summary, suppress_follow_up_questions
        ),
        visible_in_transcript_only=True,
    )
    attachments: list[dict[str, Any]] = []
    if context is not None:
        attachments = await create_post_compact_attachments(
            context,
            effective_model=effective_model,
            messages_to_keep=[],
            full_compact=True,
        )

    true_post_compact = rough_token_estimation_for_messages(
        [boundary, summary_msg, *attachments]
    )

    # ── PostCompact hooks ──
    from openspace.services.tooling.hooks import run_post_compact_hooks
    post_hook = await run_post_compact_hooks(
        hook_registry,
        {
            "trigger": "auto" if is_auto_compact else "manual",
            "compact_summary": summary,
        },
        context,
    )
    combined_display = "\n".join(
        m for m in [user_display_message, post_hook.user_display_message] if m
    ) or None

    await _reappend_session_metadata(context)
    segment_data = await _write_session_transcript_segment(
        context,
        messages,
        reason="compact",
    )
    await _record_compact_summary_ref(
        context,
        summary_msg,
        compact_source="llm_compact",
        segment_data=segment_data,
    )

    # ── Emit completion event ──
    if emit_lifecycle_events:
        await _emit_context_event(context, "compact_complete", {
            "trigger": "auto" if is_auto_compact else "manual",
            "pre_compact_token_count": pre_compact_token_count,
            "true_post_compact_token_count": true_post_compact,
        })

    return CompactionResult(
        boundary_marker=boundary,
        summary_messages=[summary_msg],
        attachments=attachments,
        user_display_message=combined_display,
        pre_compact_token_count=pre_compact_token_count,
        true_post_compact_token_count=true_post_compact,
        compact_source="llm_compact",
    )


async def partial_compact_conversation(
    all_messages: list[dict[str, Any]],
    pivot_index: int,
    llm_client: "LLMClient",
    context: "ToolUseContext | None" = None,
    *,
    user_feedback: str | None = None,
    direction: str = "from",
    hook_registry: "HookRegistry | None" = None,
    model: str | None = None,
) -> CompactionResult:
    """Partial compaction around a pivot index.

    direction='from': summarizes messages[pivot_index:], keeps [:pivot_index].
    direction='up_to': summarizes messages[:pivot_index], keeps [pivot_index:].
    """
    _ensure_messages_have_storage_uuids(all_messages)
    if direction == "up_to":
        to_summarize = all_messages[:pivot_index]
        to_keep = [
            m for m in all_messages[pivot_index:]
            if not is_compact_boundary_message(m)
        ]
    else:
        to_summarize = all_messages[pivot_index:]
        to_keep = [
            m for m in all_messages[:pivot_index]
            if m.get("_meta", {}).get("type") != "progress"
        ]

    if not to_summarize:
        raise ValueError(
            "Nothing to summarize before the selected message."
            if direction == "up_to"
            else "Nothing to summarize after the selected message."
        )

    pre_compact_token_count = token_count_with_estimation(all_messages)

    await _emit_context_event(context, "compact_start", {
        "trigger": "manual",
        "direction": direction,
        "pre_compact_token_count": pre_compact_token_count,
    })

    # ── PreCompact hooks ──
    from openspace.services.tooling.hooks import run_pre_compact_hooks
    hook_result = await run_pre_compact_hooks(
        hook_registry,
        {"trigger": "manual", "custom_instructions": None},
        context,
    )

    custom_instructions: str | None = None
    if hook_result.new_custom_instructions and user_feedback:
        custom_instructions = f"{hook_result.new_custom_instructions}\n\nUser context: {user_feedback}"
    elif hook_result.new_custom_instructions:
        custom_instructions = hook_result.new_custom_instructions
    elif user_feedback:
        custom_instructions = f"User context: {user_feedback}"

    compact_prompt = get_partial_compact_prompt(custom_instructions, direction)

    api_messages = to_summarize if direction == "up_to" else all_messages
    from openspace.llm.errors import PromptTooLongError

    try:
        summary = await _call_compact_model(
            api_messages,
            compact_prompt,
            llm_client,
            context,
            model=model,
        )
    except PromptTooLongError as ptl_err:
        raise RuntimeError(ERROR_MESSAGE_PROMPT_TOO_LONG) from ptl_err

    if summary is None:
        raise RuntimeError(
            "Failed to generate conversation summary — response did "
            "not contain valid text content"
        )

    effective_model = model or getattr(llm_client, "model", None)
    pre_compact_discovered = extract_discovered_tool_names(all_messages)
    if context is not None:
        pre_compact_discovered.update(getattr(context, "discovered_tool_names", set()) or set())
    boundary = build_compact_boundary_message(
        "manual",
        pre_compact_token_count,
        last_pre_compact_message_uuid=_get_last_message_uuid(all_messages),
        messages_summarized=len(to_summarize),
        pre_compact_discovered_tools=sorted(pre_compact_discovered),
    )
    summary_msg = build_compact_summary_message(
        get_compact_user_summary_message(summary, False),
        messages_summarized=len(to_summarize),
        direction=direction,
        visible_in_transcript_only=len(to_keep) == 0,
    )
    attachments: list[dict[str, Any]] = []
    if context is not None:
        attachments = await create_post_compact_attachments(
            context,
            effective_model=effective_model,
            messages_to_keep=to_keep,
            full_compact=False,
        )

    from openspace.services.tooling.hooks import run_post_compact_hooks
    post_hook = await run_post_compact_hooks(
        hook_registry,
        {"trigger": "manual", "compact_summary": summary},
        context,
    )

    await _reappend_session_metadata(context)
    segment_data = await _write_session_transcript_segment(
        context,
        to_summarize,
        reason="partial_compact",
    )
    await _record_compact_summary_ref(
        context,
        summary_msg,
        compact_source="llm_compact",
        segment_data=segment_data,
    )

    if to_keep:
        anchor_uuid = (
            get_message_uuid(summary_msg) or get_message_uuid(boundary)
            if direction == "up_to"
            else get_message_uuid(boundary)
        )
        if anchor_uuid:
            boundary = annotate_boundary_with_preserved_segment(
                boundary,
                anchor_uuid,
                to_keep,
            )

    await _emit_context_event(context, "compact_complete", {
        "trigger": "manual",
        "direction": direction,
        "pre_compact_token_count": pre_compact_token_count,
    })

    return CompactionResult(
        boundary_marker=boundary,
        summary_messages=[summary_msg],
        attachments=attachments,
        messages_to_keep=to_keep if to_keep else None,
        user_display_message=post_hook.user_display_message,
        pre_compact_token_count=pre_compact_token_count,
        compact_source="llm_compact",
    )


async def auto_compact_if_needed(
    messages: list[dict[str, Any]],
    llm_client: "LLMClient",
    context: "ToolUseContext | None" = None,
    *,
    model: str | None = None,
    tracking: AutoCompactTracking | None = None,
    hook_registry: "HookRegistry | None" = None,
) -> AutoCompactResult:
    """Check and automatically execute compact when needed."""
    if not is_auto_compact_enabled(getattr(context, "cwd", None)):
        return AutoCompactResult(was_compacted=False, reason="disabled")

    if tracking is None:
        tracking = AutoCompactTracking()

    if tracking.consecutive_failures >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES:
        logger.debug(
            "autocompact: circuit breaker active (%d failures)",
            tracking.consecutive_failures,
        )
        return AutoCompactResult(
            was_compacted=False,
            reason="circuit_breaker",
            consecutive_failures=tracking.consecutive_failures,
        )

    effective_model = model or getattr(llm_client, "model", "") or ""
    if not should_auto_compact(messages, effective_model):
        return AutoCompactResult(was_compacted=False, reason="below_threshold")

    recompaction_info = RecompactionInfo(
        is_recompaction_in_chain=tracking.compacted,
        turns_since_previous_compact=tracking.turn_counter,
        previous_compact_turn_id=tracking.turn_id or None,
        auto_compact_threshold=get_auto_compact_threshold(effective_model),
    )

    try:
        session_memory_result = None
        try:
            from openspace.services.session.compact_memory import (
                try_session_memory_compaction,
            )

            session_memory_result = await try_session_memory_compaction(
                messages,
                context,
                auto_compact_threshold=recompaction_info.auto_compact_threshold,
                hook_registry=hook_registry,
                model=effective_model,
            )
        except Exception as e:
            logger.debug("Session memory auto compact skipped: %s", e, exc_info=True)

        if session_memory_result is not None:
            return AutoCompactResult(
                was_compacted=True,
                compaction_result=session_memory_result,
                consecutive_failures=0,
            )

        result = await compact_conversation(
            messages,
            llm_client,
            context,
            suppress_follow_up_questions=True,
            is_auto_compact=True,
            recompaction_info=recompaction_info,
            hook_registry=hook_registry,
            model=effective_model,
        )
        try:
            from openspace.services.memory.session_memory import (
                set_last_summarized_message_id,
            )

            if context is not None:
                set_last_summarized_message_id(context, None)
        except Exception:
            pass
        return AutoCompactResult(
            was_compacted=True,
            compaction_result=result,
            consecutive_failures=0,
        )
    except Exception as e:
        if str(e) != ERROR_MESSAGE_USER_ABORT:
            logger.warning("Auto compact failed: %s", e)
        prev = tracking.consecutive_failures
        next_failures = prev + 1
        if next_failures >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES:
            logger.warning(
                "autocompact: circuit breaker tripped after %d failures",
                next_failures,
            )
        return AutoCompactResult(
            was_compacted=False,
            reason="error",
            consecutive_failures=next_failures,
        )


# ════════════════════════════════════════════════════════════════════════
# Internal LLM call for compact summary
# ════════════════════════════════════════════════════════════════════════

COMPACT_MODEL_ENV_KEY = "OPENSPACE_COMPACT_MODEL"


async def _call_compact_model(
    messages: list[dict[str, Any]],
    compact_prompt: str,
    llm_client: "LLMClient",
    context: "ToolUseContext | None",
    *,
    model: str | None = None,
) -> str | None:
    """Call LLM to generate a compact summary.

    **Model selection**:

    1. ``OPENSPACE_COMPACT_MODEL`` env var  (e.g. ``deepseek/deepseek-chat``)
    2. Fallback: main-loop model via ``llm_client.model``

    Compact is a pure text-summarisation task (tools=None, thinking disabled),
    so a cheaper / faster model is perfectly adequate.

    Key: thinking is disabled for compact calls.
    """
    api_messages = normalize_messages_for_api(
        strip_images_from_messages(
            strip_old_thinking_blocks(
                get_messages_after_compact_boundary(messages),
                keep_recent=0,
            )
        ),
        strip_thinking_keep_recent=0,
    )
    api_messages.append({"role": "user", "content": compact_prompt})

    compact_model = os.environ.get(COMPACT_MODEL_ENV_KEY, "").strip()
    effective_model = compact_model or model or getattr(llm_client, "model", "") or ""

    if compact_model and compact_model != getattr(llm_client, "model", ""):
        from openspace.llm.client import LLMClient as _LLMClient
        compact_client = _LLMClient(
            model=compact_model,
            enable_thinking=False,
            fallback_model=getattr(llm_client, "fallback_model", None),
        )
        call_model = getattr(
            compact_client,
            "call_model_with_fallback",
            compact_client.call_model,
        )
        response = await call_model(
            messages=api_messages,
            tools=None,
            model=effective_model,
            max_tokens=min(
                COMPACT_MAX_OUTPUT_TOKENS,
                _get_max_output_tokens_for_model(effective_model),
            ),
            reasoning_effort=None,
            thinking_config=ThinkingConfig.disabled(source="compact"),
            strip_thinking_keep_recent=0,
        )
    else:
        call_model = getattr(llm_client, "call_model_with_fallback", llm_client.call_model)
        response = await call_model(
            messages=api_messages,
            tools=None,
            model=effective_model,
            max_tokens=min(
                COMPACT_MAX_OUTPUT_TOKENS,
                _get_max_output_tokens_for_model(effective_model),
            ),
            reasoning_effort=None,
            thinking_config=ThinkingConfig.disabled(source="compact"),
            strip_thinking_keep_recent=0,
        )

    text = get_assistant_message_text(response.assistant_message)
    return text


# ════════════════════════════════════════════════════════════════════════
# Post-compact cleanup
# ════════════════════════════════════════════════════════════════════════

def run_post_compact_cleanup(
    context: "ToolUseContext | None" = None,
) -> None:
    """Reset compact-sensitive state.

    Callers: autoCompactIfNeeded (auto),
    /compact command (manual), agent loop reactive compact.

    Clears compact-sensitive prompt caches so dynamic context (environment,
    git status, OPENSPACE.md) is regenerated on the next model call.
    """
    try:
        from openspace.prompts.grounding_agent_prompts import (
            clear_system_prompt_sections,
        )

        clear_system_prompt_sections()
    except Exception:
        logger.debug("failed to clear system prompt section cache", exc_info=True)
    logger.debug("post-compact cleanup complete")


# ════════════════════════════════════════════════════════════════════════
# Time-based microcompact
#
# Lightweight pre-processing layer that content-clears old tool results
# when the gap since the last assistant message exceeds a threshold.
# Runs BEFORE auto_compact_if_needed — pure rule-based, no LLM call.
#
# ════════════════════════════════════════════════════════════════════════

from openspace.tool_runtime.pipeline.execution import (
    BASH_TOOL_NAME,
    FILE_EDIT_TOOL_NAME,
    FILE_READ_TOOL_NAME,
    FILE_WRITE_TOOL_NAME,
    GLOB_TOOL_NAME,
    GREP_TOOL_NAME,
    WEB_FETCH_TOOL_NAME,
    WEB_SEARCH_TOOL_NAME,
)

TIME_BASED_MC_CLEARED_MESSAGE: str = "[Old tool result content cleared]"

# Compactable shell tools available in this runtime.
COMPACTABLE_TOOLS: frozenset[str] = frozenset({
    FILE_READ_TOOL_NAME,   # 'read'
    BASH_TOOL_NAME,        # 'bash'
    GREP_TOOL_NAME,        # 'grep'
    GLOB_TOOL_NAME,        # 'glob'
    WEB_SEARCH_TOOL_NAME,  # 'web_search'
    WEB_FETCH_TOOL_NAME,   # 'web_fetch'
    FILE_EDIT_TOOL_NAME,   # 'edit'
    FILE_WRITE_TOOL_NAME,  # 'write'
})


@dataclass
class TimeBasedMCConfig:
    """Configuration for time-based microcompact."""
    enabled: bool = True
    gap_threshold_minutes: float = 5.0
    keep_recent: int = 3


# Can be swapped via ``set_time_based_mc_config()`` for testing.
_time_based_mc_config = TimeBasedMCConfig()


def get_time_based_mc_config() -> TimeBasedMCConfig:
    """Return the module-level singleton."""
    return _time_based_mc_config


def set_time_based_mc_config(config: TimeBasedMCConfig) -> None:
    """Test helper — swap the singleton config."""
    global _time_based_mc_config
    _time_based_mc_config = config


def _is_main_thread_source(query_source: str | None) -> bool:
    """Return whether the source represents the primary agent loop."""
    return query_source is not None and query_source.startswith("main_thread")


def collect_compactable_tool_ids(
    messages: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Collect compactable tool_use IDs in encounter order."""
    ids: list[str] = []
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        tool_calls = msg.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for tc in tool_calls:
            func = tc.get("function") if isinstance(tc, dict) else None
            if func is None:
                continue
            name = func.get("name", "")
            if name in COMPACTABLE_TOOLS:
                tc_id = tc.get("id")
                if tc_id:
                    ids.append(tc_id)
    return ids


@dataclass
class TimeBasedMCResult:
    """Return type of time_based_microcompact."""
    messages: list[dict[str, Any]]
    was_cleared: bool = False
    gap_minutes: float = 0.0
    tools_cleared: int = 0
    tools_kept: int = 0
    tokens_saved: int = 0
    event_data: dict[str, Any] | None = None


def evaluate_time_based_trigger(
    messages: Sequence[Mapping[str, Any]],
    query_source: str | None,
) -> tuple[float, TimeBasedMCConfig] | None:
    """Return (gap_minutes, config) when the time-based trigger fires."""
    config = get_time_based_mc_config()
    if not config.enabled:
        return None
    if not query_source or not _is_main_thread_source(query_source):
        return None

    last_assistant_ts: float | None = None
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            meta = msg.get("_meta")
            if isinstance(meta, Mapping):
                ts = meta.get("timestamp")
                if isinstance(ts, (int, float)):
                    last_assistant_ts = float(ts)
            break

    if last_assistant_ts is None:
        return None

    gap_minutes = (time.time() - last_assistant_ts) / 60.0
    if not math.isfinite(gap_minutes) or gap_minutes < config.gap_threshold_minutes:
        return None

    return (gap_minutes, config)


def time_based_microcompact(
    messages: list[dict[str, Any]],
    context: "ToolUseContext | None" = None,
    *,
    query_source: str | None = None,
) -> TimeBasedMCResult:
    """Clear old compactable tool results after a long assistant gap.

    When the gap since the last assistant message exceeds the configured
    threshold, content-clear all but the most recent N compactable tool
    results.

    Returns TimeBasedMCResult with was_cleared=False when nothing changed
    (disabled, wrong source, gap under threshold, nothing to clear).
    """
    trigger = evaluate_time_based_trigger(messages, query_source)
    if trigger is None:
        return TimeBasedMCResult(messages=messages)

    gap_minutes, config = trigger

    compactable_ids = collect_compactable_tool_ids(messages)

    # Keep at least one recent tool result.
    keep_recent = max(1, config.keep_recent)
    keep_set = set(compactable_ids[-keep_recent:])
    clear_set = set(id_ for id_ in compactable_ids if id_ not in keep_set)

    if not clear_set:
        return TimeBasedMCResult(messages=messages)

    # Walk messages and replace content of cleared tool results.
    tokens_saved = 0
    result: list[dict[str, Any]] = []
    for msg in messages:
        if (
            msg.get("role") == "tool"
            and msg.get("tool_call_id") in clear_set
            and msg.get("content") != TIME_BASED_MC_CLEARED_MESSAGE
        ):
            old_content = msg.get("content", "")
            if isinstance(old_content, str):
                tokens_saved += rough_token_estimation(old_content)
            else:
                tokens_saved += rough_token_estimation(
                    json.dumps(old_content, ensure_ascii=False)
                )
            result.append({**msg, "content": TIME_BASED_MC_CLEARED_MESSAGE})
        else:
            result.append(msg)

    if tokens_saved == 0:
        return TimeBasedMCResult(messages=messages)

    # Emit a lifecycle event when a context is available.
    event_data = {
        "gap_minutes": round(gap_minutes),
        "gap_threshold_minutes": config.gap_threshold_minutes,
        "tools_cleared": len(clear_set),
        "tools_kept": len(keep_set),
        "keep_recent": config.keep_recent,
        "tokens_saved": tokens_saved,
    }
    if context is not None:
        _emit_context_event_sync(context, "time_based_microcompact", event_data)

    logger.info(
        "[TIME-BASED MC] gap %dmin > %.0fmin, cleared %d tool results "
        "(~%d tokens), kept last %d",
        round(gap_minutes),
        config.gap_threshold_minutes,
        len(clear_set),
        tokens_saved,
        len(keep_set),
    )

    return TimeBasedMCResult(
        messages=result,
        was_cleared=True,
        gap_minutes=gap_minutes,
        tools_cleared=len(clear_set),
        tools_kept=len(keep_set),
        tokens_saved=tokens_saved,
        event_data=event_data,
    )
