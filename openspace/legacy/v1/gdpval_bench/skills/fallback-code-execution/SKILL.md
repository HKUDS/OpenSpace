---
name: fallback-code-execution
description: Fallback workflow for running code via file write and shell when sandbox execution fails
---

# Fallback Code Execution Workflow

## Overview

This skill defines a robust workaround for executing code (specifically Python) when the primary `execute_code_sandbox` tool fails repeatedly with unknown or transient errors. Instead of continuing to retry the failing tool, the agent switches to a manual file-write and shell-execution pattern.

## Trigger Conditions

Activate this workflow when:
1.  `execute_code_sandbox` fails **2 or more times** consecutively for the same logic.
2.  Error messages are generic, unknown, or indicate environment issues rather than syntax errors.
3.  The code logic itself is verified correct but the execution environment is unstable.

## Procedure

### Step 1: Write Script to File

Use the `write_file` tool to save the Python script to a specific path in the workspace.

- **Path:** Choose a descriptive name ending in `.py` (e.g., `scripts/generate_report.py`).
- **Content:** Ensure the script includes necessary error handling and print statements for debugging.
- **Dependencies:** If the script requires external libraries, ensure a `requirements.txt` is updated or installed via shell beforehand.

**Example:**
```yaml
tool: write_file
path: workspace/scripts/process_data.py
content: |
  import sys
  # ... script logic ...
  print("Success")
```

### Step 2: Execute via Shell

Use the `run_shell` tool to execute the script using the system Python interpreter.

- **Command:** `python3 <path_to_script>` or `python <path_to_script>`.
- **Working Directory:** Ensure the shell command runs from the workspace root or the directory containing the script.
- **Capture Output:** Store stdout and stderr for verification.

**Example:**
```yaml
tool: run_shell
command: python3 scripts/process_data.py
```

### Step 3: Verify Execution

1.  **Check Exit Code:** Ensure the shell command returned exit code `0`.
2.  **Check Output:** Verify expected files were created or expected stdout messages appeared.
3.  **Handle Errors:** If the shell execution fails, inspect the stderr output. This often provides more detailed tracebacks than the sandbox tool.

## Best Practices

- **Absolute Paths:** When writing scripts that access files, use absolute paths or resolve paths relative to `__file__` to avoid working directory issues.
- **Permissions:** Ensure the workspace directory allows file creation and execution.
- **Cleanup:** Optionally remove temporary scripts after successful execution if cleanliness is required.
- **Logging:** Add explicit `print()` statements in the Python script to log progress, as shell output capture is sometimes more reliable than sandbox return values.

## Example Scenario

**Problem:** `execute_code_sandbox` times out while generating a PDF.
**Solution:**
1.  Write `generate_pdf.py` to `workspace/scripts/`.
2.  Run `python3 workspace/scripts/generate_pdf.py` via `run_shell`.
3.  Confirm `output.pdf` exists in the workspace.