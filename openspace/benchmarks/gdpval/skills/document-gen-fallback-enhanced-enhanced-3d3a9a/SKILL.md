---
name: doc-gen-unicode-diagnostic
description: Systematic document generation with unicode sanitization, engine fallback chain, and explicit error diagnosis
---

# Document Generation with Diagnostic Workflow (Unicode-Safe)

## When to Use

Use this skill when document generation tasks fail or return unclear errors, especially when:
- Generating documents in multiple formats (`.docx`, `.pdf`, `.html`)
- **PDF generation fails with encoding/LaTeX errors**
- `shell_agent` returns "unknown error" without diagnostics
- Documents contain special characters, symbols, or non-ASCII text
- You need systematic error diagnosis rather than blind retries

## ⚠️ Critical: Why This Skill Exists

Recent executions show **43% effectiveness** because agents:
- Skip unicode sanitization before PDF conversion
- Don't try `xelatex` engine (better Unicode support)
- Don't capture stderr for proper diagnosis
- Exhaust iterations on repeated failures without systematic troubleshooting

**This skill fixes those gaps with mandatory steps.**

## Pre-Flight Validation (NEW - Required Step 0)

Before starting document generation, verify your toolchain:

```
run_shell
command: which pandoc && pandoc --version | head -3
```

```
run_shell
command: which pdflatex xelatex wkhtmltopdf 2>/dev/null || echo "Some engines missing"
```

```
run_shell
command: python3 -c "import sys; print(sys.version)"
```

If pandoc is missing, install it:
```
run_shell
command: apt-get update && apt-get install -y pandoc
```

For PDF support, install LaTeX engines:
```
run_shell
command: apt-get install -y texlive-latex-recommended texlive-fonts-recommended texlive-xetex
```

## Core Technique

**Manually split the workflow into observable, diagnostic steps:**
1. **Pre-flight** → Verify toolchain availability
2. **Content creation** → Use `write_file` for markdown source (visible content)
3. **Unicode sanitization** → **MANDATORY for PDF**: Create sanitized version
4. **Format conversion with fallback** → Try engines in order, capture stderr
5. **Verification** → Check outputs exist and validate content

## Unicode & LaTeX Compatibility (MANDATORY for PDF)

**PDF generation via LaTeX has limited Unicode support.** Before PDF conversion, you MUST sanitize:

| Character | Issue | Safe Replacement |
|-----------|-------|------------------|
| `—` (em dash) | LaTeX incompatibility | `--` |
| `–` (en dash) | LaTeX incompatibility | `-` |
| `" "` (curly quotes) | Encoding errors | `" "` (straight) |
| `' '` (curly apostrophe) | Encoding errors | `'` (straight) |
| `…` (ellipsis) | May not render | `...` |
| `→` `←` `↑` `↓` (arrows) | LaTeX incompatibility | `->` `<-` `^` `v` |
| `✓` `✗` (checkmarks) | May not render | `[x]` `[ ]` |
| `★` `●` (symbols) | May not render | `*` `-` |
| `©` `®` `™` | Require packages | `(c)` `(r)` `(tm)` |
| Non-ASCII (é, ñ, ü) | Font-dependent | Keep for xelatex, sanitize for pdflatex |

**DOCX and HTML handle Unicode natively** - use original markdown for these formats.

## Step-by-Step Workflow

### Step 0: Pre-Flight Validation

```
run_shell
command: which pandoc || (apt-get update && apt-get install -y pandoc)
```

```
run_shell
command: which xelatex || echo "xelatex not available - will use fallback"
```

### Step 1: Create Source Content with write_file

```
write_file
path: /tmp/document_source.md
content: |
  # Document Title
  
  ## Section 1
  Your content here with full Unicode support...
  
  ## Section 2
  Special chars: "quotes" — dashes … ellipsis ✓ checkmarks
```

### Step 2: Create Sanitized Version for PDF (**MANDATORY**)

