---
name: document-gen-resilient-workflow
description: Robust document generation with engine fallback chain, error diagnostics, and Python alternatives
---

# Resilient Document Generation Workflow

## When to Use

Use this skill when document generation tasks encounter tool errors or failures, especially:
- `run_shell` commands return 'unknown error' without diagnostic details
- PDF generation fails with LaTeX encoding or missing engine errors
- You need fallback options when primary conversion methods fail
- **Workspace or path issues** cause file access problems
- Unicode/special characters cause rendering issues in PDF output
- `shell_agent` has failed multiple times on similar tasks

## Core Technique

Split document generation into **observable, verifiable steps** with explicit error capture and progressive fallback:

1. **Validate workspace** → Confirm current directory and write permissions
2. **Pre-flight checks** → Verify required tools (pandoc, LaTeX engines) are available
3. **Content creation** → Use `write_file` for source Markdown with full visibility
4. **Unicode sanitization** → Preprocess for PDF conversion when needed
5. **Progressive conversion** → Try multiple engines/methods until one succeeds
6. **Explicit error capture** → Redirect stderr to diagnose failures
7. **Verification** → Confirm output files exist and are readable

## ⚠️ Critical: Tool Error Patterns

**'unknown error' from run_shell**: This typically indicates:
- Sandbox execution context issues (not the command itself)
- Missing stderr capture preventing diagnosis
- Transient file access problems

**Solution**: Use explicit error capture with `2>&1` and verify tool availability first:
```
run_shell
command: pandoc --version 2>&1 || echo "PANDOC NOT AVAILABLE"
```

## Pre-Flight Checklist

Before starting conversion, verify your environment:

```markdown
## Pre-Flight Checks

### Check 1: Verify working directory
run_shell
command: pwd && ls -la

### Check 2: Verify pandoc availability
run_shell
command: pandoc --version 2>&1 | head -3 || echo "PANDOC MISSING"

### Check 3: Check LaTeX engines
run_shell
command: which pdflatex xelatex wkhtmltopdf 2>&1 || echo "Some engines missing"

### Check 4: Check Python libraries (fallback option)
run_shell
command: python3 -c "import fpdf; print('fpdf OK')" 2>&1 || echo "fpdf missing"
run_shell
command: python3 -c "import docx; print('python-docx OK')" 2>&1 || echo "python-docx missing"
```

## Complete Step-by-Step Workflow

### Step 1: Validate Workspace

Confirm you're in the correct directory and can write files:

```
run_shell
command: pwd && echo "Test write" > /tmp/workspace_test_$$.txt && ls -la /tmp/workspace_test_$$.txt && rm /tmp/workspace_test_$$.txt
```

**If this fails**, you may have directory permission issues. Use absolute paths for all files.

### Step 2: Create Source Content with write_file

Write document content as Markdown to a source file:

```
write_file
path: /tmp/document_source.md
content: |
  # Document Title
  
  ## Section 1
  Content here...
```

### Step 3: Unicode Sanitization (PDF Only)

**Only needed for PDF conversion via LaTeX engines**. DOCX and HTML handle Unicode natively.

Create a sanitized version replacing LaTeX-problematic characters:

```
write_file
path: /tmp/document_source_sanitized.md
content: |
  # Document Title
  
  [Same content with: em-dash → --, curly quotes → straight, → arrows → text, etc.]
```

**Common replacements** (see full table below):
| Original | Replace With |
|----------|--------------|
| `—` (em dash) | `--` |
| `" "` (curly quotes) | `" "` (straight) |
| `…` (ellipsis) | `...` |
| `→` `←` (arrows) | `->` `<-` |
| `✓` `✗` | `[x]` `[ ]` |
| `©` `®` `™` | `(c)` `(r)` `(tm)` |

**Alternative**: Use sanitization script (see Scripts section below).

### Step 4: Progressive Format Conversion

Try multiple methods in order of preference. **Capture stderr explicitly** for diagnosis.

#### Method A: DOCX Conversion (Most Reliable)

```
run_shell
command: pandoc /tmp/document_source.md -o output.docx 2>&1 && echo "SUCCESS" || echo "FAILED: $?"
```

If this fails, try with explicit UTF-8 flag:
```
run_shell
command: pandoc /tmp/document_source.md -f markdown+utf8 -o output.docx 2>&1
```

#### Method B: PDF Conversion (Progressive Fallback)

**Try engines in this order** until one succeeds:

```
run_shell
command: pandoc /tmp/document_source_sanitized.md -o output.pdf 2>&1 | tee /tmp/pdf_error.log && echo "PDF SUCCESS"
```

If Method B fails, check error log and try next engine:

```
run_shell
command: pandoc /tmp/document_source_sanitized.md --pdf-engine=xelatex -o output.pdf 2>&1 | tee /tmp/pdf_error.log && echo "PDF SUCCESS"
```

If xelatex fails:
```
run_shell
command: pandoc /tmp/document_source_sanitized.md --pdf-engine=wkhtmltopdf -o output.pdf 2>&1 | tee /tmp/pdf_error.log && echo "PDF SUCCESS"
```

