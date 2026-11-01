"""Canonical shared defaults for Shinka configs and CLIs."""

from __future__ import annotations

from typing import Any

DEFAULT_TASK_SYS_MSG = (
    "You are an expert optimization and algorithm design assistant. "
    "Improve the program while preserving correctness and immutable regions."
)


def default_patch_types() -> list[str]:
    return ["diff", "full", "cross"]


def default_patch_type_probs() -> list[float]:
    return [0.6, 0.3, 0.1]


def default_llm_models() -> list[str]:
    return [
        "gpt-5-mini",
        "gemini-3-flash-preview",
        "gemini-3.1-pro-preview",
        "gpt-5.4",
    ]


def default_llm_dynamic_selection_kwargs() -> dict[str, Any]:
    return {"cost_aware_coef": 0.5}


def default_llm_kwargs() -> dict[str, Any]:
    return {
        "temperatures": [0.0, 0.5, 1.0],
        "max_tokens": 16384,
    }


def default_prompt_patch_types() -> list[str]:
    return ["diff", "full"]


def default_prompt_patch_type_probs() -> list[float]:
    return [0.7, 0.3]


def default_archive_criteria() -> dict[str, float]:
    return {"combined_score": 1.0}
