---
name: document-gen-resilient-workflow
description: Multi-engine document generation with cascading PDF fallbacks and robust Unicode handling
---

# Resilient Document Generation Workflow (Multi-Engine Fallback)

## When to Use

Use this skill when document generation tasks fail or when `shell_agent` returns unknown errors, especially for:
- Generating documents in multiple formats (`.docx`, `.pdf`, `.html`)
- **PDF generation fails due to missing LaTeX, encoding issues, or tool errors**
- Documents contain special characters, symbols, or non-ASCII text
- You need maximum reliability with automatic fallback options

## Core Technique

Split document generation into discrete, observable steps with **cascading fallbacks** for PDF generation:
1. **Content creation** → Use `write_file` to create source Markdown
2. **Unicode sanitization** → Use Python script for reliable character replacement
3. **Format conversion** → Try multiple PDF engines in sequence until one succeeds
4. **Verification** → Check file exists, has non-zero size, and is valid

## ⚠️ Unicode & PDF Engine Guide

Different PDF engines have different Unicode support:

| Engine | Unicode Support | Best For | Fallback Position |
|--------|----------------|----------|-------------------|
| `pdflatex` | Limited (ASCII-focused) | Simple documents | 1st (fastest) |
| `xelatex` | Full Unicode | Documents with non-ASCII | 2nd |
| `wkhtmltopdf` | Good Unicode | Web-style documents | 3rd |
| `reportlab` (Python) | Full control | Programmatic PDFs | 4th |
| `fpdf2` (Python) | Full control | Simple text PDFs | 5th (last resort) |

### Character Replacement Table

| Character | Issue | Safe Replacement |
|-----------|-------|------------------|
| `—` (em dash) | LaTeX incompatibility | `--` |
| `–` (en dash) | LaTeX incompatibility | `-` |
| `" "` (curly quotes) | Encoding errors | `" "` (straight) |
| `' '` (curly apostrophe) | Encoding errors | `'` (straight) |
| `…` (ellipsis) | May not render | `...` |
| `→` `←` `↑` `↓` | LaTeX incompatibility | `->` `<-` `^` `v` |
| `✓` `✗` | May not render | `[x]` `[ ]` |
| `★` `●` | May not render | `*` `-` |
| `©` `®` `™` | May require packages | `(c)` `(r)` `(tm)` |
| `é` `ñ` `ü` | Font-dependent | Keep for xelatex, replace for pdflatex |

## Step-by-Step Workflow

### Step 1: Create Source Content with write_file

Write your document content as Markdown to a source file:

```
write_file
path: /tmp/document_source.md
content: |
  # Document Title
  
  ## Section 1
  Content here...
  
  ## Section 2
  More content...
```

### Step 2: Sanitize Unicode with Python Script

**Create a reusable Python sanitizer** for reliable character replacement:

```
write_file
path: /tmp/sanitize_unicode.py
content: |
  #!/usr/bin/env python3
  import sys
  import re
  
  if len(sys.argv) < 2:
      print("Usage: sanitize_unicode.py <input.md> [output.md]")
      sys.exit(1)
  
  input_file = sys.argv[1]
  output_file = sys.argv[2] if len(sys.argv) > 2 else input_file.replace('.md', '_sanitized.md')
  
  replacements = {
      '—': '--',    # em dash
      '–': '-',     # en dash
      '"': '"',     # left curly quote
      '"': '"',     # right curly quote
      "'": "'",     # left curly apostrophe
      "'": "'",     # right curly apostrophe
      '…': '...',   # ellipsis
      '→': '->',    # right arrow
      '←': '<-',    # left arrow
      '↑': '^',     # up arrow
      '↓': 'v',     # down arrow
      '✓': '[x]',   # checkmark
      '✗': '[ ]',   # cross
      '★': '*',     # star
      '●': '-',     # bullet
      '©': '(c)',   # copyright
      '®': '(r)',   # registered
      '™': '(tm)',  # trademark
  }
  
  with open(input_file, 'r', encoding='utf-8') as f:
      content = f.read()
  
  for old, new in replacements.items():
      content = content.replace(old, new)
  
  with open(output_file, 'w', encoding='utf-8') as f:
      f.write(content)
  
  print(f"Sanitized: {input_file} -> {output_file}")
```

**Apply sanitization for PDF:**