#### Method C: Python Library Fallback (When pandoc Unavailable)

If all pandoc methods fail, use Python libraries:

**Option C1: FPDF2 for PDF**
```
execute_code_sandbox
language: python
code: |
  from fpdf import FPDF
  
  pdf = FPDF()
  pdf.add_page()
  pdf.set_font('Arial', 'B', 16)
  pdf.cell(40, 10, 'Document Title')
  pdf.ln(20)
  pdf.set_font('Arial', '', 12)
  pdf.multi_cell(0, 10, 'Your content here...')
  pdf.output('output.pdf')
  print('PDF created successfully')
```

**Option C2: python-docx for DOCX**
```
execute_code_sandbox
language: python
code: |
  from docx import Document
  
  doc = Document()
  doc.add_heading('Document Title', 0)
  doc.add_paragraph('Your content here...')
  doc.save('output.docx')
  print('DOCX created successfully')
```

### Step 5: Verify Output Files

Don't just check existence—verify files are valid:

```
run_shell
command: ls -lh output.* 2>&1
```

```
run_shell
command: file output.docx output.pdf 2>&1
```

For DOCX, verify content:
```
run_shell
command: unzip -p output.docx word/document.xml 2>&1 | head -50 || echo "Cannot read DOCX"
```

For PDF, check page count:
```
run_shell
command: pdfinfo output.pdf 2>&1 | grep Pages || echo "pdfinfo not available"
```

Alternative verification via Python:
```
execute_code_sandbox
code: |
  import os
  for f in ['output.docx', 'output.pdf', 'output.html']:
    if os.path.exists(f):
      size = os.path.getsize(f)
      print(f'{f}: {size} bytes')
    else:
      print(f'{f}: NOT FOUND')
```

## Complete Example: Generating a Report

```markdown
# Generate Quarterly Report (DOCX + PDF)

## Step 1: Pre-flight checks
run_shell
command: pandoc --version 2>&1 | head -1

## Step 2: Write Markdown source
write_file
path: /tmp/quarterly_report.md
content: |
  # Quarterly Business Report
  
  ## Executive Summary
  Q4 performance exceeded expectations...
  
  ## Key Metrics
  - Revenue: $1.2M
  - Growth: 15%
  
  ## Recommendations
  Continue current strategy...

## Step 3: Create sanitized version for PDF
write_file
path: /tmp/quarterly_report_sanitized.md
content: |
  # Quarterly Business Report
  
  ## Executive Summary
  Q4 performance exceeded expectations...
  
  [Rest of content with special chars replaced]

## Step 4: Generate DOCX
run_shell
command: pandoc /tmp/quarterly_report.md -o quarterly_report.docx 2>&1 && echo "DOCX OK"

## Step 5: Generate PDF (try xelatex for Unicode safety)
run_shell
command: pandoc /tmp/quarterly_report_sanitized.md --pdf-engine=xelatex -o quarterly_report.pdf 2>&1 && echo "PDF OK"

## Step 6: Verify outputs
run_shell
command: ls -lh quarterly_report.* && file quarterly_report.*
```

## Full Unicode Replacement Table

| Character | Unicode | Issue | Safe Replacement |
|-----------|---------|-------|------------------|
| `—` | U+2014 | LaTeX incompatible | `--` |
| `–` | U+2013 | LaTeX incompatible | `-` |
| `"` `"` | U+201C/U+201D | Encoding errors | `"` (straight) |
| `'` `'` | U+2018/U+2019 | Encoding errors | `'` (straight) |
| `…` | U+2026 | May not render | `...` |
| `→` | U+2192 | LaTeX incompatible | `->` or `to` |
| `←` | U+2190 | LaTeX incompatible | `<-` |
| `↑` `↓` | U+2191/U+2193 | LaTeX incompatible | `^` `v` |
| `•` | U+2022 | Font-dependent | `-` or `*` |
| `✓` | U+2713 | May not render | `[x]` |
| `✗` | U+2717 | May not render | `[ ]` |
| `★` `●` | U+2605/U+25CF | May not render | `*` `-` |
| `©` | U+00A9 | May require packages | `(c)` |
| `®` | U+00AE | May require packages | `(r)` |
| `™` | U+2122 | May require packages | `(tm)` |
| `é` `ñ` `ü` | Various | Font-dependent | Use xelatex or replace |

## Reusable Sanitization Script

Save this script for repeated use:

```bash
#!/bin/bash
# sanitize_for_pdf.sh - Replace problematic unicode chars for LaTeX/PDF
set -e
if [ -z "$1" ]; then
  echo "Usage: $0 <input.md> [output.md]"
  exit 1
fi
INPUT="$1"
OUTPUT="${2:-${1%.md}_sanitized.md}"

sed -e 's/—/--/g' \
    -e 's/–/-/g' \
    -e 's/"\([^"]*\)"/"\1"/g' \
    -e "s/'\([^']*\)'/\'\1\'/g" \
    -e 's/…/.../g' \
    -e 's/→/->/g' \
    -e 's/←/<-/g' \
    -e 's/↑/^/g' \
    -e 's/↓/v/g' \
    -e 's/•/-/g' \
    -e 's/✓/[x]/g' \
    -e 's/✗/[ ]/g' \
    -e 's/★/*/g' \
    -e 's/©/(c)/g' \
    -e 's/®/(r)/g' \
    -e 's/™/(tm)/g' \
    "$INPUT" > "$OUTPUT"

echo "Sanitized: $INPUT -> $OUTPUT"
```

