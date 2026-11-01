"""Conversation log formatting for execution analysis.

Converts ``conversations.jsonl`` entries into a priority-based text block
suitable for LLM analysis prompts.  All functions are pure (stateless).

Priority levels (lower = more important):
  0 — CRITICAL : User instruction (never truncated)
  1 — CRITICAL : Final iteration assistant response (never truncated)
  2 — HIGH     : Tool calls (name + args) AND tool errors — kept together
  3 — HIGH     : Non-final assistant reasoning; tool results with embedded summary
  4 — MEDIUM   : Tool success results (try to preserve)
  5 — LOW      : System guidance messages between iterations
  SKIP         : Skill injection text, verbose system prompts (not included;
                 skill & tool info are provided separately in the prompt)
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# Per-section truncation limits (kept in sync with analyzer constants)
TOOL_ERROR_MAX_CHARS = 1000
TOOL_SUCCESS_MAX_CHARS = 800
PERSISTED_TOOL_SUCCESS_MAX_CHARS = 400
TOOL_ARGS_MAX_CHARS = 500
TOOL_SUMMARY_MAX_CHARS = 1500
PERSISTED_PREVIEW_PREFIX = "Preview of persisted output:\n"


def format_conversations(
    conversations: List[Dict[str, Any]],
    budget: int,
) -> str:
    """Format ``conversations.jsonl`` entries into a readable text block.

    Uses priority-based truncation instead of simple tail-truncation.

    When total exceeds *budget*:
      1. Include all priority ≤ 3 (CRITICAL + HIGH) segments in full.
      2. Add MEDIUM + LOW segments until budget is exhausted, truncating
         if possible.
      3. If even HIGH content exceeds budget, keep priority 0-1 in full,
         budget-allocate priority 2, and summarize priority 3.
    """
    # Count total iterations for priority assignment
    total_iters = sum(
        1 for c in conversations if c.get("type") == "iteration"
    )

    # Phase 1: Collect all segments in chronological order with priority
    segments: List[Dict[str, Any]] = []

    for conv in conversations:
        conv_type = conv.get("type", "")
        if conv_type == "setup":
            _collect_setup_segments(conv, segments)
        elif conv_type == "iteration":
            _collect_iteration_segments(conv, total_iters, segments)

    # Phase 2: Assemble with budget management
    return _assemble_with_budget(segments, budget)

def _collect_setup_segments(
    conv: Dict[str, Any],
    segments: List[Dict[str, Any]],
) -> None:
    """Extract segments from a ``type: "setup"`` conversation entry.

    Only the user instruction is extracted.  System prompts (including skill
    injection text and tool descriptions) are skipped — they are provided in
    dedicated sections of the analysis prompt.
    """
    for msg in conv.get("messages", []):
        role = msg.get("role", "")
        content = msg.get("content", "")
        if not isinstance(content, str):
            content = str(content)

        if role == "user":
            segments.append({
                "priority": 0,  # CRITICAL — always keep
                "text": f"[USER INSTRUCTION]\n{content}",
                "iteration": 0,
                "role": "user",
                "truncatable_to": None,
            })

def _collect_iteration_segments(
    conv: Dict[str, Any],
    total_iters: int,
    segments: List[Dict[str, Any]],
) -> None:
    """Extract segments from a ``type: "iteration"`` conversation entry.

    Key design decisions:
      - Tool calls and tool errors share the SAME high priority (2)
      - Tool success results get MEDIUM priority (4)
      - Shell agent results with embedded "Execution Summary" get HIGH (3).
    """
    iteration = conv.get("iteration", "?")
    is_last = (iteration == total_iters) if isinstance(iteration, int) else False

    # Process delta_messages in order
    for msg in conv.get("delta_messages", []):
        role = msg.get("role", "")
        content = msg.get("content", "")
        if not isinstance(content, str):
            content = str(content)

        if role == "assistant":
            # Assistant reasoning
            if content:
                priority = 1 if is_last else 3
                segments.append({
                    "priority": priority,
                    "text": f"[Iter {iteration}] ASSISTANT: {content}",
                    "iteration": iteration,
                    "role": "assistant",
                    "truncatable_to": None,
                })

            # Tool calls
            for tc in msg.get("tool_calls", []):
                fn = tc.get("function", {})
                fn_name = fn.get("name", "?")
                fn_args = fn.get("arguments", "")
                if isinstance(fn_args, str) and len(fn_args) > TOOL_ARGS_MAX_CHARS:
                    fn_args = fn_args[:TOOL_ARGS_MAX_CHARS] + "..."
                segments.append({
                    "priority": 2,  # HIGH — paired with tool results/errors
                    "text": f"[Iter {iteration}] TOOL_CALL: {fn_name}({fn_args})",
                    "iteration": iteration,
                    "role": "tool_call",
                    "truncatable_to": None,
                })

        elif role == "tool":
            # Tool result
            evidence = _extract_tool_result_evidence(msg, content)
            evidence_label = _format_tool_result_label(evidence)
            display_content = _format_tool_result_content_for_prompt(
                content,
                evidence,
            )
            if _has_persisted_evidence(evidence):
                segments.append({
                    "priority": 2,
                    "text": (
                        f"[Iter {iteration}] TOOL_RESULT_EVIDENCE: "
                        f"{_format_tool_result_evidence(evidence)}"
                    ),
                    "iteration": iteration,
                    "role": "tool_result_evidence",
                    "truncatable_to": None,
                })
            is_error = _is_error_result(content)

            if is_error:
                truncated = display_content[:TOOL_ERROR_MAX_CHARS]
                if len(display_content) > TOOL_ERROR_MAX_CHARS:
                    truncated += (
                        f"... [truncated, total {len(display_content)} chars]"
                    )
                segments.append({
                    "priority": 2,  # HIGH — errors are critical, same tier as tool calls
                    "text": (
                        f"[Iter {iteration}] TOOL_ERROR{evidence_label}: "
                        f"{truncated}"
                    ),
                    "iteration": iteration,
                    "role": "tool_error",
                    "truncatable_to": None,
                })
            else:
                # Check if result contains a self-generated summary.
                summary = _extract_embedded_summary(display_content)
                if summary:
                    # Show the embedded summary (high value, compact)
                    segments.append({
                        "priority": 3,  # HIGH — self-generated summaries are informative
                        "text": (
                            f"[Iter {iteration}] TOOL_RESULT{evidence_label} "
                            f"(with summary):\n{summary}"
                        ),
                        "iteration": iteration,
                        "role": "tool_result",
                        "truncatable_to": 500,
                    })
                else:
                    max_chars = (
                        len(PERSISTED_PREVIEW_PREFIX)
                        + PERSISTED_TOOL_SUCCESS_MAX_CHARS
                        if _has_persisted_evidence(evidence)
                        else TOOL_SUCCESS_MAX_CHARS
                    )
                    truncated = display_content[:max_chars]
                    if len(display_content) > max_chars:
                        truncated += (
                            f"... [truncated, total {len(display_content)} chars]"
                        )
                    segments.append({
                        "priority": 4,  # MEDIUM — try to preserve success results
                        "text": (
                            f"[Iter {iteration}] TOOL_RESULT{evidence_label}: "
                            f"{truncated}"
                        ),
                        "iteration": iteration,
                        "role": "tool_result",
                        "truncatable_to": 300,
                    })

        elif role == "system":
            # System guidance between iterations (e.g. "Iteration N complete...")
            if content:
                segments.append({
                    "priority": 5,  # LOW — guidance messages
                    "text": f"[Iter {iteration}] SYSTEM: {content}",
                    "iteration": iteration,
                    "role": "system",
                    "truncatable_to": 150,
                })

def _assemble_with_budget(
    segments: List[Dict[str, Any]],
    budget: int,
) -> str:
    """Assemble segments into final text respecting the character budget.

    Strategy:
      1. Include all segments with priority ≤ 3 (CRITICAL + HIGH) in full.
      2. Add MEDIUM + LOW segments in chronological order until budget is hit.
      3. If even HIGH-priority content exceeds budget, progressively truncate
         older iterations while preserving user instruction and final iteration.
    """
    # Calculate essential (priority ≤ 3) size
    essential = [s for s in segments if s["priority"] <= 3]
    essential_chars = sum(len(s["text"]) for s in essential)

    remaining_budget = budget - essential_chars

    if remaining_budget < 0:
        # Essential content alone exceeds budget — need to reduce
        # Keep priority 0-1 (user instruction + final iteration) in full
        # Truncate priority 2-3 (tool calls/errors + older assistant content)
        return _assemble_essential_only(segments, budget)

    # Build output in chronological order
    output_parts: List[str] = []
    used_chars = 0
    skipped_count = 0

    for seg in segments:
        text = seg["text"]
        priority = seg["priority"]

        if priority <= 3:
            # Essential — always include
            output_parts.append(text)
            used_chars += len(text) + 1
        elif used_chars + len(text) + 1 <= budget:
            # Within budget — include
            output_parts.append(text)
            used_chars += len(text) + 1
        else:
            # Over budget — try truncation
            truncatable_to = seg.get("truncatable_to")
            if truncatable_to and len(text) > truncatable_to:
                truncated = text[:truncatable_to] + "... [budget-truncated]"
                if used_chars + len(truncated) + 1 <= budget:
                    output_parts.append(truncated)
                    used_chars += len(truncated) + 1
                    continue
            skipped_count += 1

    if skipped_count > 0:
        output_parts.append(
            f"\n[... {skipped_count} lower-priority segment(s) omitted due to length ...]"
        )

    return "\n\n".join(output_parts)


def _assemble_essential_only(
    segments: List[Dict[str, Any]],
    budget: int,
) -> str:
    """Fallback: even essential content exceeds budget.

    Keep:
      - User instruction (priority 0) — never truncated
      - Final iteration (priority 1) — never truncated
      - Tool calls + tool errors (priority 2) — budget-allocated, truncated if needed
      - Non-final assistant reasoning (priority 3) — heavily summarized
    """
    output_parts: List[str] = []
    used_chars = 0

    # Pass 1: priority 0 and 1 (user instruction + final iteration)
    for seg in segments:
        if seg["priority"] <= 1:
            output_parts.append(seg["text"])
            used_chars += len(seg["text"]) + 1

    remaining = budget - used_chars

    # Pass 2: priority 2 (tool calls + tool errors) — budget-allocated
    tool_segments = [s for s in segments if s["priority"] == 2]
    if tool_segments:
        per_segment_budget = max(400, remaining // (len(tool_segments) + 1))
        for seg in tool_segments:
            text = seg["text"]
            if len(text) > per_segment_budget:
                text = text[:per_segment_budget] + "... [budget-truncated]"
            if used_chars + len(text) + 1 <= budget:
                output_parts.append(text)
                used_chars += len(text) + 1

    # Pass 3: priority 3 (non-final assistant reasoning) — one-line summaries
    assistants = [s for s in segments if s["priority"] == 3]
    if assistants and used_chars < budget:
        output_parts.append("\n--- Older iteration summaries ---")
        for seg in assistants:
            first_line = seg["text"].split("\n", 1)[0][:200]
            if used_chars + len(first_line) + 1 > budget:
                output_parts.append("[... remaining iterations omitted ...]")
                break
            output_parts.append(first_line)
            used_chars += len(first_line) + 1

    return "\n\n".join(output_parts)

def _is_error_result(content: str) -> bool:
    """Detect if a tool result represents an error."""
    if not content:
        return False
    # Check common error patterns in the first 200 chars
    head = content[:200].lower()
    return (
        content.startswith("[ERROR]")
        or content.startswith("ERROR")
        or "error" in head[:50]
        or "task failed" in head
        or "connection refused" in head
        or "timed out" in head
        or "traceback" in head
    )


_PERSISTED_PATH_RE = re.compile(r"Full output saved to:\s*(.+)")
_ORIGINAL_LENGTH_RE = re.compile(r"Output too large \(([\d,]+) chars\)")
_PERSISTED_PREVIEW_RE = re.compile(
    r"Preview \(first ~[\d,]+ chars\):\n(.*?)(?:\n\.\.\.\n)?</persisted-output>",
    re.DOTALL,
)


def _extract_tool_result_evidence(
    msg: Dict[str, Any],
    content: str,
) -> Dict[str, Any]:
    """Return structured tool-result identity and persisted-output evidence."""
    meta = msg.get("_meta")
    if not isinstance(meta, dict):
        meta = {}
    tr_meta = meta.get("tool_result_metadata")
    if not isinstance(tr_meta, dict):
        tr_meta = {}

    evidence: Dict[str, Any] = {}
    tool_name = meta.get("tool_name") or msg.get("name") or tr_meta.get("tool")
    tool_call_id = (
        meta.get("tool_call_id")
        or msg.get("tool_call_id")
        or tr_meta.get("tool_call_id")
        or tr_meta.get("tool_use_id")
    )
    if tool_name:
        evidence["tool_name"] = str(tool_name)
    if tool_call_id:
        evidence["tool_call_id"] = str(tool_call_id)

    for key in (
        "persisted",
        "persisted_path",
        "original_length",
        "persist_error",
        "persisted_size",
        "content_type",
        "url",
    ):
        if key in tr_meta and tr_meta[key] is not None:
            evidence[key] = tr_meta[key]

    if "persisted_path" not in evidence:
        path_match = _PERSISTED_PATH_RE.search(content)
        if path_match:
            evidence["persisted_path"] = path_match.group(1).strip()
    if "original_length" not in evidence:
        length_match = _ORIGINAL_LENGTH_RE.search(content)
        if length_match:
            try:
                evidence["original_length"] = int(
                    length_match.group(1).replace(",", "")
                )
            except ValueError:
                evidence["original_length"] = length_match.group(1)
    if "persisted_path" in evidence and "persisted" not in evidence:
        evidence["persisted"] = True

    return evidence


def _has_persisted_evidence(evidence: Dict[str, Any]) -> bool:
    return any(
        key in evidence
        for key in ("persisted_path", "original_length", "persist_error")
    )


def _format_tool_result_label(evidence: Dict[str, Any]) -> str:
    parts = [
        f"{key}={evidence[key]}"
        for key in ("tool_name", "tool_call_id")
        if evidence.get(key)
    ]
    return f" [{' '.join(parts)}]" if parts else ""


def _format_tool_result_evidence(evidence: Dict[str, Any]) -> str:
    parts = []
    if evidence.get("tool_name"):
        parts.append(f"tool={evidence['tool_name']}")
    if evidence.get("tool_call_id"):
        parts.append(f"call_id={evidence['tool_call_id']}")
    if evidence.get("persisted_path"):
        parts.append(f"full_output={evidence['persisted_path']}")
    if evidence.get("persist_error"):
        parts.append(f"persist_error={evidence['persist_error']}")
    return "; ".join(parts) if parts else "(no structured evidence)"


def _format_tool_result_content_for_prompt(
    content: str,
    evidence: Dict[str, Any],
) -> str:
    """Avoid repeating persisted-output path/size inside the prompt body."""
    if not evidence.get("persisted_path"):
        return content
    preview_match = _PERSISTED_PREVIEW_RE.search(content)
    if not preview_match:
        return "[persisted output preview unavailable]"
    preview = preview_match.group(1).strip()
    if not preview:
        return "[persisted output preview empty]"
    return f"{PERSISTED_PREVIEW_PREFIX}{preview}"


def _extract_embedded_summary(content: str) -> Optional[str]:
    """Extract self-generated summary from tool result content.

    Shell agent results often contain an ``Execution Summary (N steps):``
    block that provides a compact view of what happened internally.
    This is more informative than the raw output.
    """
    # Look for "Execution Summary (N steps):" pattern
    match = re.search(
        r"(Execution Summary \(\d+ steps?\):.*?)(?:={10,}|$)",
        content,
        re.DOTALL,
    )
    if match:
        summary = match.group(1).strip()
        # Also capture any "Summary:" line after the steps
        summary_match = re.search(r"\nSummary:\s*(.+)", content)
        if summary_match:
            summary += f"\nConclusion: {summary_match.group(1).strip()}"
        return summary[:TOOL_SUMMARY_MAX_CHARS]

    return None
