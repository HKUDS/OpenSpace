---
name: shell-python-heredoc
description: Execute complex Python code via run_shell heredoc when execute_code_sandbox fails
---

# Shell Python Heredoc (Fallback Pattern)

## Overview

When `execute_code_sandbox` fails repeatedly (e.g., with "unknown error"), use `run_shell` with a Python heredoc as a reliable fallback for executing multi-line Python scripts with imports.

## When to Use

- `execute_code_sandbox` has failed 2+ times with unexplained errors
- You need to run Python code with multiple imports (e.g., ReportLab, pandas)
- The script is too complex for a one-liner but doesn't require persistence between calls
- You need better error visibility from raw stdout/stderr

## How to Use

### Basic Pattern

```python
run_shell(
    command="""python3 << 'EOF'
import sys
# Your Python code here
print("Hello from heredoc")
EOF""",
    timeout=300
)
```

### Key Syntax Rules

1. **Use `<< 'EOF'`** (with single quotes) to prevent shell variable expansion
2. **Indent Python code** relative to the heredoc delimiter
3. **End with `EOF`** on its own line (no leading whitespace)
4. **Capture output** from stdout/stderr for debugging

### Example: Generating a PDF with ReportLab

```python
run_shell(
    command="""python3 << 'EOF'
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

c = canvas.Canvas("output.pdf", pagesize=letter)
c.drawString(100, 750, "Hello PDF")
c.save()
print("PDF created successfully")
EOF""",
    timeout=300
)
```

### Example: Multi-file Data Processing

```python
run_shell(
    command="""python3 << 'EOF'
import pandas as pd
import json

# Load and process data
df = pd.read_csv("data.csv")
summary = df.describe()

# Output results
print(summary.to_string())
with open("summary.json", "w") as f:
    json.dump(summary.to_dict(), f)
print("Processing complete")
EOF""",
    timeout=300
)
```

## Advantages Over execute_code_sandbox

- **More reliable** for complex imports (no sandbox isolation issues)
- **Full stdout/stderr** access for debugging
- **Longer timeout** flexibility (60s default vs 30s)
- **Direct filesystem access** without ARTIFACT_PATH indirection

## Disadvantages

- **No persistent state** between calls (each heredoc is a fresh process)
- **Verbose** for simple one-liners
- **Shell escaping** may be needed for complex string literals

## Troubleshooting

| Issue | Solution |
|-------|----------|
| `EOF` not recognized | Ensure `EOF` is on its own line with no leading spaces |
| Import errors | Verify packages are installed in the shell environment |
| File not found | Use absolute paths or check working directory with `pwd` |
| Syntax errors | Check Python indentation within heredoc |

## See Also

- Use `execute_code_sandbox` for simpler scripts when available
- Use `shell_agent` for tasks requiring autonomous iteration and error recovery
- Consider writing scripts to `.py` files for complex, reusable code