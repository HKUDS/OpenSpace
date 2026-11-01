---
name: sandbox-exec-fallback
description: Fallback pattern for executing Python code when execute_code_sandbox fails by writing to file and running via shell
---

# Sandbox Execution Fallback

## When to Use

Use this skill when `execute_code_sandbox` fails during Python code execution, particularly for tasks involving:
- Spreadsheet generation (xlsx, csv)
- Data processing and transformations
- File output operations
- Complex computations requiring libraries

## Recovery Pattern

When `execute_code_sandbox` returns an error or fails to produce expected output:

### Step 1: Write Python Code to File

Use `write_file` to save your Python script to a file:

```
# Use write_file tool with:
path: "script.py"
content: <your complete Python code as a string>
```

### Step 2: Execute via Shell

Use `run_shell` to execute the script:

```
# Use run_shell tool with:
command: "python3 script.py"
timeout: <appropriate timeout, e.g., 300 for long-running tasks>
```

### Step 3: Verify Output

Check the shell output for:
- Success messages or printed results
- Error messages (if any, diagnose and fix the script)
- File creation confirmations

### Step 4: Access Generated Files

If the script creates output files (e.g., spreadsheets), they will be in the current working directory. Use `list_dir` to confirm, then `read_file` to access if needed.

## Why This Works

- **Same Python Environment**: `python3` in the shell uses the same environment as the sandbox
- **More Robust**: Shell execution handles long-running or memory-intensive tasks better
- **Debuggable**: Errors are captured in stdout/stderr for easier diagnosis
- **Identical Results**: Produces the same output as sandbox execution when successful

## Example: Spreadsheet Generation

Original code that failed in execute_code_sandbox:

```python
import pandas as pd

data = {'A': [1, 2, 3], 'B': [4, 5, 6]}
df = pd.DataFrame(data)
df.to_excel('output.xlsx', index=False)
print("File created successfully")
```

Recovery Execution:

1. `write_file` with path="generate_spreadsheet.py" and the code above as content
2. `run_shell` with command="python3 generate_spreadsheet.py"
3. Verify output.xlsx was created with `list_dir`

## Tips

- Increase `timeout` for complex operations (default 30s may be insufficient)
- Add explicit print statements for debugging
- Handle exceptions in your script and print error messages
- Clean up temporary script files after successful execution if needed