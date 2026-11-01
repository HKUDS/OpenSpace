---
name: reliable-script-execution
description: Execute Python scripts reliably using file-first approach instead of heredoc
---

# Reliable Script Execution

When executing Python code via shell commands, avoid inline heredoc execution which can fail unpredictably with 'unknown error'. Use this two-step file-first approach for more reliable script execution.

## Problem

Direct heredoc Python execution like:

```bash
python3 << 'EOF'
# complex code here
EOF
```

Can fail with 'unknown error', especially when:
- Script contains multiple lines or complex logic
- Special characters or quotes are present
- Working directory context matters

## Solution: File-First Approach

### Step 1: Write Python Script to File

Use `write_file` to save your Python code to a `.py` file:

```
write_file(path="./temp_script.py", content="""
import json

data = {"key": "value"}
print(json.dumps(data))
""")
```

### Step 2: Execute via Shell

Use `run_shell` with explicit working directory:

```
run_shell(command="python3 ./temp_script.py", timeout=60)
```

### Step 3: Clean Up (Optional)

Remove temporary files after execution:

```
run_shell(command="rm ./temp_script.py")
```

## Complete Example

**Task**: Generate a JSON report with calculations

```
# Step 1: Write the script
write_file(
    path="./generate_report.py",
    content="""
import json
from datetime import datetime

revenue = 500000.00
expenses = 379577.06
net_income = revenue - expenses

report = {
    "generated": datetime.now().isoformat(),
    "revenue": revenue,
    "expenses": expenses,
    "net_income": net_income
}

print(json.dumps(report, indent=2))
"""
)

# Step 2: Execute
run_shell(command="python3 ./generate_report.py", timeout=60)

# Step 3: Clean up
run_shell(command="rm ./generate_report.py")
```

## Best Practices

1. **Use descriptive filenames**: Name scripts according to their purpose (e.g., `calculate_pnl.py`, `transform_data.py`)

2. **Set appropriate timeouts**: For data processing scripts, use longer timeouts (60-300 seconds)

3. **Specify working directory**: If the script depends on relative paths, include `cd /path && python3 script.py`

4. **Handle errors gracefully**: Check shell output for errors and retry if needed

5. **Clean up temporary files**: Don't leave `.py` files cluttering the workspace unless they need to persist

## When to Use This Pattern

- Executing multi-line Python code via shell
- Scripts with complex string handling or special characters
- When heredoc execution has failed previously
- Any scenario where reliability matters more than brevity

## Anti-Pattern to Avoid

Do NOT rely on heredoc for production-critical scripts:

```bash
# Unreliable - may fail with 'unknown error'
python3 << 'EOF'
# Your code here
EOF
```

Use file-first approach instead for consistent, debuggable execution.