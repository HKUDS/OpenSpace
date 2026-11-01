"""Post-assembly check for malformed EVOLVE-BLOCK markers in a candidate."""

from __future__ import annotations

from rsi_arena.baselines.shinka.runtime.utils.languages import (
    get_evolve_marker_examples,
    get_evolve_marker_patterns,
    has_block_comments,
    normalize_language,
)


def validate_evolve_markers(text: str, language: str) -> str | None:
    """Return ``None`` if the candidate's EVOLVE-BLOCK markers are well-formed
    for the given language, else a human-readable error description.

    The check is deliberately scoped to the marker lines themselves — it
    never parses or balances arbitrary comments elsewhere in the file, so a
    candidate that happens to contain block-comment delimiters in unrelated
    code (e.g. a Wolfram string literal containing ``(*``) is not affected.

    1.  **Always** — both markers must be present somewhere in the file.
        Missing markers mean the candidate cannot be re-evolved later.

    2.  **Block-comment languages only** (Wolfram, Markdown) — every line
        containing a marker must match the strict canonical form
        (e.g. ``(* EVOLVE-BLOCK-START *)``). Because that form requires the
        comment to open *and close* on the marker line, it directly catches
        the LLM failure mode where a marker is emitted without its closing
        delimiter, leaving the candidate body trapped inside a comment.

    Line-comment languages (everything that is not block-comment) need
    only the existence check: ``# EVOLVE-BLOCK-END`` is still a valid line
    comment even if it trails another statement, so the strict per-line
    form would over-reject.
    """
    try:
        canonical = normalize_language(language)
    except ValueError:
        return None

    try:
        start_pat, end_pat = get_evolve_marker_patterns(canonical)
    except ValueError:
        return None

    lines = text.splitlines()
    start_lines = [
        (i, line) for i, line in enumerate(lines) if "EVOLVE-BLOCK-START" in line
    ]
    end_lines = [
        (i, line) for i, line in enumerate(lines) if "EVOLVE-BLOCK-END" in line
    ]

    errors: list[str] = []

    if not start_lines:
        errors.append("No EVOLVE-BLOCK-START marker found in candidate.")
    if not end_lines:
        errors.append("No EVOLVE-BLOCK-END marker found in candidate.")

    if has_block_comments(canonical):
        expected_start, expected_end = get_evolve_marker_examples(canonical)

        for line_no, line in start_lines:
            if not start_pat.match(line):
                errors.append(
                    _marker_error(
                        line_no, line, "EVOLVE-BLOCK-START", expected_start, canonical
                    )
                )

        for line_no, line in end_lines:
            if not end_pat.match(line):
                errors.append(
                    _marker_error(
                        line_no, line, "EVOLVE-BLOCK-END", expected_end, canonical
                    )
                )

    if errors:
        header = f"EVOLVE-BLOCK marker validation failed for language {canonical!r}:"
        return "\n".join([header, *errors])
    return None


def _marker_error(
    line_no: int, line: str, marker: str, expected: str, language: str
) -> str:
    return (
        f"Line {line_no + 1}: malformed {marker} marker.\n"
        f"  Got:      {line.rstrip()!r}\n"
        f"  Expected: {expected!r} (the marker must occupy its own line and "
        f"be fully wrapped in {language} comment delimiters; nothing else "
        f"on the line)."
    )
