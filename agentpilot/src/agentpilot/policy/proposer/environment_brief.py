"""Model-facing views of previously generated environment descriptions.

New builders omit execution allowances. Old immutable packets can still carry
the exact builder-owned clauses below. Remove only those clauses in this view;
leave the packet, runtime settings, task data and arbitrary user prose intact.
"""
import re


_AIME_HEADER = "The evolution target is Harbor's Terminus 2 agent."
_TB2_HEADER = "The evolution target is Harbor's Terminus 2 agent on Terminal-Bench 2.1"
_NUMBER = r"\d+(?:\.\d+)?(?:[eE][+-]?\d+)?"
_AIME_ALLOWANCE = re.compile(
    r" The configured agent worker wall-clock budget is "
    + _NUMBER + r" seconds \(" + _NUMBER + r" minutes\) per task\."
)
_TB2_ALLOWANCE = (
    "Available software, required outputs and the agent time budget are task-specific; "
    "the budget comes from task.toml [agent].timeout_sec."
)


def environment_brief(text: str) -> str:
    """Project known legacy builder text without scanning source or trace bodies."""
    if text.startswith(_AIME_HEADER) and "Tasks are AIME competition math problems" in text:
        before, grading, after = text.partition('grading is exact match.')
        allowance = _AIME_ALLOWANCE.match(after) if grading else None
        if allowance is not None:
            return before + grading + after[allowance.end():]
    if text.startswith(_TB2_HEADER):
        paragraph, separator, rest = text.partition('\n\n')
        return paragraph.replace(_TB2_ALLOWANCE, "Available software and required outputs are task-specific.", 1) + separator + rest
    return text
