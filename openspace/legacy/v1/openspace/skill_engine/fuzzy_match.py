"""Fuzzy matching chain for SEARCH/REPLACE edits.

The chain degrades gracefully:
  Level 1 — exact match
  Level 2 — line-trimmed match (per-line strip)
  Level 3 — block-anchor match (first/last line + Levenshtein middle)
  Level 4 — whitespace-normalized match (collapse whitespace)
  Level 5 — indentation-flexible match (strip common indent)
  Level 6 — trimmed-boundary match (strip entire block)
"""

from __future__ import annotations

import re
from typing import Generator, List, Optional, Tuple

from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

__all__ = [
    "fuzzy_find_match",
    "fuzzy_replace",
    "REPLACER_CHAIN",
]

# Type alias — each replacer yields candidate match strings.
Replacer = Generator[str, None, None]

# Thresholds
SINGLE_CANDIDATE_SIMILARITY_THRESHOLD = 0.0
MULTIPLE_CANDIDATES_SIMILARITY_THRESHOLD = 0.3

def levenshtein(a: str, b: str) -> int:
    """Compute the Levenshtein edit distance between two strings."""
    if not a or not b:
        return max(len(a), len(b))
    rows = len(a) + 1
    cols = len(b) + 1
    matrix = [[0] * cols for _ in range(rows)]
    for i in range(rows):
        matrix[i][0] = i
    for j in range(cols):
        matrix[0][j] = j
    for i in range(1, rows):
        for j in range(1, cols):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            matrix[i][j] = min(
                matrix[i - 1][j] + 1,
                matrix[i][j - 1] + 1,
                matrix[i - 1][j - 1] + cost,
            )
    return matrix[len(a)][len(b)]

def simple_replacer(_content: str, find: str) -> Replacer:
    """Yield *find* unconditionally; the caller verifies via ``str.find``."""
    yield find

def line_trimmed_replacer(content: str, find: str) -> Replacer:
    """Match by trimming each line, then yield the original substring."""
    original_lines = content.split("\n")
    search_lines = find.split("\n")

    # Strip trailing empty line (common LLM artifact)
    if search_lines and search_lines[-1] == "":
        search_lines.pop()

    if not search_lines:
        return

    n_search = len(search_lines)
    for i in range(len(original_lines) - n_search + 1):
        matches = True
        for j in range(n_search):
            if original_lines[i + j].strip() != search_lines[j].strip():
                matches = False
                break
        if matches:
            start_idx = sum(len(original_lines[k]) + 1 for k in range(i))
            end_idx = start_idx
            for k in range(n_search):
                end_idx += len(original_lines[i + k])
                if k < n_search - 1:
                    end_idx += 1
            yield content[start_idx:end_idx]

def block_anchor_replacer(content: str, find: str) -> Replacer:
    """Anchor on first/last lines (trimmed) and use Levenshtein on middles."""
    original_lines = content.split("\n")
    search_lines = find.split("\n")

    if len(search_lines) < 3:
        return
    if search_lines and search_lines[-1] == "":
        search_lines.pop()
    if len(search_lines) < 3:
        return

    first_search = search_lines[0].strip()
    last_search = search_lines[-1].strip()
    search_block_size = len(search_lines)

    candidates: List[Tuple[int, int]] = []
    for i, line in enumerate(original_lines):
        if line.strip() != first_search:
            continue
        for j in range(i + 2, len(original_lines)):
            if original_lines[j].strip() == last_search:
                candidates.append((i, j))
                break

    if not candidates:
        return

    def _extract_block(start_line: int, end_line: int) -> str:
        s = sum(len(original_lines[k]) + 1 for k in range(start_line))
        e = s
        for k in range(start_line, end_line + 1):
            e += len(original_lines[k])
            if k < end_line:
                e += 1
        return content[s:e]

    if len(candidates) == 1:
        start_line, end_line = candidates[0]
        actual_size = end_line - start_line + 1
        lines_to_check = min(search_block_size - 2, actual_size - 2)

        if lines_to_check > 0:
            similarity = 0.0
            for j in range(1, min(search_block_size - 1, actual_size - 1)):
                orig_line = original_lines[start_line + j].strip()
                srch_line = search_lines[j].strip()
                max_len = max(len(orig_line), len(srch_line))
                if max_len == 0:
                    continue
                dist = levenshtein(orig_line, srch_line)
                similarity += (1 - dist / max_len) / lines_to_check
                if similarity >= SINGLE_CANDIDATE_SIMILARITY_THRESHOLD:
                    break
        else:
            similarity = 1.0

        if similarity >= SINGLE_CANDIDATE_SIMILARITY_THRESHOLD:
            yield _extract_block(start_line, end_line)
        return

    # Multiple candidates: pick the best
    best_match: Optional[Tuple[int, int]] = None
    max_similarity = -1.0

    for start_line, end_line in candidates:
        actual_size = end_line - start_line + 1
        lines_to_check = min(search_block_size - 2, actual_size - 2)

        if lines_to_check > 0:
            raw_sim = 0.0
            for j in range(1, min(search_block_size - 1, actual_size - 1)):
                orig_line = original_lines[start_line + j].strip()
                srch_line = search_lines[j].strip()
                max_len = max(len(orig_line), len(srch_line))
                if max_len == 0:
                    continue
                dist = levenshtein(orig_line, srch_line)
                raw_sim += 1 - dist / max_len
            similarity = raw_sim / lines_to_check
        else:
            similarity = 1.0

        if similarity > max_similarity:
            max_similarity = similarity
            best_match = (start_line, end_line)

    if max_similarity >= MULTIPLE_CANDIDATES_SIMILARITY_THRESHOLD and best_match:
        yield _extract_block(best_match[0], best_match[1])

