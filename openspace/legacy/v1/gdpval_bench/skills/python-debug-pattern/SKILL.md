---
name: python-debug-pattern
description: Debug Python script execution failures by capturing full output with exit codes and verifying working directory before file operations
---

# Python Debug Pattern

This skill provides a reusable pattern for debugging Python script execution failures. It ensures you capture actual tracebacks instead of opaque errors and verify the working directory before file operations.

## Core Technique

### 1. Execute Python Scripts with Full Error Capture

When running Python scripts, always use this command pattern to surface actual tracebacks:

```bash
python3 script.py 2>&1 ; echo Exit code: $?
```

**Why this works:**
- `2>&1` redirects stderr to stdout, capturing both regular output and errors
- `; echo Exit code: $?` displays the actual exit code after execution
- This reveals the full Python traceback instead of opaque failure messages

**Examples:**

```bash
# Good: Full error capture
python3 process_data.py 2>&1 ; echo Exit code: $?

# Bad: Opaque error (no stderr capture, no exit code)
python3 process_data.py
```

### 2. Verify Working Directory Before File Operations

Before any file read/write operations in Python, verify the current working directory:

```python
import os

# At script start, log the working directory
print(f"Working directory: {os.getcwd()}")

# For file operations, use absolute paths or log the resolved path
file_path = "output/result.csv"
abs_path = os.path.abspath(file_path)
print(f"Writing to: {abs_path}")
```

**Why this works:**
- Many failures occur because scripts run from unexpected directories
- Logging the working directory immediately surfaces path-related issues
- Absolute paths prevent ambiguity in file operations

**Full example script structure:**

```python
#!/usr/bin/env python3
import os
import sys

def main():
    # Debug: verify working directory
    print(f"Working directory: {os.getcwd()}", file=sys.stderr)
    
    # Debug: list directory contents if dealing with files
    print(f"Directory contents: {os.listdir('.')}", file=sys.stderr)
    
    # Your actual logic here
    # ...

if __name__ == "__main__":
    main()
```

## When to Apply This Pattern

- Python script fails with an opaque error message
- File operations (read/write) fail unexpectedly
- Script works locally but fails in automated execution
- Debugging CI/CD pipeline failures
- Investigating "file not found" or permission errors

## Quick Checklist

- [ ] Run script with `2>&1 ; echo Exit code: $?` pattern
- [ ] Add `os.getcwd()` logging at script start
- [ ] Use `os.path.abspath()` for file paths
- [ ] Log directory contents with `os.listdir('.')` if relevant
- [ ] Check for missing dependencies (capture full traceback)

## Common Failure Modes This Pattern Surfaces

| Symptom | Without Pattern | With Pattern |
|---------|-----------------|--------------|
| File not found | "Error: failed" | Full traceback showing exact path attempted |
| Permission denied | Script exits silently | stderr shows permission error |
| Missing module | Opaque exit | ImportError with module name |
| Wrong directory | Confusing path errors | cwd logged, reveals directory mismatch |