Usage:
```
run_shell
command: chmod +x sanitize_for_pdf.sh && ./sanitize_for_pdf.sh /tmp/document_source.md /tmp/document_source_sanitized.md
```

## Error Diagnosis Guide

### Error: "unknown error" from run_shell

**Diagnosis:**
```
run_shell
command: pandoc --version 2>&1
run_shell
command: echo "test" | pandoc -f markdown -t plain 2>&1
```

**If pandoc works in isolation**, the issue is likely:
- File path problems (use absolute paths)
- Sandbox context issues (retry or use shell_agent)
- Transient errors (retry the same command)

### Error: "LaTeX not found" or "pdflatex: command not found"

**Solutions in order:**
1. Try xelatex: `--pdf-engine=xelatex`
2. Try wkhtmltopdf: `--pdf-engine=wkhtmltopdf`
3. Install LaTeX: `apt-get install texlive-latex-recommended texlive-fonts-recommended`
4. Use Python fpdf2 fallback (see Step 4, Method C)

### Error: "Encoding error" or "Invalid UTF-8"

**Solutions:**
1. Add UTF-8 flag: `pandoc -f markdown+utf8`
2. Check file encoding: `file -i source.md`
3. Re-save with explicit UTF-8: 
   ```
   run_shell
   command: iconv -f UTF-8 -t UTF-8 source.md -o source_utf8.md
   ```
4. Use sanitization step

### Error: PDF generated but empty or garbled

**Diagnosis:**
```
run_shell
command: pdfinfo output.pdf 2>&1
```
```
execute_code_sandbox
code: |
  import fitz  # PyMuPDF
  doc = fitz.open('output.pdf')
  print(f'Pages: {doc.page_count}')
  if doc.page_count > 0:
      page = doc[0]
      print(f'Text: {page.get_text()[:200]}')
```

**Solutions:**
1. Use xelatex engine with Unicode content
2. Check sanitization step (if using pdflatex)
3. Verify source markdown is valid

### Error: DOCX file won't open or shows error

**Diagnosis:**
```
run_shell
command: unzip -l output.docx 2>&1 | head -10
```

**Solutions:**
1. Add `--reference-doc` with valid template
2. Ensure markdown syntax is valid
3. Try pandoc verbose mode: `pandoc -v input.md -o output.docx 2>&1`

## Workspace Troubleshooting

### Files appearing in wrong directory

**Diagnosis:**
```
run_shell
command: pwd && find /tmp -name "*.docx" -o -name "*.pdf" 2>/dev/null | head -10
```

**Solutions:**
1. Use absolute paths in all commands
2. Explicitly change to target directory: `cd /path/to/workspace && ...`
3. After generation, copy files to correct location:
   ```
   run_shell
   command: cp /tmp/output.docx ./output.docx
   ```

### Permission denied errors

**Solutions:**
1. Use /tmp directory for intermediate files
2. Copy final output to target location
3. Check disk space: `df -h .`

## When to Use shell_agent vs Manual Workflow

| Situation | Recommended Approach |
|-----------|---------------------|
| Simple DOCX generation, no special chars | `shell_agent` (faster) |
| PDF with Unicode/symbols | Manual workflow with sanitization |
| Multiple 'unknown error' failures | Manual workflow with error capture |
| Need to debug conversion issues | Manual workflow (visible steps) |
| Time-critical, simple format | `shell_agent` |
| Critical document, must succeed | Manual workflow with fallbacks |

## Alternative: Hybrid Approach

For reliability with less manual effort:

```markdown
## Hybrid Workflow

1. **First attempt**: `shell_agent` with clear task description

2. **On first error**: Switch to manual workflow:
   - `write_file` for source
   - `run_shell` with error capture (2>&1)
   - Verify with explicit checks

3. **On repeated errors**: Use Python library fallback (execute_code_sandbox)

This balances speed with reliability.
```

## Related Skills

- `workspace-path-troubleshooting`: For diagnosing file location issues
- `pdf-verification-cli`: For validating generated PDFs
- `write-file-fallback-report`: For content-first document creation when web sources fail

## Quick Reference Card

```
QUICK START: Generate Document in 3 Formats

1. Write source: write_file → /tmp/doc.md

2. Sanitize (PDF only): 
   Replace: — → --, "" → "", … → ...

3. Convert with error capture:
   DOCX: pandoc /tmp/doc.md -o out.docx 2>&1
   PDF:  pandoc /tmp/doc_sanitized.md --pdf-engine=xelatex -o out.pdf 2>&1
   HTML: pandoc /tmp/doc.md -o out.html 2>&1

4. Verify: ls -lh out.* && file out.*

5. If pandoc fails: Use execute_code_sandbox with fpdf2 or python-docx
```
