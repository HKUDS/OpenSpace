---
name: code-exec-fallback
description: Fallback pattern for executing Python code when execute_code_sandbox fails
---

# Code Execution Fallback

## When to Use

Use this pattern when `execute_code_sandbox` fails repeatedly (typically 2+ attempts) due to environment limitations, timeouts, dependency issues, or sandbox restrictions.

## The Pattern

Instead of executing code directly in the sandbox, write the Python script to a file and execute it via shell:

1. **Write the script** using `write_file`
2. **Execute via shell** using `run_shell` with `python3 script.py`
3. **Clean up** (optional) remove the temporary file

## Step-by-Step Instructions

### Step 1: Write the Python Script

```
Use write_file to save your Python code:
- Path: Choose a descriptive name (e.g., "process_data.py", "analyze.py")
- Content: Your complete Python script with all imports and logic
```

### Step 2: Execute via Shell

```
Use run_shell to execute:
- Command: "python3 <script_name>.py"
- Timeout: Set appropriately for your task (default 30s, increase if needed)
```

### Step 3: Handle Output

```
- Capture stdout/stderr from run_shell
- Parse results as needed
- Optionally delete the script file after execution
```

## Example

```python
# Instead of this (which may fail):
execute_code_sandbox(code="import pandas as pd; df = pd.read_csv('data.csv')...")

# Do this:
write_file(path="analyze.py", content="""
import pandas as pd
import json

df = pd.read_csv('data.csv')
result = df.groupby('category').sum()
print(json.dumps(result.to_dict()))
""")

run_shell(command="python3 analyze.py", timeout=60)
```

## Tips for Success

1. **Include all imports** in the script file - the shell environment may differ from the sandbox
2. **Use absolute paths** or ensure working directory is correct
3. **Add error handling** to your script for better debugging
4. **Increase timeout** for long-running operations (default is 30s)
5. **Print structured output** (JSON) if you need to parse results
6. **Clean up temporary files** after successful execution to avoid clutter

## When This Helps

- Sandbox has missing dependencies
- Code execution times out in sandbox but would work in shell
- File I/O operations are restricted in sandbox
- Need to run external commands or system utilities
- Complex multi-file projects that need proper file structure