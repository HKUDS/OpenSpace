---
name: code-execution-fallback
description: Handle code execution failures with fallback strategies and anchored workspace paths
---

# Code Execution Fallback & Workspace Anchoring

This skill provides a robust pattern for executing code when the primary method fails, combined with proper workspace path management to prevent file location errors.

## Core Techniques

### 1. Workspace Path Anchoring

Always establish and verify your working directory at the start of any task:

```python
# At the beginning of any code execution
import os
workspace_path = os.getcwd()
print(f"Working directory: {workspace_path}")
```

```bash
# In shell scripts
pwd
echo "Current directory: $(pwd)"
```

**Why:** Prevents files from being written to unexpected locations when agents switch between tools.

### 2. Execution Fallback Ladder

When `execute_code_sandbox` fails, follow this escalation pattern:

#### Level 1: Retry with Simpler Code
- Simplify the code structure
- Remove complex dependencies
- Add explicit error handling

#### Level 2: Use `run_shell` with Heredoc
When sandbox execution repeatedly fails, switch to shell execution:

```bash
python3 << 'EOF'
import os
import pandas as pd

# Your code here
data = {"col1": [1, 2, 3], "col2": ["a", "b", "c"]}
df = pd.DataFrame(data)
df.to_csv("output.csv", index=False)
print("File written successfully")
EOF
```

**Key points:**
- Use `<< 'EOF'` (quoted) to prevent variable expansion
- Include all imports and dependencies inline
- Add explicit success/failure messages

#### Level 3: Delegate to `shell_agent`
For complex multi-step tasks with error recovery needs:

```
Task: Create a data processing pipeline that reads CSV, transforms data, and outputs Excel
Requirements:
- Handle missing values
- Apply transformations
- Write to ./output/ directory
- Retry on transient errors
```

### 3. Explicit Path Management

Always use absolute or explicitly relative paths:

```python
# BAD - relies on implicit working directory
df.to_csv("output/data.csv")

# GOOD - explicit path anchoring
import os
base_path = os.getcwd()
output_dir = os.path.join(base_path, "output")
os.makedirs(output_dir, exist_ok=True)
df.to_csv(os.path.join(output_dir, "data.csv"))
```

```bash
# BAD
cd some_dir && python script.py

# GOOD
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
python script.py
```

## Decision Tree

```
execute_code_sandbox fails?
├── Yes, with syntax/import errors → Fix code, retry Level 1
├── Yes, with timeout/resource errors → Use Level 2 (run_shell heredoc)
├── Yes, with unknown/unclear errors → Use Level 3 (shell_agent)
└── No, success → Verify output file exists at expected path
```

## Common Failure Scenarios & Solutions

| Error Type | Likely Cause | Recommended Fallback |
|------------|--------------|---------------------|
| ModuleNotFoundError | Missing packages | run_shell with pip install first |
| Timeout | Long-running operation | shell_agent with progress tracking |
| PermissionError | Wrong directory | Verify workspace path, use explicit paths |
| Unknown error | Sandbox limitations | run_shell or shell_agent |

## Example: Robust File Generation

```python
# Step 1: Anchor workspace
import os
workspace = os.getcwd()
print(f"Workspace: {workspace}")

# Step 2: Create output directory explicitly
output_path = os.path.join(workspace, "deliverables")
os.makedirs(output_path, exist_ok=True)

# Step 3: Generate content with error handling
try:
    # Your generation logic here
    with open(os.path.join(output_path, "report.txt"), "w") as f:
        f.write("Content here")
    print(f"Success: File written to {output_path}")
except Exception as e:
    print(f"Error: {e}")
    # Signal to escalate to run_shell or shell_agent
    raise
```

## Anti-Patterns to Avoid

- ❌ Assuming current directory without verification
- ❌ Using relative paths like `../output/file.txt` without context
- ❌ Repeatedly retrying failed `execute_code_sandbox` without changing approach
- ❌ Not checking if output files exist after generation
- ❌ Mixing implicit and explicit path styles in same task

## Verification Checklist

After any code execution:
- [ ] Confirm working directory was verified at start
- [ ] Confirm output files exist at expected paths
- [ ] Confirm file contents are non-empty and valid
- [ ] If execution failed, escalate to next fallback level within 2 retries