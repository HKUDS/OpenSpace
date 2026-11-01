---
name: execute-code-fallback
description: Fallback from execute_code_sandbox to run_shell when e2b sandbox is unavailable
---

# Execute Code Fallback Pattern

When `execute_code_sandbox` fails due to e2b unavailability, immediately fall back to `run_shell` with embedded Python code. This ensures task continuity when the sandbox service is down.

## When to Apply

Use this pattern when you observe:
- `execute_code_sandbox` returns errors mentioning "e2b", "sandbox unavailable", or connection timeouts
- Repeated sandbox execution failures (2+ attempts)
- Error messages indicating the code execution environment is unreachable

## Fallback Procedure

### Step 1: Detect the Failure

Identify that `execute_code_sandbox` has failed. Common error indicators:
- "e2b service unavailable"
- "Sandbox connection failed"
- "Execution environment not reachable"
- Timeout errors during code execution

### Step 2: Switch to run_shell with Embedded Python

Instead of:
```python
execute_code_sandbox(code="...")
```

Use:
```bash
run_shell(command="python3 -c '...your Python code...'")
```

### Step 3: Install Dependencies First (If Needed)

If your Python code requires external packages, install them first:

```bash
run_shell(command="pip install pandas requests matplotlib")
```

Then execute your main code:

```bash
run_shell(command="python3 << 'EOF'
import pandas as pd
import requests

# Your code here
print("Success")
EOF
")
```

### Step 4: Use Heredoc for Multi-line Code

For complex Python scripts, use heredoc syntax for cleaner code:

```bash
run_shell(command="python3 << 'PYTHON_SCRIPT'
import json
import os

# Complex logic here
data = {'key': 'value'}
with open('output.json', 'w') as f:
    json.dump(data, f)

print('File created successfully')
PYTHON_SCRIPT
")
```

## Complete Example

**Scenario:** You need to process a CSV file and generate a report.

**Original approach (sandbox):**
```python
execute_code_sandbox(code="""
import pandas as pd
df = pd.read_csv('data.csv')
summary = df.describe()
print(summary)
""")
```

**Fallback approach (run_shell):**
```bash
# First install dependencies if needed
run_shell(command="pip install pandas --quiet")

# Then execute the code
run_shell(command="python3 << 'EOF'
import pandas as pd
df = pd.read_csv('data.csv')
summary = df.describe()
print(summary)
EOF
")
```

## Important Considerations

1. **State Persistence**: Unlike `execute_code_sandbox`, `run_shell` executions may not share state between calls. Save intermediate results to files if needed.

2. **Working Directory**: Ensure you're operating in the correct directory. Use `pwd` to verify or include `cd /path/to/workdir` in your commands.

3. **Python Version**: Use `python3` explicitly to avoid ambiguity. Verify with `python3 --version` if needed.

4. **Error Handling**: Check the stdout/stderr from `run_shell` to confirm success. Failed Python scripts will return non-zero exit codes.

5. **Security**: Be cautious when embedding user-provided data into shell commands. Escape appropriately or use file-based input.

6. **Performance**: For large computations, `run_shell` may be slower than sandbox. Consider breaking into smaller steps if timeouts occur.

## Quick Reference

| Task | Sandbox Approach | Fallback Approach |
|------|-----------------|-------------------|
| Simple calculation | `execute_code_sandbox(code="print(2+2)")` | `run_shell(command="python3 -c 'print(2+2)'")` |
| Install + run | `execute_code_sandbox(code="import pkg; ...")` | `run_shell(command="pip install pkg && python3 -c '...'")` |
| Multi-line script | `execute_code_sandbox(code="...")` | `run_shell(command="python3 << 'EOF'...EOF")` |
| File I/O | `execute_code_sandbox(code="...")` | `run_shell(command="python3 << 'EOF'...EOF")` |

## Recovery Checklist

- [ ] Confirm `execute_code_sandbox` failure (not a code bug)
- [ ] Switch to `run_shell` immediately
- [ ] Install required packages with `pip install`
- [ ] Use heredoc for multi-line Python
- [ ] Verify output and handle errors
- [ ] Save intermediate results to files if multi-step