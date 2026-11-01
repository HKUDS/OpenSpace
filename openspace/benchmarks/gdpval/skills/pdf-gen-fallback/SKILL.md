---
name: pdf-gen-fallback
description: Reliable PDF generation using shell-based Python execution when sandbox fails
---

# PDF Generation Fallback

This skill provides a reliable workflow for generating PDFs and documents when `execute_code_sandbox` returns opaque errors. Use direct `run_shell` with `python -c` as a fallback, with library availability checks and explicit error handling.

## When to Use

- `execute_code_sandbox` fails with cryptic error messages during document/PDF generation
- You need to generate PDFs, reports, or structured documents
- Standard library execution is unreliable for document creation tasks

## Step-by-Step Procedure

### Step 1: Check Library Availability

Before attempting generation, verify that required libraries are installed:

```bash
run_shell command="python -c \"import reportlab; print('reportlab available')\""
run_shell command="python -c \"import fpdf; print('fpdf available')\""
run_shell command="python -c \"import pypdf; print('pypdf available')\""
```

### Step 2: Attempt execute_code_sandbox First

Try the sandbox approach initially (it's cleaner for iteration):

```python
execute_code_sandbox code="""
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

c = canvas.Canvas("output.pdf", pagesize=letter)
c.drawString(100, 750, "Document Content")
c.save()
print("ARTIFACT_PATH:/path/to/output.pdf")
"""
```

### Step 3: Detect Opaque Errors

Watch for these failure indicators:
- Generic "execution failed" messages
- Import errors that don't match library availability checks
- Timeout errors during document rendering
- Missing artifact paths in output

### Step 4: Fall Back to run_shell

If sandbox fails, use direct shell execution:

```bash
run_shell command="python -c \"
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
c = canvas.Canvas('output.pdf', pagesize=letter)
c.drawString(100, 750, 'Document Content')
c.save()
print('PDF created successfully')
\""
```

### Step 5: Verify Output

After generation, confirm the file exists:

```bash
run_shell command="ls -la output.pdf && file output.pdf"
```

## Complete Example Workflow

```
1. Check libraries: run_shell "python -c \"import reportlab\""
2. Try sandbox: execute_code_sandbox with PDF generation code
3. If error detected: Fall back to run_shell with python -c
4. Verify: run_shell "ls -la *.pdf"
5. Read result: read_file filetype="pdf" file_path="output.pdf"
```

## Error Handling Tips

- Capture stderr from run_shell to diagnose issues
- Use absolute paths when possible to avoid working directory issues
- For complex documents, write the Python script to a file first, then execute
- Consider installing missing libraries: `run_shell command="pip install reportlab"`

## Library Alternatives

If reportlab is unavailable, try:
- `fpdf` - Simpler API, good for basic PDFs
- `weasyprint` - HTML/CSS to PDF conversion
- `pypdf` - PDF manipulation and merging

## Common Pitfalls

- Don't assume sandbox errors mean the approach is wrong—often it's环境 issues
- Always verify the PDF was created before proceeding
- Handle encoding issues explicitly (use utf-8)
- For images in PDFs, ensure image files are accessible to the execution context