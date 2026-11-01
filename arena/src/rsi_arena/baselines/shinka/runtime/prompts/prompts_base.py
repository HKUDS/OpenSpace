from typing import List, Dict
from rsi_arena.baselines.shinka.runtime.database import Program


BASE_SYSTEM_MSG = (
    "You are an expert software engineer tasked with improving the "
    "performance of a given program. Your job is to analyze the current "
    "program and suggest improvements based on the collected feedback from "
    "previous attempts."
)


def perf_str(combined_score: float, public_metrics: Dict[str, float]) -> str:
    perf_str = f"Combined score to maximize: {combined_score:.2f}\n"
    for key, value in public_metrics.items():
        if isinstance(value, float):
            perf_str += f"{key}: {value:.2f}; "
        else:
            perf_str += f"{key}: {value}; "
    return perf_str[:-2]


def format_text_feedback_section(text_feedback) -> str:
    """Format text feedback for inclusion in prompts."""
    if not text_feedback or not text_feedback.strip():
        return ""

    feedback_text = text_feedback
    if isinstance(feedback_text, list):
        feedback_text = "\n".join(feedback_text)

    return f"""
Here is additional text feedback about the current program:

{feedback_text.strip()}
"""


def construct_eval_history_msg(
    inspiration_programs: List[Program],
    language: str = "python",
    include_text_feedback: bool = False,
    correct: bool = True,
) -> str:
    """Construct an edit message for the given parent program and
    inspiration programs."""
    if correct:
        inspiration_str = (
            "Here are the performance metrics of a set of previously "
            "implemented programs:\n\n"
        )
    else:
        inspiration_str = (
            "Here is the error output of a set of previously "
            "implemented but incorrect programs:\n\n"
        )

    for i, prog in enumerate(inspiration_programs):
        if i == 0:
            inspiration_str += "# Prior programs\n\n"
        inspiration_str += f"```{language}\n{prog.code}\n```\n\n"

        if correct:
            inspiration_str += (
                f"Performance metrics:\n"
                f"{perf_str(prog.combined_score, prog.public_metrics)}\n\n"
            )
        else:
            inspiration_str += (
                "The program is incorrect and does not pass all validation tests.\n\n"
            )

        # Add text feedback if available and requested
        if include_text_feedback and prog.text_feedback:
            feedback_text = prog.text_feedback
            if isinstance(feedback_text, list):
                feedback_text = "\n".join(feedback_text)
            if feedback_text.strip():
                inspiration_str += f"Text feedback:\n{feedback_text.strip()}\n\n"

    return inspiration_str


def construct_individual_program_msg(
    program: Program,
    language: str = "python",
    include_text_feedback: bool = False,
) -> str:
    """Construct a message for a single program for individual analysis."""
    program_str = "# Program to Analyze\n\n"
    program_str += f"```{language}\n{program.code}\n```\n\n"
    program_str += (
        f"Performance metrics:\n"
        f"{perf_str(program.combined_score, program.public_metrics)}\n\n"
    )
    # Include program correctness if available
    if program.correct:
        program_str += "The program is correct and passes all validation tests.\n\n"
    else:
        program_str += (
            "The program is incorrect and does not pass all validation tests.\n\n"
        )

    # Add text feedback if available and requested
    if include_text_feedback and program.text_feedback:
        feedback_text = program.text_feedback
        if isinstance(feedback_text, list):
            feedback_text = "\n".join(feedback_text)
        if feedback_text.strip():
            program_str += f"Text feedback:\n{feedback_text.strip()}\n\n"

    return program_str
