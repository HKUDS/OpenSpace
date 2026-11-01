---
name: python-debug-execution
description: Debug Python scripts with proper error surfacing and working directory verification
---

# Python Debug Execution Pattern

When executing Python scripts that may fail, use this pattern to surface clear error information and diagnose issues effectively.

## Core Technique

### 1. Execute with Full Error Output

Always run Python scripts with stderr redirected to stdout and echo the exit code:

```bash
python3 script.py 2>&1 ; echo Exit code: $?
```

**Why this works:**
- `2>&1` captures both stdout and stderr, ensuring tracebacks are visible
- `echo Exit code: $?` reveals the actual exit status for debugging
- Avoids opaque "command failed" errors that hide the real cause

### 2. Verify Working Directory in Script

Before any file operations in Python scripts, add working directory verification:

```python
import os

# At the start of your script or before file operations
print(f"Current working directory: {os.getcwd()}")

# For debugging, also list directory contents
print(f"Directory contents: {os.listdir('.')}")
```

**Why this works:**
- File not found errors often stem from incorrect working directory assumptions
- Makes path-related failures immediately diagnosable
- Confirms the execution context matches expectations

## Complete Debugging Workflow

### Step 1: Add Diagnostic Code to Script

```python
#!/usr/bin/env python3
import os
import sys

def main():
    # Diagnostic: verify execution context
    print(f"Working directory: {os.getcwd()}")
    print(f"Python version: {sys.version}")
    print(f"Directory listing: {os.listdir('.')}")
    
    # Your actual logic here
    # ...

if __name__ == "__main__":
    main()
```

### Step 2: Execute with Full Error Capture

```bash
python3 script.py 2>&1 ; echo Exit code: $?
```

### Step 3: Analyze Output

Look for:
- Traceback messages (indicate code errors)
- Exit code (0 = success, non-zero = failure)
- Working directory confirmation
- Missing file/directory errors

## Common Failure Patterns

| Symptom | Likely Cause | Debug Clue |
|---------|-------------|------------|
| FileNotFoundError | Wrong working directory | Check `os.getcwd()` output |
| ModuleNotFoundError | Missing dependencies | Traceback shows import path |
| PermissionError | File access issues | Traceback shows file path |
| Silent failure (exit 0, no output) | Logic bug, not crash | Add print statements |

## When to Use This Pattern

- Running Python scripts in automated/agent contexts
- Debugging scripts that interact with files
- Troubleshooting CI/CD pipeline failures
- Any scenario where script output may be truncated or hidden