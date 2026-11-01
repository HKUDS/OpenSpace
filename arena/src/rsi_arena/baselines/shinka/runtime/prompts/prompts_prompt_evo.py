"""
Prompts for evolving system prompts (meta-meta level).

These prompts are used to generate mutations of task_sys_msg prompts
using different strategies: diff (targeted modifications) and full (complete
rewrites). Both strategies receive the same context: the current prompt
and top-k performing programs.
"""

from typing import List, Optional
from rsi_arena.baselines.shinka.runtime.database.prompt_dbase import SystemPrompt


# =============================================================================
# SHARED SYSTEM MESSAGE BASE
# =============================================================================

PROMPT_EVO_SYSTEM_BASE = (
    "You are an expert prompt engineer specializing in crafting optimal "
    "task instructions for code generation. You will be shown a system "
    "prompt and examples of top-performing programs generated using it.\n\n"
    "Your goal is to improve the system prompt so that future code "
    "generations achieve even higher scores.\n\n"
    "Analyze the successful programs to understand:\n"
    "1. What patterns or techniques led to high scores\n"
    "2. What the prompt could emphasize more clearly\n"
    "3. What aspects of the prompt may be unclear or suboptimal\n\n"
    "DO NOT RECOMMEND VISUALIZATION OR GRAPHICAL OUTPUTS. ONLY RECOMMEND "
    "TASK-SPECIFIC CODE IMPROVEMENT RECOMMENDATIONS.\n\n"
    "You MUST respond using a short summary name, description, and the "
    "new prompt:\n\n"
    "<NAME>\n"
    "A shortened name summarizing the prompt approach. Lowercase, "
    "no spaces, underscores allowed.\n"
    "</NAME>\n\n"
    "<DESCRIPTION>\n"
    "A description of your changes and the reasoning behind them.\n"
    "</DESCRIPTION>\n\n"
    "<PROMPT>\n"
    "The improved system prompt text here.\n"
    "</PROMPT>\n\n"
    "* Use the <NAME>, <DESCRIPTION>, and <PROMPT> delimiters to "
    "structure your response."
)


# =============================================================================
# DIFF-STYLE PROMPT EVOLUTION
# =============================================================================

PROMPT_EVO_DIFF_SYSTEM = (
    PROMPT_EVO_SYSTEM_BASE + "\n\n"
    "IMPORTANT: Make TARGETED modifications based on SPECIFIC patterns you "
    "observe in the successful programs. Do not just rephrase or reorganize - "
    "you must add NEW guidance derived from analyzing what made the top "
    "programs successful.\n\n"
    "For each modification, explicitly identify:\n"
    "1. A specific technique or pattern from the top programs\n"
    "2. How to encode this insight as actionable guidance in the prompt"
)

PROMPT_EVO_DIFF_USER = (
    "# Current System Prompt\n"
    "```\n{current_prompt}\n```\n\n"
    "{global_scratchpad_section}"
    "# Top Performing Programs\n"
    "{top_programs}\n\n"
    "# Instructions\n"
    "CAREFULLY analyze the top-performing programs above. Identify 1-3 "
    "SPECIFIC techniques, algorithms, or implementation patterns that "
    "contributed to their high scores.\n\n"
    "Then modify the system prompt to explicitly encourage these patterns. "
    "Your changes should:\n"
    "- Reference concrete techniques you observed (e.g., 'use vectorized "
    "operations', 'implement early pruning', 'use restart strategies')\n"
    "- Add specific algorithmic guidance based on what worked\n"
    "- NOT just rephrase existing instructions - add NEW actionable "
    "insights\n\n"
    "In your <DESCRIPTION>, explain which specific patterns from the programs "
    "inspired each change.\n\n"
    "Provide your response using the <NAME>, <DESCRIPTION>, and <PROMPT> "
    "delimiters."
)


# =============================================================================
# FULL REWRITE PROMPT EVOLUTION
# =============================================================================

PROMPT_EVO_FULL_SYSTEM = (
    PROMPT_EVO_SYSTEM_BASE + "\n\n"
    "You have freedom to completely rewrite the prompt. Your new prompt "
    "MUST incorporate specific algorithmic insights and implementation "
    "strategies extracted from the successful programs.\n\n"
    "Structure your rewrite around:\n"
    "1. Key techniques that made the top programs successful\n"
    "2. Specific algorithmic patterns to recommend\n"
    "3. Implementation strategies that led to high scores"
)

PROMPT_EVO_FULL_USER = (
    "# Current System Prompt\n"
    "```\n{current_prompt}\n```\n\n"
    "{global_scratchpad_section}"
    "# Top Performing Programs\n"
    "{top_programs}\n\n"
    "# Instructions\n"
    "First, ANALYZE the top-performing programs to extract:\n"
    "- What algorithms or data structures did they use?\n"
    "- What optimizations or clever techniques appear?\n"
    "- What implementation patterns led to high scores?\n\n"
    "Then, write a NEW system prompt that explicitly guides future code "
    "generation to use these successful approaches. Your prompt should:\n"
    "- Include specific algorithmic recommendations\n"
    "- Mention concrete techniques observed in the successful programs\n"
    "- Provide actionable implementation guidance\n\n"
    "In your <DESCRIPTION>, list the key insights you extracted from the "
    "programs and how you incorporated them.\n\n"
    "Provide your response using the <NAME>, <DESCRIPTION>, and <PROMPT> "
    "delimiters."
)


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================


