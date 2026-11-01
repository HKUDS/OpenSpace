---
name: sandbox-fallback-execution-048d5a
description: Fallback method to execute Python code when execute_code_sandbox fails with e2b errors
---

# Sandbox Fallback Execution

When `execute_code_sandbox` repeatedly fails with e2b sandbox errors, use this fallback pattern to execute Python code directly on the host system.

## When to Use

Use this skill when:
- `execute_code_sandbox` fails with e2b-related errors
- Multiple retry attempts (3+) have not resolved the issue
- The code does not require sandbox isolation

## Steps

### Step 1: Write Python Script to File

Use `write_file` to save your Python code as a `.py` file:

```
Tool: write_file
path: /workspace/script_name.py
content: |
  # Your Python code here
  import pandas as pd
  # ... rest of code
```

### Step 2: Execute via Shell

Use `run_shell` with explicit `python3` path:

```
Tool: run_shell
command: python3 /workspace/script_name.py
timeout: 120
```

### Step 3: Verify Output

Check the stdout/stderr from `run_shell` for:
- Successful execution messages
- Expected output or file creation confirmations
- Any errors that need debugging

## Complete Example

```yaml
# Instead of this (failing):
Tool: execute_code_sandbox
code: |
  import pandas as pd
  df = pd.DataFrame({'a': [1,2,3]})
  df.to_excel('output.xlsx')

# Use this fallback:
Tool: write_file
path: /workspace/generate_report.py
content: |
  import pandas as pd
  
  # Create sample data
  df = pd.DataFrame({'a': [1,2,3], 'b': [4,5,6]})
  
  # Save to Excel
  df.to_excel('/workspace/output.xlsx', index=False)
  print('File created successfully')

Tool: run_shell
command: python3 /workspace/generate_report.py
timeout: 120
```

## Tips

1. **Use absolute paths** in your Python script when reading/writing files
2. **Add print statements** for debugging since you won't get interactive output
3. **Increase timeout** if the script performs heavy computation
4. **Check working directory** - scripts run from the workspace root by default
5. **Handle errors explicitly** with try/except blocks and meaningful error messages

## Common e2b Errors That Trigger This Fallback

- `E2B execution error`
- `Sandbox timeout`
- `Connection refused`
- `Sandbox initialization failed`
- Repeated retry failures (3+ attempts)

## When NOT to Use This Fallback

- When sandbox isolation is required for security
- When the code needs specific sandbox environment variables
- On first failure only - try `execute_code_sandbox` retry first