def whitespace_normalized_replacer(content: str, find: str) -> Replacer:
    r"""Normalize whitespace (``\s+`` -> single space) before comparing."""

    def _normalize(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    normalized_find = _normalize(find)
    lines = content.split("\n")

    # Single-line matching
    for line in lines:
        if _normalize(line) == normalized_find:
            yield line
        else:
            normalized_line = _normalize(line)
            if normalized_find in normalized_line:
                words = find.strip().split()
                if words:
                    pattern = r"\s+".join(re.escape(word) for word in words)
                    try:
                        match = re.search(pattern, line)
                        if match:
                            yield match.group(0)
                    except re.error:
                        pass

    # Multi-line matching
    find_lines = find.split("\n")
    if len(find_lines) > 1:
        for i in range(len(lines) - len(find_lines) + 1):
            block = lines[i: i + len(find_lines)]
            if _normalize("\n".join(block)) == normalized_find:
                yield "\n".join(block)

def indentation_flexible_replacer(content: str, find: str) -> Replacer:
    """Remove the common leading indentation and compare blocks."""

    def _remove_indent(text: str) -> str:
        lines = text.split("\n")
        non_empty = [line for line in lines if line.strip()]
        if not non_empty:
            return text
        min_indent = min(len(line) - len(line.lstrip()) for line in non_empty)
        return "\n".join(
            line[min_indent:] if line.strip() else line for line in lines
        )

    normalized_find = _remove_indent(find)
    content_lines = content.split("\n")
    find_lines = find.split("\n")

    for i in range(len(content_lines) - len(find_lines) + 1):
        block = "\n".join(content_lines[i: i + len(find_lines)])
        if _remove_indent(block) == normalized_find:
            yield block

def trimmed_boundary_replacer(content: str, find: str) -> Replacer:
    """Trim the entire find block, then search."""
    trimmed_find = find.strip()
    if trimmed_find == find:
        return

    if trimmed_find in content:
        yield trimmed_find

    lines = content.split("\n")
    find_lines = find.split("\n")
    for i in range(len(lines) - len(find_lines) + 1):
        block = "\n".join(lines[i: i + len(find_lines)])
        if block.strip() == trimmed_find:
            yield block

REPLACER_CHAIN: list = [
    ("simple", simple_replacer),
    ("line_trimmed", line_trimmed_replacer),
    ("block_anchor", block_anchor_replacer),
    ("whitespace_normalized", whitespace_normalized_replacer),
    ("indentation_flexible", indentation_flexible_replacer),
    ("trimmed_boundary", trimmed_boundary_replacer),
]

def fuzzy_find_match(content: str, find: str) -> Tuple[str, int]:
    """Locate *find* in *content* using the replacer chain.

    Returns ``(matched_text, position)`` where *matched_text* is the
    actual substring of *content*, and *position* is its character offset.
    Returns ``("", -1)`` when no match is found.
    """
    for name, replacer in REPLACER_CHAIN:
        for candidate in replacer(content, find):
            pos = content.find(candidate)
            if pos == -1:
                continue
            if name != "simple":
                logger.debug(
                    "fuzzy_find_match: matched via '%s' at position %d",
                    name, pos,
                )
            return candidate, pos

    return "", -1

def fuzzy_replace(
    content: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> str:
    """Replace *old_string* with *new_string* in *content*.

    Walks the chain until a unique match is found.

    Raises:
        ValueError: When old_string not found or match is ambiguous.
    """
    if old_string == new_string:
        raise ValueError("old_string and new_string are identical")

    not_found = True

    for name, replacer in REPLACER_CHAIN:
        for candidate in replacer(content, old_string):
            idx = content.find(candidate)
            if idx == -1:
                continue

            not_found = False

            if replace_all:
                return content.replace(candidate, new_string)

            last_idx = content.rfind(candidate)
            if idx != last_idx:
                continue  # ambiguous

            return content[:idx] + new_string + content[idx + len(candidate):]

    if not_found:
        raise ValueError(
            "Could not find old_string in the file. "
            "Must match exactly (including whitespace and indentation)."
        )
    raise ValueError(
        "Found multiple matches for old_string. "
        "Provide more context to make the match unique."
    )
