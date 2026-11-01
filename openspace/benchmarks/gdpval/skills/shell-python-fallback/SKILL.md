---
name: shell-python-fallback
description: Use run_shell with embedded Python heredoc as reliable fallback when code execution tools fail
---

# Shell Python Fallback

## When to Use

Use this pattern when `execute_code_sandbox` or `shell_agent` tools consistently fail with "unknown error" for tasks such as:
- Data processing and transformation
- PDF generation (reportlab, etc.)
- File parsing and manipulation
- Any Python script execution that requires reliability

## The Pattern

Instead of using code execution tools, embed your Python script directly in a `run_shell` command using a heredoc:

```bash
python3 << 'EOF'
# Your Python code here
import sys
print("Hello from embedded Python")
EOF
```

## Step-by-Step Instructions

1. **Identify the failure**: When `execute_code_sandbox` or `shell_agent` returns "unknown error" repeatedly (2+ attempts), switch to this fallback.

2. **Write your Python script**: Prepare the complete Python code you need to execute.

3. **Embed in run_shell**: Use `run_shell` with a heredoc syntax:

```
Command: python3 << 'EOF'
import json
# Your complete script here
data = {"key": "value"}
print(json.dumps(data))
EOF
```

4. **Handle multi-line scripts**: For longer scripts, ensure proper indentation is preserved. The heredoc preserves whitespace exactly.

5. **Check output**: Parse the stdout from `run_shell` to verify success or capture errors.

## Example: PDF Generation

```bash
python3 << 'EOF'
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

c = canvas.Canvas("output.pdf", pagesize=letter)
c.drawString(100, 750, "Hello World")
c.save()
print("PDF created successfully")
EOF
```

## Example: Data Processing

```bash
python3 << 'EOF'
import csv
import json

data = []
with open('input.csv', 'r') as f:
    reader = csv.DictReader(f)
    for row in reader:
        data.append(row)

with open('output.json', 'w') as f:
    json.dump(data, f, indent=2)

print(f"Processed {len(data)} records")
EOF
```

## Best Practices

- **Quote the EOF delimiter** (`<< 'EOF'` not `<< EOF`) to prevent shell variable expansion in your Python code
- **Include error handling** in your Python script with try/except blocks
- **Print status messages** to stdout so you can verify execution succeeded
- **Use absolute paths** or ensure working directory is correct
- **Test incrementally**: Start with a minimal script, then expand

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Script doesn't run | Ensure `python3` is available in the shell environment |
| Indentation errors | Check that heredoc preserves spaces (use spaces, not tabs) |
| Module not found | Use `python3 -m pip install <module>` before running script |
| File not found | Use absolute paths or `pwd` to verify working directory |

## Why This Works

The `run_shell` tool executes commands directly in the system shell, bypassing the sandbox restrictions or internal errors that affect `execute_code_sandbox` and `shell_agent`. The heredoc approach allows multi-line Python scripts while keeping everything in a single shell command.