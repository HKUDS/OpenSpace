---
name: python-execution-fallback-9d4989
description: Fallback method for executing Python when execute_code_sandbox fails with unknown errors
---

# Python Execution Fallback

## When to Use

Use this skill when `execute_code_sandbox` returns 'unknown error' or fails unexpectedly, even on simple Python code like print statements.

## Solution

Switch to using `run_shell` with Python heredoc syntax instead of `execute_code_sandbox`.

### Basic Syntax

```bash
python3 << 'EOF'
# Your Python code here
print("Hello World")
result = 2 + 2
print(f"Result: {result}")
EOF
```

### How to Call

Instead of:
```python
execute_code_sandbox(code="print('Hello')")
```

Use:
```python
run_shell(command="python3 << 'EOF'\nprint('Hello')\nEOF")
```

For multi-line code, build the command string:
```python
code = """
import json
data = {"status": "ok", "count": 42}
print(json.dumps(data))
"""
run_shell(command=f"python3 << 'EOF'\n{code}\nEOF")
```

## Why This Works

- `run_shell` executes commands directly in the shell environment, bypassing sandbox issues
- Heredoc syntax (`<< 'EOF'`) allows clean multi-line Python code without complex escaping
- Single quotes around EOF prevent unwanted shell variable expansion
- More reliable for simple to moderate Python execution tasks

## Guidelines

1. **Always use single quotes around EOF** (`'EOF'`) to prevent variable expansion
2. **Test with a simple print statement first** to verify Python is available
3. **For longer scripts**, consider writing to a `.py` file first, then executing:
   ```python
   run_shell(command="python3 /path/to/script.py")
   ```
4. **Capture stdout** from run_shell to get Python output
5. **Handle errors** by checking run_shell's stderr output

## Example: Complete Workflow

```python
# Step 1: Try execute_code_sandbox
try:
    result = execute_code_sandbox(code="print(2+2)")
except Exception as e:
    # Step 2: Fallback to run_shell with heredoc
    result = run_shell(command="python3 << 'EOF'\nprint(2+2)\nEOF")
    # Parse stdout to get the output
    output = result.stdout.strip()
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Python not found | Use `which python3` to verify installation |
| Heredoc not working | Ensure single quotes around EOF delimiter |
| Complex imports fail | Install required packages or write to .py file |
| Output not captured | Check run_shell stdout, not return value |