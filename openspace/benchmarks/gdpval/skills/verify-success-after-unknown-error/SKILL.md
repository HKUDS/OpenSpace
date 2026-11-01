---
name: verify-success-after-unknown-error
description: Verify task completion by checking filesystem state when execute_code_sandbox or run_shell return misleading unknown errors
---

# Verify Success After Unknown Error

## Purpose

When `execute_code_sandbox` or `run_shell` return "unknown error" messages, the underlying task may have actually succeeded. This skill provides a systematic approach to verify actual completion before assuming failure and retrying unnecessarily.

## When to Apply

Use this pattern when:
- `execute_code_sandbox` returns "unknown error" but your code may have completed
- `run_shell` fails with unclear error messages
- File creation, modification, or transformation tasks report errors
- The error message is generic/unspecified rather than a clear failure reason

## Verification Steps

### Step 1: Check Expected Output Files

After receiving an unknown error, immediately verify if expected files were created:

```python
# Example: Verify file creation after execute_code_sandbox
from tools import list_dir, read_file

# List directory to check if files exist
files = list_dir(path="/workspace/output")
print(files)

# Check specific file existence
expected_files = ["report.pdf", "data.xlsx"]
for f in expected_files:
    try:
        content = read_file(filetype="pdf", file_path=f"/workspace/output/{f}")
        print(f"✓ {f} exists and is readable")
    except:
        print(f"✗ {f} not found or unreadable")
```

### Step 2: Validate File Content/State

Don't just check existence — verify the files have expected content:

```python
# For spreadsheets
file_content = read_file(filetype="xlsx", file_path="/workspace/output/schedule.xlsx")
# Verify expected sheets, columns, or data exist

# For text/json files
file_content = read_file(filetype="txt", file_path="/workspace/output/result.json")
# Parse and validate structure

# For directories
dir_contents = list_dir(path="/workspace/output")
# Verify expected number of files or specific files exist
```

### Step 3: Decision Logic

```
IF expected files exist AND content is valid:
    → Task succeeded despite error message
    → Proceed to next step without retry
    
ELIF files exist but content is incomplete:
    → Partial success, may need targeted fix
    
ELSE (files missing or corrupted):
    → True failure, retry or debug required
```

## Code Example: Complete Pattern

```python
def execute_with_verification(code, expected_files):
    """Execute code and verify success even if error returned."""
    
    # Attempt execution
    result = execute_code_sandbox(code=code)
    
    # Check for unknown/generic errors
    if "unknown error" in result.get("output", "").lower() or result.get("error"):
        print("Received error, verifying actual outcome...")
        
        # Verify filesystem state
        all_present = True
        for f in expected_files:
            try:
                list_dir(path=f"/workspace/{f}")  # or appropriate path
                print(f"✓ {f} verified")
            except:
                print(f"✗ {f} missing")
                all_present = False
        
        if all_present:
            print("Task completed successfully despite error message")
            return {"status": "success_verified", "files": expected_files}
        else:
            print("True failure - files not created")
            return {"status": "failed", "error": result.get("error")}
    
    return {"status": "success", "output": result.get("output")}
```

## Shell Command Example

```bash
# After run_shell returns error, verify with:
ls -la /workspace/output/
test -f /workspace/output/result.pdf && echo "File exists" || echo "File missing"
file /workspace/output/result.pdf  # Verify file type is correct
```

## Benefits

- **Saves iterations**: Avoids unnecessary retries when task already succeeded
- **Handles tool bugs**: Works around sandbox/shell tool reporting issues
- **Faster completion**: Move forward immediately when verification passes
- **Clearer debugging**: Distinguishes true failures from false positives

## Anti-Patterns to Avoid

- ✗ Immediately retrying on any error without verification
- ✗ Assuming "unknown error" means complete failure
- ✗ Only checking file existence without validating content
- ✗ Skipping verification for "minor" tasks (any file output should be verified)

## Related Tools

- `list_dir` — Check directory contents
- `read_file` — Validate file content and accessibility
- `execute_code_sandbox` — Primary execution tool this pattern supports
- `run_shell` — Shell execution tool this pattern supports