```
run_shell
command: python3 /tmp/sanitize_unicode.py /tmp/document_source.md /tmp/document_source_sanitized.md
```

**Note**: Keep original for DOCX/HTML (these formats handle Unicode well).

### Step 3: Convert to Target Formats with Cascading Fallbacks

#### For DOCX (from original, no sanitization needed):

```
run_shell
command: pandoc /tmp/document_source.md -o output.docx
```

#### For PDF (try multiple engines in sequence):

**Attempt 1: pdflatex (fastest, limited Unicode)**
```
run_shell
command: pandoc /tmp/document_source_sanitized.md -o output.pdf --pdf-engine=pdflatex
```

**If pdflatex fails, Attempt 2: xelatex (full Unicode)**
```
run_shell
command: pandoc /tmp/document_source.md -o output.pdf --pdf-engine=xelatex
```

**If xelatex fails, Attempt 3: wkhtmltopdf (web-based)**
```
run_shell
command: pandoc /tmp/document_source.md -o output.pdf --pdf-engine=wkhtmltopdf
```

**If all pandoc engines fail, Attempt 4: Python reportlab**
```
write_file
path: /tmp/generate_pdf_reportlab.py
content: |
  #!/usr/bin/env python3
  from reportlab.lib.pagesizes import letter
  from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
  from reportlab.lib.styles import getSampleStyleSheet
  import markdown
  
  input_md = '/tmp/document_source.md'
  output_pdf = 'output.pdf'
  
  with open(input_md, 'r', encoding='utf-8') as f:
      md_content = f.read()
  
  html_content = markdown.markdown(md_content)
  
  doc = SimpleDocTemplate(output_pdf, pagesize=letter)
  styles = getSampleStyleSheet()
  story = []
  
  # Simple HTML to flowables (basic implementation)
  for line in html_content.split('\n'):
      if line.strip():
          story.append(Paragraph(line, styles['Normal']))
          story.append(Spacer(1, 6))
  
  doc.build(story)
  print(f"PDF created: {output_pdf}")
```

```
run_shell
command: python3 /tmp/generate_pdf_reportlab.py
```

**If reportlab fails, Attempt 5: Python fpdf2 (simpler)**
```
write_file
path: /tmp/generate_pdf_fpdf.py
content: |
  #!/usr/bin/env python3
  from fpdf import FPDF
  
  input_md = '/tmp/document_source.md'
  output_pdf = 'output.pdf'
  
  pdf = FPDF()
  pdf.add_page()
  pdf.set_font('Helvetica', '', 12)
  
  with open(input_md, 'r', encoding='utf-8') as f:
      for line in f:
          # Simple line-by-line, escape special chars
          safe_line = line.encode('latin-1', 'replace').decode('latin-1')
          pdf.cell(0, 10, safe_line[:180], ln=True)
  
  pdf.output(output_pdf)
  print(f"PDF created: {output_pdf}")
```

```
run_shell
command: python3 /tmp/generate_pdf_fpdf.py
```

#### For HTML (from original):

```
run_shell
command: pandoc /tmp/document_source.md -o output.html
```

### Step 4: Verify Outputs with Multiple Checks

**Check 1: Files exist and have size**
```
run_shell
command: ls -lh output.docx output.pdf output.html 2>/dev/null && echo "FILES_OK" || echo "FILES_MISSING"
```

**Check 2: Validate PDF is not corrupted**
```
run_shell
command: python3 -c "import fitz; doc=fitz.open('output.pdf'); print(f'PDF_VALID: {doc.page_count} pages')" 2>/dev/null || echo "PDF_CHECK_SKIPPED"
```

**Check 3: Validate DOCX**
```
run_shell
command: python3 -c "from docx import Document; d=Document('output.docx'); print(f'DOCX_VALID: {len(d.paragraphs)} paragraphs')" 2>/dev/null || echo "DOCX_CHECK_SKIPPED"
```

**Check 4: Read back content for manual verification**
```
read_file
filetype: md
file_path: output.html
```

## Complete Example

