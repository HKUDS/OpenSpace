---
name: pdf-generation-troubleshooting
description: Systematic fallback workflow for PDF generation through pandoc, reportlab, and fpdf2 with installation verification
---

# PDF Generation Troubleshooting Workflow

This skill provides a systematic approach to generating PDFs when multiple tools may be unavailable. Follow this fallback sequence to ensure PDF generation succeeds.

## Workflow Overview

1. **Try pandoc first** (simplest for markdown/HTML conversion)
2. **Fallback to reportlab** (Python library for programmatic PDFs)
3. **Fallback to fpdf2** (alternative Python PDF library)

Each step includes installation verification before execution.

## Step 1: Attempt pandoc Conversion

Use pandoc for markdown or HTML to PDF conversion. Check availability first:

```bash
# Check if pandoc is available
which pandoc || echo "pandoc not found"
```

If available, convert:

```bash
pandoc input.md -o output.pdf
```

If pandoc fails or is unavailable, proceed to Step 2.

## Step 2: Attempt reportlab

### Check Installation

```bash
python3 -c "import reportlab" 2>/dev/null && echo "reportlab available" || echo "reportlab not found"
```

### Install if Needed

```bash
pip install reportlab
```

### Create PDF Script

Use `write_file` to create a Python script:

```python
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

def create_pdf(filename, content):
    c = canvas.Canvas(filename, pagesize=letter)
    c.drawString(100, 750, content)
    c.save()

if __name__ == "__main__":
    create_pdf("output.pdf", "Your content here")
```

### Execute Script

Use `run_shell` with proper directory context:

```bash
cd /path/to/working/directory && python3 script.py
```

If reportlab fails, proceed to Step 3.

## Step 3: Attempt fpdf2

### Check Installation

```bash
python3 -c "from fpdf import FPDF" 2>/dev/null && echo "fpdf2 available" || echo "fpdf2 not found"
```

### Install if Needed

```bash
pip install fpdf2
```

### Create PDF Script

```python
from fpdf import FPDF

def create_pdf(filename, text):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", size=12)
    pdf.cell(200, 10, txt=text, ln=True)
    pdf.output(filename)

if __name__ == "__main__":
    create_pdf("output.pdf", "Your content here")
```

### Execute Script

```bash
cd /path/to/working/directory && python3 script.py
```

## Decision Matrix

| Tool | Best For | Limitations |
|------|----------|-------------|
| pandoc | Markdown/HTML conversion | Requires external installation |
| reportlab | Complex layouts, graphics | Steeper learning curve |
| fpdf2 | Simple text PDFs | Limited formatting options |

## Error Handling Pattern

For each tool attempt:

1. Check availability/installation
2. Install if missing (`pip install <package>`)
3. Create script using `write_file`
4. Execute using `run_shell` with `cd <dir> && <command>`
5. Verify output file exists
6. If fails, move to next tool in sequence

## Example Fallback Implementation

```bash
# Pseudocode for full workflow
if command -v pandoc >/dev/null 2>&1; then
    pandoc input.md -o output.pdf && exit 0
fi

if python3 -c "import reportlab" 2>/dev/null; then
    # Create and run reportlab script
    exit 0
fi

# Final fallback: fpdf2
pip install fpdf2
# Create and run fpdf2 script
```

## Key Principles

- **Always verify** tool availability before attempting use
- **Install on-demand** when a tool is missing
- **Use write_file** for script creation (ensures clean, versionable code)
- **Use run_shell** with explicit directory context (`cd <dir> && <cmd>`)
- **Progressive fallback** ensures at least one method succeeds
- **Verify output** after each attempt before moving to next tool