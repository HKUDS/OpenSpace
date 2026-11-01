"""Tool result persistence — OpenSpace ``utils/toolResultStorage.ts`` equivalent.

When a tool result exceeds the per-tool ``max_result_size_chars`` threshold,
the full content is written to disk and the API-facing message is replaced
with a ``<persisted-output>`` wrapper containing a preview and the file path.
The model can then use ReadFile to retrieve the full content.

OpenSpace architecture note:
  In OpenSpace this logic lives in the *execution pipeline* (``toolExecution.ts``
  calls ``processPreMappedToolResultBlock`` after ``tool.call``).  In os the
  canonical call site is ``tool_runtime.pipeline.execution.run_tool_use()``, where the
  real tool_use_id is available for stable file names and metadata.

Per-message aggregate budget (OpenSpace ``enforceToolResultBudget`` +
``ContentReplacementState``) is implemented by ``enforce_tool_result_budget()``
and consumed by the agent loop after each tool turn.
"""
from __future__ import annotations

import math
import os
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from openspace.services.conversation.content_blocks import (
    content_has_multimodal_block,
    content_text_size,
    extract_text_from_content,
)
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants  (Implementation: constants/toolLimits.ts + toolResultStorage.ts)
# ---------------------------------------------------------------------------


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        logger.warning("Invalid %s=%r; using default %s", name, raw, default)
        return default

TOOL_RESULTS_SUBDIR = "tool-results"

PERSISTED_OUTPUT_TAG = "<persisted-output>"
PERSISTED_OUTPUT_CLOSING_TAG = "</persisted-output>"

PREVIEW_SIZE_CHARS: int = _env_int(
    "OPENSPACE_TOOL_RESULT_PREVIEW_CHARS",
    2_000,
    minimum=200,
)
"""OpenSpace ``PREVIEW_SIZE_BYTES = 2000``.  We use character count (OpenSpace uses byte
count, but since os deals in Python strings, chars is the natural unit)."""

TOOL_RESULT_CLEARED_MESSAGE = "[Old tool result content cleared]"

READ_TOOL_NAME = "read"

MAX_TOOL_RESULTS_PER_MESSAGE_CHARS: int = _env_int(
    "OPENSPACE_MAX_TOOL_RESULTS_PER_MESSAGE_CHARS",
    200_000,
    minimum=4_000,
)
"""OpenSpace ``MAX_TOOL_RESULTS_PER_MESSAGE_CHARS``.  Aggregate budget for all
tool_result blocks within one user message.  Consumed by the per-message
budget enforcement in the agent loop (step 7.1)."""

BUDGET_CLEAR_MESSAGE = TOOL_RESULT_CLEARED_MESSAGE


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class PersistedToolResult:
    """Successful persistence metadata."""
    filepath: str
    original_size: int
    preview: str
    has_more: bool


# ---------------------------------------------------------------------------
# Directory management
# ---------------------------------------------------------------------------

