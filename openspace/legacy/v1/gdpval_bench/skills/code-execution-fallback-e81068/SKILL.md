---
name: code-execution-fallback-e81068
description: Fallback workflow for executing Python code when execute_code_sandbox fails repeatedly
---

# Code Execution Fallback Workflow

## When to Use

Use this skill when `execute_code_sandbox` fails repeatedly (2+ attempts) with unknown, persistent, or unexplained errors. This fallback approach uses `write_file` + `run_shell` to save Python scripts to disk and execute them via command line, which has proven more reliable in certain failure scenarios.

## Step-by-Step Instructions

### Step 1: Detect Repeated Failures

Monitor `execute_code_sandbox` attempts. After 2 consecutive failures with errors like:
- "Unknown error"
- Timeout errors
- Unexplained execution failures
- Sandbox environment issues

Switch to the fallback workflow immediately.

### Step 2: Write the Python Script to File

Use `write_file` to save your Python code as a `.py` file in the working directory:

```python
write_file(
    path="script.py",
    content="""
import sys
import json

# Your Python code here
def main():
    # Your logic
    result = {"status": "success", "data": "example"}
    print(json.dumps(result))

if __name__ == "__main__":
    main()
"""
)
```

**Tips:**
- Use clear, self-contained code that doesn't rely on sandbox-specific paths
- Include error handling and informative print statements
- Save output to files if needed for later retrieval

### Step 3: Execute via Shell

Use `run_shell` to execute the Python script via command line:

```python
run_shell(
    command="python3 script.py",
    timeout=60  # Adjust timeout as needed
)
```

**Alternative commands:**
- `python script.py` - if python3 alias isn't available
- `python3 -u script.py` - for unbuffered output
- `python3 script.py arg1 arg2` - with arguments

### Step 4: Verify Output and Results

Check the stdout/stderr from `run_shell` to:
- Confirm execution succeeded (exit code 0)
- Inspect printed output or results
- Identify any new errors (different from sandbox errors)

If the script writes output files, use `read_file` to retrieve results.

### Step 5: Clean Up (Optional)

Remove temporary script files if they won't be reused:

```python
run_shell(command="rm script.py")
```

## Complete Example

**Scenario:** `execute_code_sandbox` failed twice while trying to process data.

**Fallback execution:**

```python
# Step 1: Write the processing script
write_file(
    path="process_data.py",
    content="""
import pandas as pd
import json

def process():
    data = [1, 2, 3, 4, 5]
    result = {"sum": sum(data), "count": len(data)}
    print(json.dumps(result))
    
    # Also save to file for reliability
    with open("result.json", "w") as f:
        json.dump(result, f)

if __name__ == "__main__":
    process()
"""
)

# Step 2: Execute via shell
output = run_shell(command="python3 process_data.py")

# Step 3: Read results from file
results = read_file(file_path="result.json", filetype="json")
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| `python3: command not found` | Try `python` instead, or check available interpreters with `which python` |
| Permission denied | Ensure the working directory is writable; `write_file` creates files in workspace by default |
| Module not found | Install dependencies via `run_shell(command="pip install package_name")` before execution |
| Script hangs | Increase timeout parameter in `run_shell` |
| Output too long | Redirect output to file within the script and read it separately |

## Best Practices

1. **Always include error handling** in scripts to capture failures gracefully
2. **Write results to files** in addition to printing, for reliable retrieval
3. **Use descriptive filenames** to avoid conflicts (e.g., `task_specific_script.py`)
4. **Keep scripts self-contained** - avoid dependencies on sandbox environment variables
5. **Log execution details** for debugging: `print(f"Step X complete: {value}")`

## When NOT to Use This Fallback

- When sandbox isolation is required for security
- When the task explicitly requires `execute_code_sandbox`
- When `execute_code_sandbox` succeeds consistently (no need to add complexity)
- When working with sensitive data that shouldn't persist to disk