def format_global_scratchpad(scratchpad: Optional[str]) -> str:
    """
    Format the global scratchpad section for prompt evolution.

    Args:
        scratchpad: The global scratchpad content from meta-reviewing

    Returns:
        Formatted string with the scratchpad section, or empty string if None
    """
    if not scratchpad or not scratchpad.strip():
        return ""

    return (
        "# Global Insights from Meta-Review\n"
        "The following insights have been extracted from analyzing the "
        "evolution of programs so far. Use these to guide your prompt "
        "improvements:\n\n"
        f"{scratchpad.strip()}\n\n"
    )


def format_top_programs(
    programs: List,  # List[Program]
    language: str = "python",
    include_text_feedback: bool = False,
) -> str:
    """
    Format a list of top-performing programs for prompt evolution context.

    Args:
        programs: List of Program objects (top performers)
        language: Programming language for code blocks
        include_text_feedback: Whether to include text feedback

    Returns:
        Formatted string with program information
    """
    if not programs:
        return "No program examples available."

    parts = []
    for i, prog in enumerate(programs, 1):
        program_str = f"## Program {i}\n\n"
        program_str += f"```{language}\n{prog.code}\n```\n\n"
        program_str += f"**Score**: {prog.combined_score:.4f}\n"

        if prog.public_metrics:
            metrics_str = ", ".join(
                f"{k}={v:.4f}" if isinstance(v, float) else f"{k}={v}"
                for k, v in prog.public_metrics.items()
            )
            program_str += f"**Metrics**: {metrics_str}\n"

        if include_text_feedback and prog.text_feedback:
            feedback_text = prog.text_feedback
            if isinstance(feedback_text, list):
                feedback_text = "\n".join(feedback_text)
            if feedback_text.strip():
                program_str += f"\n**Feedback**:\n{feedback_text.strip()}\n"

        parts.append(program_str)

    return "\n---\n\n".join(parts)


def construct_prompt_evolution_context(
    parent_prompt: SystemPrompt,
    top_programs: List,  # List[Program]
    language: str = "python",
    include_text_feedback: bool = False,
    global_scratchpad: Optional[str] = None,
) -> dict:
    """
    Construct the context dictionary for prompt evolution.

    Args:
        parent_prompt: The prompt to evolve
        top_programs: List of top-performing programs
        language: Programming language for code blocks
        include_text_feedback: Whether to include text feedback
        global_scratchpad: Global insights from meta-reviewing

    Returns:
        Dictionary with formatted context
    """
    return {
        "current_prompt": parent_prompt.prompt_text,
        "top_programs": format_top_programs(
            top_programs, language, include_text_feedback
        ),
        "global_scratchpad_section": format_global_scratchpad(global_scratchpad),
    }


def construct_diff_evolution_prompt(
    parent_prompt: SystemPrompt,
    top_programs: Optional[List] = None,  # Optional[List[Program]]
    language: str = "python",
    include_text_feedback: bool = False,
    global_scratchpad: Optional[str] = None,
) -> tuple:
    """
    Construct the system and user messages for diff-style prompt evolution.

    Args:
        parent_prompt: The prompt to evolve
        top_programs: List of top-performing programs for context
        language: Programming language for code blocks
        include_text_feedback: Whether to include text feedback
        global_scratchpad: Global insights from meta-reviewing

    Returns:
        Tuple of (system_message, user_message)
    """
    context = construct_prompt_evolution_context(
        parent_prompt,
        top_programs or [],
        language,
        include_text_feedback,
        global_scratchpad,
    )

    user_msg = PROMPT_EVO_DIFF_USER.format(**context)

    return PROMPT_EVO_DIFF_SYSTEM, user_msg


def construct_full_evolution_prompt(
    parent_prompt: SystemPrompt,
    top_programs: Optional[List] = None,  # Optional[List[Program]]
    language: str = "python",
    include_text_feedback: bool = False,
    global_scratchpad: Optional[str] = None,
) -> tuple:
    """
    Construct the system and user messages for full rewrite prompt evolution.

    Args:
        parent_prompt: The prompt to evolve
        top_programs: List of top-performing programs for context
        language: Programming language for code blocks
        include_text_feedback: Whether to include text feedback
        global_scratchpad: Global insights from meta-reviewing

    Returns:
        Tuple of (system_message, user_message)
    """
    context = construct_prompt_evolution_context(
        parent_prompt,
        top_programs or [],
        language,
        include_text_feedback,
        global_scratchpad,
    )

    user_msg = PROMPT_EVO_FULL_USER.format(**context)

    return PROMPT_EVO_FULL_SYSTEM, user_msg


# Legacy functions for backward compatibility
def format_prompt_for_evolution(
    prompt: SystemPrompt,
    recent_improvements: Optional[List[float]] = None,
) -> dict:
    """
    DEPRECATED: Use construct_prompt_evolution_context instead.

    Format a SystemPrompt for use in evolution prompts.
    """
    return {
        "current_prompt": prompt.prompt_text,
        "top_programs": "No program examples available.",
    }


def format_inspiration_prompts(
    inspirations: List[SystemPrompt],
    max_inspirations: int = 3,
) -> str:
    """
    Format a list of inspiration prompts for crossover.

    Args:
        inspirations: List of SystemPrompt objects to use as inspiration
        max_inspirations: Maximum number of inspirations to include

    Returns:
        Formatted string with inspiration prompts
    """
    if not inspirations:
        return "No inspiration prompts available."

    # Take top N by fitness
    sorted_insp = sorted(inspirations, key=lambda p: p.fitness, reverse=True)
    selected = sorted_insp[:max_inspirations]

    parts = []
    for i, p in enumerate(selected, 1):
        parts.append(
            f"## Inspiration {i}\n"
            f"```\n{p.prompt_text}\n```\n"
            f"Performance: fitness={p.fitness:.4f}, programs={p.program_count}"
        )

    return "\n\n".join(parts)