**Option A: Manual sanitization with write_file**
```
write_file
path: /tmp/document_source_sanitized.md
content: |
  # Document Title
  
  ## Section 1
  Your content here...
  
  ## Section 2
  Special chars: "quotes" -- dashes ... ellipsis [x] checkmarks
```

**Option B: Automated sanitization script**

First create the script:
```
write_file
path: /tmp/sanitize_for_pdf.sh
content: |
  #!/bin/bash
  INPUT="$1"
  OUTPUT="${2:-${1%.md}_sanitized.md}"
  sed -e 's/—/--/g' -e 's/–/-/g' \
      -e 's/"([^"]*)"/"\1"/g' -e "s/'([^']*)'/\`\1\`/g" \
      -e 's/…/.../g' -e 's/→/->/g' -e 's/←/<-/g' \
      -e 's/✓/[x]/g' -e 's/✗/[ ]/g' \
      -e 's/©/(c)/g' -e 's/®/(r)/g' -e 's/™/(tm)/g' \
      "$INPUT" > "$OUTPUT"
  echo "Sanitized: $INPUT -> $OUTPUT"
```

```
run_shell
command: chmod +x /tmp/sanitize_for_pdf.sh && /tmp/sanitize_for_pdf.sh /tmp/document_source.md /tmp/document_source_sanitized.md
```

### Step 3: Convert to DOCX (from original, supports Unicode)

```
run_shell
command: pandoc /tmp/document_source.md -o output.docx 2>&1
```

**Capture stderr** with `2>&1` to see actual errors (not "unknown error").

### Step 4: Convert to PDF with Engine Fallback Chain (**CRITICAL**)

**Try engines in order: xelatex (best Unicode) → pdflatex → wkhtmltopdf**

```
run_shell
command: pandoc /tmp/document_source_sanitized.md -o output.pdf --pdf-engine=xelatex 2>&1
```

If xelatex fails, try pdflatex:
```
run_shell
command: pandoc /tmp/document_source_sanitized.md -o output.pdf --pdf-engine=pdflatex 2>&1
```

If pdflatex fails, try wkhtmltopdf:
```
run_shell
command: pandoc /tmp/document_source_sanitized.md -o output.pdf --pdf-engine=wkhtmltopdf 2>&1
```

**If ALL engines fail**, diagnose with:
```
run_shell
command: file /tmp/document_source_sanitized.md && head -20 /tmp/document_source_sanitized.md
```

### Step 5: Convert to HTML (from original)

```
run_shell
command: pandoc /tmp/document_source.md -o output.html 2>&1
```

### Step 6: Verify All Outputs

```
run_shell
command: ls -lh output.* && file output.*
```

```
run_shell
command: [ -f output.pdf ] && echo "PDF created: $(wc -c < output.pdf) bytes" || echo "PDF MISSING"
```

## Complete Example

```markdown
# Generate Client Report in Multiple Formats

## Step 0: Pre-flight
run_shell
command: which pandoc xelatex || echo "Checking toolchain..."

## Step 1: Write markdown source
write_file
path: /tmp/client_report.md
content: |
  # Client Investment Report
  
  ## Executive Summary
  Portfolio performance shows strong returns — up 15% this quarter...
  
  ## Risk Analysis
  Key metrics: "Sharpe ratio" ✓ passed … continuing analysis

## Step 2: Sanitize for PDF (MANDATORY)
write_file
path: /tmp/client_report_sanitized.md
content: |
  # Client Investment Report
  
  ## Executive Summary
  Portfolio performance shows strong returns -- up 15% this quarter...
  
  ## Risk Analysis
  Key metrics: "Sharpe ratio" [x] passed ... continuing analysis

## Step 3: Convert to DOCX (original unicode OK)
run_shell
command: pandoc /tmp/client_report.md -o client_report.docx 2>&1

## Step 4: Convert to PDF (sanitized, xelatex first)
run_shell
command: pandoc /tmp/client_report_sanitized.md -o client_report.pdf --pdf-engine=xelatex 2>&1

## Step 5: Convert to HTML (original unicode OK)
run_shell
command: pandoc /tmp/client_report.md -o client_report.html 2>&1

## Step 6: Verify
run_shell
command: ls -lh client_report.* && file client_report.*
```

