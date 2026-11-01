---
name: pdf-gen-fallback-501bcc
description: Fallback pattern for PDF/document generation when execute_code_sandbox fails with opaque errors
---

# PDF Generation Fallback Pattern

## When to Use

Apply this pattern when `execute_code_sandbox` returns opaque or unhelpful errors during PDF, DOCX, or other document generation tasks. This fallback uses `run_shell` with direct `python -c` execution for more reliable document generation.

## Step-by-Step Procedure

### 1. Detect Sandbox Failure

If `execute_code_sandbox` fails during document generation with errors like:
- Opaque runtime errors
- Missing library errors that don't resolve with import statements
- Silent failures with no output

Proceed to the fallback approach.

### 2. Check Library Availability

Before generating documents, verify the required Python libraries are available in the shell environment:

```bash
python -c "import reportlab; print('OK')" 2>&1 || echo "reportlab missing"
python -c "import fpdf; print('OK')" 2>&1 || echo "fpdf missing"
python -c "import pypdf; print('OK')" 2>&1 || echo "pypdf missing"
python -c "import docx; print('OK')" 2>&1 || echo "python-docx missing"
```

### 3. Install Missing Libraries (If Needed)

If a library is missing, install it via pip:

```bash
pip install reportlab fpdf2 pypdf python-docx
```

### 4. Execute Document Generation via run_shell

Use `run_shell` with `python -c` and multi-line strings for document generation:

```bash
python -c "
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

c = canvas.Canvas('output.pdf', pagesize=letter)
c.drawString(100, 750, 'Document Title')
c.drawString(100, 700, 'Content here')
c.save()
print('SUCCESS: PDF created')
"
```

For DOCX files:

```bash
python -c "
from docx import Document

doc = Document()
doc.add_heading('Document Title', 0)
doc.add_paragraph('Content here')
doc.save('output.docx')
print('SUCCESS: DOCX created')
"
```

### 5. Include Explicit Error Handling

Always wrap generation code with try/except for clear error reporting:

```bash
python -c "
try:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    
    c = canvas.Canvas('output.pdf', pagesize=letter)
    c.drawString(100, 750, 'Content')
    c.save()
    print('SUCCESS')
except ImportError as e:
    print(f'MISSING_LIBRARY: {e}')
    exit(1)
except Exception as e:
    print(f'GENERATION_ERROR: {e}')
    exit(1)
"
```

### 6. Verify Output File

After generation, confirm the file was created and is valid:

```bash
ls -la output.pdf
file output.pdf
test -f output.pdf && echo "File exists" || echo "File missing"
```

## Complete Example Workflow

```bash
# Step 1: Check and install library
python -c "import reportlab" 2>&1 || pip install reportlab

# Step 2: Generate PDF with error handling
python -c "
try:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    
    c = canvas.Canvas('report.pdf', pagesize=letter)
    c.setFont('Helvetica', 12)
    c.drawString(100, 750, 'Surveillance Report')
    c.drawString(100, 700, 'Generated via fallback pattern')
    c.save()
    print('SUCCESS: report.pdf created')
except Exception as e:
    print(f'ERROR: {e}')
    exit(1)
"

# Step 3: Verify
test -f report.pdf && echo "Verification: PASSED" || echo "Verification: FAILED"
```

## Why This Works

- **Direct Execution**: `run_shell` with `python -c` bypasses sandbox limitations that may affect document generation libraries
- **Explicit Errors**: Try/except blocks provide actionable error messages instead of opaque failures
- **Environment Control**: Shell execution gives more control over the Python environment and library paths
- **Verification**: Explicit file checks confirm success rather than assuming it

## Common Libraries

| Library | Purpose | Import Statement |
|---------|---------|------------------|
| reportlab | PDF generation | `from reportlab.pdfgen import canvas` |
| fpdf2 | PDF generation | `from fpdf import FPDF` |
| pypdf | PDF manipulation | `from pypdf import PdfReader` |
| python-docx | DOCX generation | `from docx import Document` |
| xlsxwriter | Excel files | `import xlsxwriter` |