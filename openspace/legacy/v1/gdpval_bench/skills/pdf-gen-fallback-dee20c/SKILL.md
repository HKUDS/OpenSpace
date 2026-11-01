---
name: pdf-gen-fallback-dee20c
description: Fallback workflow for document generation when code sandbox returns opaque errors
---

# PDF Generation Fallback Workflow

## Purpose

When `execute_code_sandbox` returns opaque or unhelpful errors during document/PDF generation, use `run_shell` with direct `python -c` execution as a reliable alternative. This pattern includes library availability checks and explicit error handling.

## When to Use

- `execute_code_sandbox` fails with unclear error messages during document generation
- PDF, report, or document creation tasks are failing unexpectedly
- You need more control over the Python execution environment

## Step-by-Step Instructions

### 1. Check Library Availability

Before attempting generation, verify required libraries are installed:

```shell
run_shell: python -c "import reportlab; print('reportlab OK')"
run_shell: python -c "import fpdf; print('fpdf OK')"
run_shell: python -c "import PyPDF2; print('PyPDF2 OK')"
```

If a library is missing, install it:

```shell
run_shell: pip install reportlab --quiet
```

### 2. Execute Generation via run_shell

Instead of `execute_code_sandbox`, use `run_shell` with `python -c`:

```shell
run_shell: python -c "
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
c = canvas.Canvas('output.pdf', pagesize=letter)
c.drawString(100, 750, 'Document Content')
c.save()
print('PDF generated successfully')
"
```

### 3. Handle Errors Explicitly

Wrap generation code with try/except and capture stderr:

```shell
run_shell: python -c "
import sys
try:
    # Your generation code here
    from reportlab.pdfgen import canvas
    c = canvas.Canvas('output.pdf')
    c.drawString(100, 750, 'Content')
    c.save()
    print('SUCCESS: PDF created')
    sys.exit(0)
except Exception as e:
    print(f'ERROR: {type(e).__name__}: {e}', file=sys.stderr)
    sys.exit(1)
" 2>&1
```

### 4. Verify Output File

After generation, confirm the file was created:

```shell
run_shell: ls -la output.pdf
run_shell: file output.pdf
```

### 5. Fallback Hierarchy

If the first approach fails, try alternatives in this order:

1. `reportlab` - Full-featured PDF generation
2. `fpdf` - Simpler PDF library
3. `wkhtmltopdf` - HTML to PDF conversion
4. Direct text/Markdown output as last resort

## Example: Complete Workflow

```shell
# Step 1: Check library
run_shell: python -c "import reportlab; print('OK')"

# Step 2: Generate with error handling
run_shell: python -c "
import sys
try:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    c = canvas.Canvas('report.pdf', pagesize=letter)
    c.setTitle('Surveillance Report')
    c.drawString(100, 750, 'Revised Surveillance Report')
    c.drawString(100, 730, 'Summary: Key findings documented')
    c.save()
    print('SUCCESS')
except Exception as e:
    print(f'FAILED: {e}', file=sys.stderr)
    sys.exit(1)
"

# Step 3: Verify
run_shell: test -f report.pdf && echo 'File exists' || echo 'File missing'
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Module not found | `pip install <module> --quiet` |
| Permission denied | Check output directory permissions |
| Font errors | Use default fonts or install font packages |
| Encoding issues | Specify encoding explicitly in string operations |

## Best Practices

- Always capture both stdout and stderr for debugging
- Exit with proper status codes (0 for success, 1 for failure)
- Keep generation code concise when using `python -c`
- For complex generation, write a temporary `.py` file instead
- Test library availability before attempting generation