---
name: python-shell-fallback
description: Fallback to shell execution when sandbox fails for library-dependent Python code
---

# Python Shell Fallback

When `execute_code_sandbox` fails for Python code that depends on external libraries, use `run_shell` with a heredoc Python script as a reliable fallback. The shell environment typically has better access to installed packages and system configuration than the sandbox.

## When to Use This Pattern

Apply this skill when:
- `execute_code_sandbox` returns errors related to missing libraries (e.g., `ModuleNotFoundError`)
- The code requires packages that may not be installed in the sandbox environment
- You need PDF generation, data visualization, or other library-heavy operations
- Previous sandbox execution attempts have failed with import errors

## How to Implement

### Step 1: Identify the Failure

Check if the sandbox error indicates a library/dependency issue:
- `ModuleNotFoundError: No module named 'xxx'`
- `ImportError: cannot import name 'xxx'`
- Similar package-related errors

### Step 2: Convert to Shell Execution

Rewrite the code execution using `run_shell` with a heredoc:

```bash
python3 << 'EOF'
# Your Python code here
import library_name
# ... rest of code
EOF
```

### Step 3: Execute via run_shell

Call `run_shell` with the heredoc Python script:

```
command: python3 << 'EOF'
import reportlab
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

# Your library-dependent code here
c = canvas.Canvas("output.pdf", pagesize=letter)
c.drawString(100, 750, "Hello World")
c.save()
EOF
```

## Example: PDF Generation Fallback

**Sandbox attempt (fails):**
```python
# execute_code_sandbox call fails with:
# ModuleNotFoundError: No module named 'reportlab'
```

**Shell fallback (works):**
```bash
python3 << 'EOF'
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

c = canvas.Canvas("legal_memo.pdf", pagesize=letter)
c.setTitle("Legal Memorandum")
c.drawString(100, 750, "CONFIDENTIAL LEGAL MEMORANDUM")
c.save()
print("PDF created successfully")
EOF
```

## Best Practices

1. **Use quoted heredoc (`<< 'EOF'`)** - Prevents variable expansion, keeping Python code intact
2. **Verify output** - Check stdout/stderr from `run_shell` to confirm success
3. **Handle file paths** - Ensure output files are written to accessible directories
4. **Test incrementally** - For complex scripts, test in smaller chunks first
5. **Include error handling** - Add try/except blocks to capture issues gracefully

## Alternative: Inline Python Command

For simple one-liners or short scripts:

```bash
python3 -c "import reportlab; print(reportlab.__version__)"
```

## Troubleshooting

If shell execution also fails:
- Check if Python3 is available: `which python3`
- Verify package installation: `pip3 list | grep package_name`
- Try `python` instead of `python3` (some systems differ)
- Consider installing the package: `pip3 install package_name`