def get_results_dir(base_dir: Optional[str] = None) -> str:
    """Return (and lazily create) the tool-results directory.

    *base_dir* may be either the session directory (e.g.
    ``~/.openspace/sessions/<id>``) or the already-resolved
    ``tool-results`` directory.  When ``None``, falls back to a temp-directory
    path so callers without session context still work.
    """
    if base_dir:
        base_path = os.path.abspath(os.path.expanduser(base_dir))
        if os.path.basename(base_path) == TOOL_RESULTS_SUBDIR:
            d = base_path
        else:
            d = os.path.join(base_path, TOOL_RESULTS_SUBDIR)
    else:
        import tempfile
        d = os.path.join(tempfile.gettempdir(), "openspace", TOOL_RESULTS_SUBDIR)
    os.makedirs(d, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Preview generation  (Implementation: generatePreview)
# ---------------------------------------------------------------------------

def generate_preview(
    content: str,
    max_chars: int = PREVIEW_SIZE_CHARS,
) -> Tuple[str, bool]:
    """Generate a preview of *content*, truncating at a line boundary.

    Returns ``(preview_text, has_more)``.
    """
    if len(content) <= max_chars:
        return content, False

    truncated = content[:max_chars]
    last_nl = truncated.rfind("\n")
    cut_point = last_nl if last_nl > max_chars * 0.5 else max_chars
    return content[:cut_point], True


# ---------------------------------------------------------------------------
# Persist to disk  (Implementation: persistToolResult)
# ---------------------------------------------------------------------------

def persist_tool_result(
    content: str,
    tool_use_id: str,
    results_dir: Optional[str] = None,
) -> PersistedToolResult | str:
    """Write full tool result to disk.

    Returns a `PersistedToolResult` on success, or an error message string
    on failure.  Uses ``tool_use_id`` as the filename so repeated calls for
    the same invocation are idempotent (OpenSpace uses ``'wx'`` exclusive-create).
    """
    d = get_results_dir(results_dir)
    filepath = os.path.join(d, f"{tool_use_id}.txt")

    if not os.path.exists(filepath):
        try:
            fd = os.open(filepath, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            try:
                os.write(fd, content.encode("utf-8"))
            finally:
                os.close(fd)
            logger.debug("Persisted tool result to %s (%d chars)", filepath, len(content))
        except FileExistsError:
            pass  # already persisted on a prior turn
        except OSError as exc:
            return f"Failed to persist tool result: {exc}"

    preview, has_more = generate_preview(content)
    return PersistedToolResult(
        filepath=filepath,
        original_size=len(content),
        preview=preview,
        has_more=has_more,
    )


# ---------------------------------------------------------------------------
# Message builders  (Implementation: buildLargeToolResultMessage)
# ---------------------------------------------------------------------------

def build_persisted_output_message(result: PersistedToolResult) -> str:
    """Build the ``<persisted-output>`` wrapper sent to the model."""
    parts = [
        PERSISTED_OUTPUT_TAG,
        f"\nOutput too large ({result.original_size:,} chars). "
        f"Full output saved to: {result.filepath}\n",
        f"\nPreview (first ~{PREVIEW_SIZE_CHARS:,} chars):\n",
        result.preview,
        "\n...\n" if result.has_more else "\n",
        PERSISTED_OUTPUT_CLOSING_TAG,
    ]
    return "".join(parts)


# ---------------------------------------------------------------------------
# Threshold resolution  (Implementation: getPersistenceThreshold)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Per-message aggregate budget  (Implementation: enforceToolResultBudget)
# ---------------------------------------------------------------------------

def enforce_tool_result_budget(
    messages: list[dict],
    max_chars: int = MAX_TOOL_RESULTS_PER_MESSAGE_CHARS,
    results_dir: Optional[str] = None,
) -> list[dict]:
    """Enforce aggregate tool_result size budget across messages.

    Implementation: ``enforceToolResultBudget`` in ``toolResultStorage.ts``.

    Scans all ``role="tool"`` messages in *messages*.  If total content
    size exceeds *max_chars*, the largest results are replaced with
    persist-to-disk previews until the total is within budget.

    Operates **in-place** on the list and also returns it for convenience.
    """
    tool_msgs: list[tuple[int, dict]] = []
    total_chars = 0
    for i, msg in enumerate(messages):
        if msg.get("role") == "tool":
            content = msg.get("content", "")
            size = content_text_size(content)
            tool_msgs.append((i, msg))
            total_chars += size

    if total_chars <= max_chars:
        return messages

    # Sort by content size descending — replace largest first
    def _content_size(item: tuple[int, dict]) -> int:
        c = item[1].get("content", "")
        return content_text_size(c)

    tool_msgs.sort(key=_content_size, reverse=True)

    for idx, msg in tool_msgs:
        if total_chars <= max_chars:
            break
        content = msg.get("content", "")
        if content_has_multimodal_block(content):
            continue
        content_text = extract_text_from_content(content)
        clen = len(content_text)

        # Skip already-small results
        if clen < PREVIEW_SIZE_CHARS * 2:
            continue

        meta = msg.get("_meta") if isinstance(msg.get("_meta"), dict) else {}
        tool_use_id = (
            msg.get("tool_call_id")
            or meta.get("tool_call_id")
            or uuid.uuid4().hex
        )
        tool_name = msg.get("name") or meta.get("tool_name") or "unknown"
        if tool_name == READ_TOOL_NAME:
            continue

        result = persist_tool_result(
            content_text,
            tool_use_id,
            results_dir,
        )
        if isinstance(result, PersistedToolResult):
            replacement = build_persisted_output_message(result)
            msg["content"] = replacement
            meta = dict(meta)
            meta.setdefault("type", "tool_result")
            meta["tool_name"] = tool_name
            meta["tool_call_id"] = str(tool_use_id)
            tool_result_metadata = dict(meta.get("tool_result_metadata") or {})
            tool_result_metadata.update({
                "persisted": True,
                "persisted_path": result.filepath,
                "original_length": result.original_size,
                "persistence_source": "budget_enforcement",
            })
            meta["tool_result_metadata"] = tool_result_metadata
            msg["_meta"] = meta
            saved = clen - len(replacement)
            total_chars -= saved
            logger.debug(
                "Budget enforcement: replaced %s result (%d→%d chars, saved %d)",
                tool_name, clen, len(replacement), saved,
            )
        # else persist failed — skip this message

    return messages


def get_persistence_threshold(
    declared_max: float,
    system_default: Optional[float] = None,
) -> float:
    """Resolve the effective persistence threshold.

    OpenSpace logic: ``Infinity`` → pass-through (never persist); otherwise
    ``Math.min(declaredMax, DEFAULT_MAX_RESULT_SIZE_CHARS)``.

    The *system_default* parameter allows callers to inject a custom cap
    (OpenSpace uses a GrowthBook override per tool name; os passes
    ``DEFAULT_MAX_RESULT_SIZE_CHARS`` from ``base.py``).
    """
    if declared_max == math.inf:
        return math.inf
    if system_default is None:
        from openspace.grounding.core.tool.base import DEFAULT_MAX_RESULT_SIZE_CHARS
        system_default = DEFAULT_MAX_RESULT_SIZE_CHARS
    return min(declared_max, system_default)


# ---------------------------------------------------------------------------
# Main entry point  (Implementation: maybePersistLargeToolResult)
# ---------------------------------------------------------------------------

def maybe_persist_large_result(
    content: str,
    tool_use_id: Optional[str],
    tool_name: str,
    max_result_size_chars: float,
    results_dir: Optional[str] = None,
) -> Tuple[str, bool, Dict[str, Any]]:
    """Persist a tool result to disk if it exceeds the threshold.

    Returns ``(final_content, was_persisted, metadata)``.

    *tool_use_id*: the unique id of the tool_use block from the API response.
    Pipeline callers should pass the real id; ``None`` remains supported for
    lower-level helper tests and legacy direct callers.
    """
    threshold = get_persistence_threshold(max_result_size_chars)

    if threshold == math.inf or threshold <= 0:
        return content, False, {}
    if len(content) <= threshold:
        return content, False, {}

    effective_id = tool_use_id or uuid.uuid4().hex
    result = persist_tool_result(content, effective_id, results_dir)

    if isinstance(result, str):
        logger.warning("Tool result persistence failed for %s: %s", tool_name, result)
        return content, False, {"persist_error": result}

    message = build_persisted_output_message(result)
    return message, True, {
        "persisted": True,
        "persisted_path": result.filepath,
        "original_length": result.original_size,
        "persistence_source": "pipeline_large_result",
    }
