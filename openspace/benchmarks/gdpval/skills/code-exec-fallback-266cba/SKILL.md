---
name: code-exec-fallback-266cba
description: Fallback workflow for reliable code execution when sandbox fails repeatedly
---

# Code Execution Fallback Pattern

## When to Use This Skill

Apply this pattern when you encounter **repeated failures** with `execute_code_sandbox`:
- 2+ consecutive failures with opaque or unknown errors
- Timeout errors that persist across retry attempts
- Environment-related errors that don't resolve with code fixes

## The Fallback Workflow

### Step 1: Detect Repeated Failures

Track execution failures. After 2 consecutive failures with `execute_code_sandbox`, switch to the fallback approach.

### Step 2: Write Script to File

Use `write_file` to save your Python script:

```
write_file(
    path="/workspace/script_name.py",
    content="# Your Python code here\nimport sys\n..."
)
```

### Step 3: Execute via Shell

Use `run_shell` to run the script:

```
run_shell(
    command="python /workspace/script_name.py",
    timeout=300
)
```

### Step 4: Capture Output

Parse stdout/stderr from `run_shell` output to verify success or diagnose issues.

## Complete Example

```python
# Instead of this (which may fail):
result = execute_code_sandbox(code="import pandas as pd\n...")

# Use this fallback pattern:
script_content = """
import pandas as pd
import sys

try:
    # Your logic here
    df = pd.DataFrame({'col': [1, 2, 3]})
    print(df.to_csv())
    sys.exit(0)
except Exception as e:
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)
"""

# Write the script
write_file(path="/workspace/my_script.py", content=script_content)

# Execute via shell
result = run_shell(command="python /workspace/my_script.py", timeout=300)
```

## Best Practices

1. **Add error handling** in your script - use try/except with `sys.exit()` codes
2. **Set appropriate timeouts** - `run_shell` default is 30s, increase for heavy operations
3. **Clean up temporary files** after execution if needed
4. **Log the fallback trigger** - document why you switched approaches
5. **Verify Python availability** - Most sandboxes have Python 3.x by default

## Why This Works

- `write_file` is more reliable for file I/O operations
- `run_shell` gives you direct control over execution environment
- Shell execution bypasses sandbox serialization issues
- Better error visibility through stdout/stderr streams

## When NOT to Use This Pattern

- First-time execution failures (retry the sandbox first)
- Simple one-liner code (sandbox is faster)
- When sandbox errors are clearly code bugs (fix the code instead)
- Security-sensitive operations requiring sandbox isolation