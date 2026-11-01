---
name: python-debug-execution-911f17
description: Debug Python script execution failures by capturing full tracebacks and verifying working directory
---

# Python Debug Execution

This skill provides a pattern for debugging Python script execution failures. It ensures you capture actual tracebacks instead of opaque errors and verifies the working directory before file operations.

## Core Pattern

### 1. Execute with Full Traceback Capture

Always run Python scripts with stderr redirected and exit code reported:

```bash
python3 script.py 2>&1 ; echo Exit code: $?
```

This pattern:
- `2>&1` - Redirects stderr to stdout so all output (including tracebacks) is captured together
- `echo Exit code: $?` - Reports the exit code to distinguish between successful runs and failures

**Why this matters:** Opaque errors without tracebacks make it impossible to identify the root cause. The exit code tells you if the script succeeded (0) or failed (non-zero).

### 2. Verify Working Directory Before File Operations

Before any file read/write operations in your Python script, add:

```python
import os
print(f"Current working directory: {os.getcwd()}")
```

Or for debugging, add at the start of your script:

```python
import os
import sys

# Debug: show execution context
print(f"Script path: {__file__}")
print(f"Working directory: {os.getcwd()}")
print(f"Python version: {sys.version}")
```

**Why this matters:** File operations often fail due to incorrect assumptions about the current working directory. Verifying `os.getcwd()` helps diagnose path-related errors.

## Usage Examples

### Example 1: Running a Script with Debug Output

```bash
# Instead of:
python3 analyze.py

# Use:
python3 analyze.py 2>&1 ; echo Exit code: $?
```

### Example 2: Script with Directory Verification

```python
#!/usr/bin/env python3
import os
import pandas as pd

# Verify execution context
print(f"Working directory: {os.getcwd()}")

# Now safe to do file operations
data_path = "data/input.csv"
print(f"Attempting to read: {data_path}")

# Check if file exists before reading
if os.path.exists(data_path):
    df = pd.read_csv(data_path)
    print(f"Successfully loaded {len(df)} rows")
else:
    print(f"ERROR: File not found at {os.path.abspath(data_path)}")
    print(f"Directory contents: {os.listdir('.')}")
```

### Example 3: Debug-First Script Template

```python
#!/usr/bin/env python3
"""Template for debuggable Python scripts."""

import os
import sys
import traceback

def main():
    # Debug: execution context
    print("=" * 50)
    print(f"Script: {__file__}")
    print(f"Working directory: {os.getcwd()}")
    print(f"Python: {sys.version}")
    print("=" * 50)
    
    try:
        # Your main logic here
        pass
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
```

## Troubleshooting Checklist

When a Python script fails:

1. **Check the exit code** - Non-zero means failure
2. **Read the full traceback** - Look for the actual error message and line number
3. **Verify working directory** - Use `os.getcwd()` to confirm file paths are correct
4. **Check file existence** - Use `os.path.exists()` before operations
5. **Use absolute paths** - Consider `os.path.abspath()` for clarity

## When to Apply This Pattern

- Running any Python script from the command line
- Debugging script execution failures in automated agents
- Writing scripts that perform file I/O operations
- Creating reproducible execution environments