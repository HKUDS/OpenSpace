---
name: sandbox-fallback-python-e5d7ae
description: Fallback to run_shell with Python heredoc when execute_code_sandbox fails
---

# Python Sandbox Fallback Pattern

## When to Use

Use this pattern when `execute_code_sandbox` fails with e2b/sandbox initialization errors, especially when running Python code that requires external libraries like reportlab, pandas, numpy, etc.

## Common Failure Indicators

Detect these error patterns to trigger the fallback:
- `e2b` initialization errors
- `sandbox` creation/creation timeout failures
- `container` startup errors
- Connection/refusal errors from sandbox service

## The Fallback Pattern

### Basic Heredoc Approach

For short scripts, use a Python heredoc directly in `run_shell`:

```python
run_shell(
    command="""python3 << 'EOF'
import sys
print(f"Python version: {sys.version}")
# Your code here
EOF
""",
    timeout=60
)
```

### File-Based Approach for Complex Scripts

For longer scripts or when you need to preserve state across multiple executions:

```python
# Step 1: Write the script to a temporary file
script_content = '''
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
import os

# Your code here
'''

run_shell(command=f"cat > /tmp/script.py << 'SCRIPT_EOF'\n{script_content}\nSCRIPT_EOF")

# Step 2: Execute the script
result = run_shell(command="python3 /tmp/script.py", timeout=120)
```

## Key Considerations

### Timeouts
- `run_shell` typically needs longer timeouts than `execute_code_sandbox`
- Use `timeout=60` for simple scripts
- Use `timeout=120` or higher for library-heavy operations (PDF generation, data processing)

### Library Availability
- Verify required libraries are installed in the shell environment
- Install missing libraries first if needed:
  ```python
  run_shell(command="pip install reportlab", timeout=60)
  ```

### Error Handling
- Capture and inspect `run_shell` output for Python errors
- Check stderr for import failures or runtime exceptions

### Working Directory
- Files created via `run_shell` are in the current working directory by default
- Use absolute paths or `os.getcwd()` to reference files reliably

## Example: PDF Generation with reportlab

```python
# FALLBACK VERSION (when execute_code_sandbox fails):

pdf_code = """
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib.units import inch

def create_memo(filename):
    c = canvas.Canvas(filename, pagesize=letter)
    c.drawString(1*inch, 10*inch, "Legal Memorandum")
    c.save()
    print(f"Created {filename}")

create_memo('/workspace/output.pdf')
"""

# Write script to file
run_shell(command=f"cat > /tmp/create_pdf.py << 'PYEOF'\n{pdf_code}\nPYEOF")

# Execute and verify
run_shell(command="python3 /tmp/create_pdf.py", timeout=120)

# Verify output exists
run_shell(command="ls -la /workspace/output.pdf", timeout=30)
```

## Comparison Table

| Aspect | execute_code_sandbox | run_shell (fallback) |
|--------|---------------------|---------------------|
| Isolation | High (containerized) | Low (host environment) |
| Reliability | Can fail with e2b errors | More reliable |
| Library Access | Pre-configured sandbox | Depends on host |
| Timeout | Default 30s | Configurable |
| File Access | ARTIFACT_PATH convention | Direct filesystem |

## Migration Checklist

When switching from `execute_code_sandbox` to `run_shell`:

- [ ] Increase timeout values (typically 2-4x)
- [ ] Convert ARTIFACT_PATH references to actual file paths
- [ ] Add explicit file verification steps after execution
- [ ] Handle potential library import errors
- [ ] Test script execution in shell environment first