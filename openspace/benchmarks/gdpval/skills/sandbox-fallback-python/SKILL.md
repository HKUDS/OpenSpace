---
name: sandbox-fallback-python
description: Fallback to run_shell with embedded Python when execute_code_sandbox fails due to e2b unavailability
---

# Sandbox Fallback: Python via run_shell

## When to Use This Skill

Apply this skill when `execute_code_sandbox` fails with errors indicating:
- e2b service unavailability
- Sandbox timeout or connection issues
- Code execution environment not accessible

This fallback allows you to execute Python code directly via shell, bypassing the sandbox infrastructure.

## Fallback Procedure

### Step 1: Detect Sandbox Failure

Identify fallback necessity from error messages like:
- "e2b unavailable"
- "sandbox connection failed"
- "execution environment error"
- Timeout errors during code execution

### Step 2: Switch to run_shell with Embedded Python

Instead of `execute_code_sandbox`, use `run_shell` with a Python heredoc or inline script:

```bash
python3 << 'EOF'
# Your Python code here
print("Hello from fallback execution")
EOF
```

### Step 3: Install Dependencies First

If your Python code requires packages not guaranteed to be installed:

```bash
pip install package_name package_name2 && python3 << 'EOF'
# Your Python code here
import package_name
print("Code with dependencies executed")
EOF
```

### Step 4: Handle File I/O

For scripts that need to read/write files:
- Use absolute or explicit relative paths
- Files persist in the workspace directory
- List directory contents with `ls` to verify file operations

```bash
python3 << 'EOF'
import os
# Write output to file
with open("output.txt", "w") as f:
    f.write("Results here")
# Verify
print(f"Working directory: {os.getcwd()}")
EOF
```

## Code Examples

### Simple Calculation
```bash
python3 << 'EOF'
result = sum(range(100))
print(f"Sum: {result}")
EOF
```

### With Package Installation
```bash
pip install requests -q && python3 << 'EOF'
import requests
response = requests.get("https://api.example.com/data")
print(response.json())
EOF
```

### With JSON Processing
```bash
python3 << 'EOF'
import json

data = {"key": "value", "count": 42}
output = json.dumps(data, indent=2)
print(output)

# Save to file
with open("data.json", "w") as f:
    f.write(output)
EOF
```

## Best Practices

1. **Use heredoc syntax** - `<< 'EOF'` prevents variable expansion issues
2. **Install quietly** - Use `pip install -q` to reduce output noise
3. **Error handling** - Add try/except blocks in Python code for robustness
4. **Verify execution** - Check stdout/stderr from run_shell to confirm success
5. **Keep code compact** - Long scripts are harder to debug in shell context

## Caveats

- **No persistent state** between run_shell calls (unlike execute_code_sandbox)
- **Longer execution time** for complex scripts due to Python interpreter startup
- **Limited debugging** - Cannot easily inspect variables between statements
- **Output size limits** - Very large outputs may be truncated

## When NOT to Use This Fallback

- If the task specifically requires sandbox isolation for security
- If you need persistent state across multiple code executions
- If the code execution is extremely long-running (consider backgrounding)