## Error Diagnosis Decision Tree

When a conversion fails, **capture stderr** (`2>&1`) and diagnose:

```
If error contains "xelatex not found" or "LaTeX error":
  → Try next engine: --pdf-engine=pdflatex or --pdf-engine=wkhtmltopdf

If error contains "encoding" or "UTF-8":
  → Unicode not properly sanitized; re-check Step 2
  → Add -f markdown+utf8 to pandoc command

If error contains "template" or "class":
  → LaTeX template issue; try --pdf-engine=wkhtmltopdf

If error is "unknown error" (no stderr captured):
  → Re-run with 2>&1 to capture actual error message
  → Check if pandoc is installed: which pandoc

If wkhtmltopdf fails:
  → Install: apt-get install wkhtmltopdf
  → Or use Python alternative: reportlab or fpdf2
```

## Alternative: Python PDF Generation (When pandoc Fails)

If all pandoc engines fail, use Python libraries directly:

**Using fpdf2:**
```
run_shell
command: python3 -c "
from fpdf import FPDF
pdf = FPDF()
pdf.add_page()
pdf.set_font('Arial', '', 12)
pdf.cell(0, 10, 'Document Title')
pdf.output('output.pdf')
"
```

**Using reportlab:**
```
run_shell
command: python3 -c "
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
c = canvas.Canvas('output.pdf', pagesize=letter)
c.drawString(100, 750, 'Document Title')
c.save()
"
```

## Common pandoc Commands Reference

```bash
# Markdown to Word (Unicode-safe)
pandoc input.md -o output.docx

# Markdown to PDF with xelatex (BEST for Unicode)
pandoc input.md -o output.pdf --pdf-engine=xelatex

# Markdown to PDF with pdflatex (requires sanitization)
pandoc input.md -o output.pdf --pdf-engine=pdflatex

# Markdown to PDF with wkhtmltopdf (HTML-based, good fallback)
pandoc input.md -o output.pdf --pdf-engine=wkhtmltopdf

# Markdown to HTML
pandoc input.md -o output.html

# With metadata
pandoc input.md -o output.pdf --metadata title="Document Title"

# Force UTF-8 encoding
pandoc -f markdown+utf8 input.md -o output.pdf
```

## Troubleshooting Quick Reference

| Symptom | Likely Cause | Solution |
|---------|--------------|----------|
| "xelatex not found" | Missing LaTeX engine | `apt-get install texlive-xetex` or try `--pdf-engine=wkhtmltopdf` |
| "LaTeX error: encoding" | Unicode in source | Use sanitized markdown for PDF |
| "unknown error" (pandoc) | stderr not captured | Re-run with `2>&1` to see real error |
| PDF missing after conversion | All engines failed | Try Python (fpdf2/reportlab) as fallback |
| DOCX has garbled text | Encoding issue | Add `-f markdown+utf8` to pandoc command |
| HTML renders but PDF fails | LaTeX-specific issue | wkhtmltopdf engine usually works |

## Verification Checklist

Before marking task complete, verify:

- [ ] Pre-flight: pandoc installed and accessible
- [ ] Source markdown created with `write_file`
- [ ] Sanitized version created for PDF conversion
- [ ] DOCX generated from original (unicode preserved)
- [ ] PDF generated with xelatex (or fallback engine documented)
- [ ] All output files exist: `ls -lh output.*`
- [ ] File types verified: `file output.*`
- [ ] Content validated (spot-check with `read_file` if applicable)

## When to Use shell_agent Instead

After successfully completing this manual workflow:
- For simple DOCX-only tasks (no PDF needed)
- When toolchain is verified working
- For repetitive tasks with known-good content

**For documents with Unicode content requiring PDF, always use this manual workflow.**

## Related Skills

- `spreadsheet-direct-python`: For Excel/CSV generation with Python
- `pdf-verification-cli`: For verifying PDF page count and content after creation