```markdown
# Generate Negotiation Strategy Document (Resilient Workflow)

## Step 1: Write Markdown source
write_file
path: /tmp/negotiation_strategy.md
content: |
  # Negotiation Strategy
  
  ## Executive Summary
  Content with original unicode characters...
  
  ## Resolution Path
  More content...

## Step 2: Sanitize for PDF
write_file
path: /tmp/sanitize_unicode.py
content: |
  [Python sanitizer script from Step 2 above]

run_shell
command: python3 /tmp/sanitize_unicode.py /tmp/negotiation_strategy.md /tmp/negotiation_strategy_sanitized.md

## Step 3: Convert to DOCX (original, Unicode-safe format)
run_shell
command: pandoc /tmp/negotiation_strategy.md -o negotiation_strategy.docx

## Step 4: Convert to PDF (try pdflatex first)
run_shell
command: pandoc /tmp/negotiation_strategy_sanitized.md -o negotiation_strategy.pdf --pdf-engine=pdflatex

## Step 4b: If pdflatex failed, try xelatex
run_shell
command: pandoc /tmp/negotiation_strategy.md -o negotiation_strategy.pdf --pdf-engine=xelatex

## Step 4c: If xelatex failed, try wkhtmltopdf
run_shell
command: pandoc /tmp/negotiation_strategy.md -o negotiation_strategy.pdf --pdf-engine=wkhtmltopdf

## Step 5: Convert to HTML (original)
run_shell
command: pandoc /tmp/negotiation_strategy.md -o negotiation_strategy.html

## Step 6: Verify all outputs
run_shell
command: ls -lh negotiation_strategy.* && echo "ALL_FILES_CREATED"

run_shell
command: python3 -c "import fitz; d=fitz.open('negotiation_strategy.pdf'); print(f'PDF: {d.page_count} pages')"
```

## Advantages Over shell_agent

| Aspect | shell_agent | Resilient Manual Workflow |
|--------|-------------|---------------------------|
| Error visibility | Opaque, may retry silently | Each step shows explicit output |
| PDF fallback | May give up after first failure | Cascading engine attempts |
| Debugging | Hard to isolate | Clear which engine/step failed |
| Unicode control | Agent-dependent | You control sanitization |
| Recovery | Automatic but may loop | Manual intervention at known points |
| Tool requirements | Assumes pandoc works | Multiple engine options |

## Troubleshooting by Error Type

### "LaTeX not found" or "pdflatex: command not found"
- **Solution**: Try `--pdf-engine=xelatex` or `--pdf-engine=wkhtmltopdf`
- **Install LaTeX**: `apt-get install texlive-latex-recommended texlive-fonts-recommended`
- **Or fallback to Python**: Use reportlab/fpdf2 approach

### "Encoding error" or "UnicodeDecodeError"
- **Solution**: Use sanitized markdown file (Step 2)
- **Or**: Add `-f markdown+utf8` to pandoc command
- **Or**: Try xelatex engine (better Unicode support)

### "wkhtmltopdf not found"
- **Solution**: Install via `apt-get install wkhtmltopdf` or fallback to Python

### "ModuleNotFoundError: No module named 'reportlab'"
- **Solution**: `pip install reportlab` or fallback to fpdf2

### "Invalid PDF" or corrupted output
- **Diagnosis**: Run `pdftotext output.pdf -` to check if text extracts
- **Solution**: Try different PDF engine from fallback chain

### DOCX formatting issues
- **Solution**: Add `--reference-doc=template.docx` for custom styles
- **Or**: Fix markdown structure in source file

### All pandoc commands fail
- **Check**: `pandoc --version` to verify installation
- **Fallback**: Use Python-based PDF generation (reportlab/fpdf2)
- **For DOCX**: Try `python-docx` library directly

## Pre-Flight Checks (Optional but Recommended)

Before starting, verify required tools are available:

```
run_shell
command: pandoc --version && echo "PANDOC_OK" || echo "PANDOC_MISSING"
run_shell
command: pdflatex --version && echo "PDFLATEX_OK" || echo "PDFLATEX_MISSING"
run_shell
command: python3 -c "import reportlab" && echo "REPORTLAB_OK" || echo "REPORTLAB_MISSING"
```

This helps you know which fallback path will be needed.

## Related Skills

- `document-gen-unicode-safe`: Parent skill with basic Unicode guidance
- `document-gen-fallback`: Original fallback without Unicode or multi-engine support
- Use this skill when you need maximum reliability with automatic fallbacks

## When to Return to shell_agent

After manually completing this workflow successfully once for a document type, you can attempt `shell_agent` for similar tasks with reduced risk (you now know the fallback path). However, for critical documents or those with heavy Unicode content, consider always using this resilient manual workflow.
