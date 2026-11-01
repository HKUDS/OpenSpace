# Fix Prompt - Used when no correct parent exists in the database
# This prompt helps the LLM fix an incorrect program using error logs and feedback

FIX_SYS_FORMAT = """
You are debugging and fixing an incorrect program that has failed validation.
Your task is to analyze the error output and fix the program so it passes validation.
You MUST respond using a short summary name, description and the full code:

<NAME>
A shortened name summarizing the fix you are proposing. Lowercase, no spaces, underscores allowed.
</NAME>

<DESCRIPTION>
Describe the bug you identified and the fix you are applying. Include your analysis of the error messages.
</DESCRIPTION>

<CODE>
```{language}
# The fixed program here.
```
</CODE>

* Keep the markers "EVOLVE-BLOCK-START" and "EVOLVE-BLOCK-END" in the code. Do not change the code outside of these markers.
* Make sure your fixed program maintains the same inputs and outputs as the original program.
* Focus on making the program correct first - performance optimization is secondary.
* Make sure the file still runs after your changes.
* Use the <NAME>, <DESCRIPTION>, and <CODE> delimiters to structure your response. It will be parsed afterwards.
""".rstrip()

FIX_ITER_MSG = """# Incorrect Program to Fix

The following program has failed validation and needs to be fixed:

```{language}
{code_content}
```

## Error Information

The program is marked as **incorrect** and did not pass validation tests.

{text_feedback_section}
{error_output_section}
# Task

Analyze the error output above and fix the program. Focus on:
1. Understanding why the program failed validation
2. Identifying the root cause from the error messages
3. Implementing a fix that addresses the issue

IMPORTANT: Make the program correct first. Performance improvements can come later.
""".rstrip()


def format_error_output_section(
    stdout_log: str = "",
    stderr_log: str = "",
) -> str:
    """Format error output section for fix prompts."""
    sections = []

    if stdout_log and stdout_log.strip():
        sections.append(
            f"### Standard Output (stdout):\n\n```\n{stdout_log.strip()}\n```"
        )

    if stderr_log and stderr_log.strip():
        sections.append(
            f"### Standard Error (stderr):\n\n```\n{stderr_log.strip()}\n```"
        )

    if not sections:
        return "\n### Error Output:\n\nNo error output captured.\n"

    return "\n" + "\n\n".join(sections) + "\n"
