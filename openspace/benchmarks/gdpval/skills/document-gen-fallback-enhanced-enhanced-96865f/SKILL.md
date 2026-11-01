---
name: document-gen-resilient
description: Multi-path document generation with tool checks, Unicode handling, and Python fallbacks
---

# Resilient Document Generation Workflow

## When to Use

Use this skill when document generation tasks encounter errors or when you need reliable multi-format output:
- `shell_agent` returns unknown or unclear errors on document generation
- Generating documents in multiple formats (e.g., `.docx`, `.pdf`, `.html`)
- **PDF generation fails due to LaTeX encoding or missing dependencies**
- **You need to handle special characters, symbols, or non-ASCII text safely**
- Previous document generation attempts have failed

## Core Technique

Instead of delegating the entire document generation to `shell_agent`, manually split the workflow into discrete, observable steps with built-in fallbacks:
1. **Tool availability check** → Verify pandoc and PDF engines are available
2. **Content creation** → Use `write_file` to create source document (Markdown)
3. **Unicode assessment** → Determine if sanitization is needed based on target format
4. **Format conversion** → Try primary method, fall back to alternatives on failure
5. **Verification** → Check output files exist and are valid

## ⚠️ Format-Specific Unicode Guidance

**Critical**: Different formats handle Unicode differently. Plan accordingly:

| Format | Unicode Support | Sanitization Needed? | Recommended Engine |
|--------|-----------------|---------------------|-------------------|
| `.docx` | Excellent | No | pandoc (default) |
| `.html` | Excellent | No | pandoc (default) |
| `.pdf` (pdflatex) | Limited | **Yes** | pandoc + sanitization |
| `.pdf` (xelatex) | Good | Rarely | pandoc --pdf-engine=xelatex |
| `.pdf` (wkhtmltopdf) | Good | Rarely | pandoc --pdf-engine=wkhtmltopdf |
| `.pdf` (Python) | Excellent | No | fpdf2 or reportlab |

## Step-by-Step Workflow

### Step 0: Check Tool Availability

Before starting, verify which tools are available:

```
run_shell
command: which pandoc && echo "PANDOC: OK" || echo "PANDOC: MISSING"
```

```
run_shell
command: which pdflatex && echo "PDFLATEX: OK" || echo "PDFLATEX: MISSING"
```

```
run_shell
command: which xelatex && echo "XELATEX: OK" || echo "XELATEX: MISSING"
```

```
run_shell
command: python3 -c "import fpdf; print('FPDF2: OK')" 2>/dev/null || echo "FPDF2: MISSING"
```

**Decision Tree Based on Availability:**
- **pandoc + xelatex available** → Use pandoc with xelatex engine (best Unicode support)
- **pandoc + pdflatex only** → Use pandoc with sanitization (Step 2)
- **pandoc missing, Python available** → Use Python libraries (Step 3 Alternative)
- **All missing** → Install dependencies or use shell_agent with explicit instructions

### Step 1: Create Source Content with write_file

Write your document content as Markdown to a temporary source file. This gives you full visibility into the content being generated.

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

### Step 2: Assess and Handle Unicode (Conditional)

**Before PDF conversion**, check if your content contains problematic characters:

```
run_shell
command: grep -P '[\x{2014}\x{2013}\x{201C}\x{201D}\x{2026}]' /tmp/document_source.md && echo "UNICODE_DETECTED" || echo "UNICODE_CLEAN"
```

**If Unicode detected AND using pdflatex**, create a sanitized version:

```
write_file
path: /tmp/document_source_sanitized.md
content: |
  # Document Title
  
  ## Section 1
  Content here... (with all special chars replaced per table below)
```

**Common Problematic Characters:**

| Character | Issue | Safe Replacement |
|-----------|-------|------------------|
| `—` (em dash) | May not render | `--` or `-` |
| `–` (en dash) | May not render | `-` |
| `" "` (curly quotes) | Encoding errors | `" "` (straight quotes) |
| `' '` (curly apostrophe) | Encoding errors | `'` (straight apostrophe) |
| `…` (ellipsis) | May not render | `...` |
| `→` `←` `↑` `↓` (arrows) | LaTeX incompatibility | `->` `<-` `^` `v` |
| `✓` `✗` (checkmarks) | May not render | `[x]` `[ ]` |
| `©` `®` `™` | May require packages | `(c)` `(r)` `(tm)` |

**Note**: Keep the original unsanitized file for DOCX/HTML conversion (these formats handle Unicode better).

### Step 3: Convert to Target Formats with run_shell

Use `run_shell` with explicit commands for each format. Try primary method first, fall back on failure.

#### For DOCX (from original, no sanitization needed):

```
run_shell
command: pandoc /tmp/document_source.md -o output.docx
```

#### For PDF (try in order):

**Option A: xelatex (best Unicode support)**
```
run_shell
command: pandoc /tmp/document_source.md -o output.pdf --pdf-engine=xelatex
```

**Option B: wkhtmltopdf (good alternative)**
```
run_shell
command: pandoc /tmp/document_source.md -o output.pdf --pdf-engine=wkhtmltopdf
```

**Option C: pdflatex with sanitization**
```
run_shell
command: pandoc /tmp/document_source_sanitized.md -o output.pdf
```

**Option D: Python fpdf2 fallback**
```
run_shell
command: python3 -c "
from fpdf import FPDF
pdf = FPDF()
pdf.add_page()
pdf.set_font('Arial', size=12)
with open('/tmp/document_source.md', 'r', encoding='utf-8') as f:
    content = f.read()
pdf.multi_cell(0, 10, content)
pdf.output('output.pdf')
"
```

#### For HTML (from original, no sanitization needed):

```
run_shell
command: pandoc /tmp/document_source.md -o output.html
```

### Step 4: Verify Outputs

Check that files were created successfully:

```
run_shell
command: ls -lh output.docx output.pdf output.html 2>/dev/null
```

```
run_shell
command: file output.pdf 2>/dev/null
```

## Complete Example

```markdown
# Generate Negotiation Strategy Document

## Step 0: Check tools
run_shell
command: which pandoc && which xelatex && echo "TOOLS_OK" || echo "TOOLS_MISSING"

## Step 1: Write Markdown source
write_file
path: /tmp/negotiation_strategy.md
content: |
  # Negotiation Strategy
  
  ## Executive Summary
  [Content with original unicode...]
  
  ## Resolution Path
  [Content...]
  
  ## BATNA Analysis
  [Content...]

## Step 2: Check for Unicode (if PDF needed)
run_shell
command: grep -P '[\x{2014}\x{2013}\x{201C}\x{201D}]' /tmp/negotiation_strategy.md && echo "UNICODE_DETECTED" || echo "UNICODE_CLEAN"

## Step 3: Convert to DOCX (from original)
run_shell
command: pandoc /tmp/negotiation_strategy.md -o negotiation_strategy.docx

## Step 4: Convert to PDF (try xelatex first)
run_shell
command: pandoc /tmp/negotiation_strategy.md -o negotiation_strategy.pdf --pdf-engine=xelatex

## Step 5: If Step 4 failed, try wkhtmltopdf
run_shell
command: pandoc /tmp/negotiation_strategy.md -o negotiation_strategy.pdf --pdf-engine=wkhtmltopdf

## Step 6: Convert to HTML (from original)
run_shell
command: pandoc /tmp/negotiation_strategy.md -o negotiation_strategy.html

## Step 7: Verify
run_shell
command: ls -lh negotiation_strategy.*
```

## Advantages Over shell_agent

| Aspect | shell_agent | Manual Workflow |
|--------|-------------|-----------------|
| Error visibility | Opaque, may retry silently | Each step shows explicit output |
| Recovery | Automatic but may loop | Manual intervention at specific step |
| Debugging | Hard to isolate failure point | Clear which step failed |
| Unicode control | Agent may not handle encoding | You control character sanitization |
| Tool fallback | Single approach | Multiple fallback options |
| Control | Agent decides approach | You control each conversion |

## Common pandoc Commands

```bash
# Markdown to Word
pandoc input.md -o output.docx

# Markdown to PDF (requires LaTeX or wkhtmltopdf)
pandoc input.md -o output.pdf

# Markdown to PDF with Unicode-safe engine (better Unicode support)
pandoc input.md -o output.pdf --pdf-engine=xelatex

# Markdown to PDF with wkhtmltopdf (good alternative)
pandoc input.md -o output.pdf --pdf-engine=wkhtmltopdf

# Markdown to HTML
pandoc input.md -o output.html

# With custom template
pandoc input.md --template=template.html -o output.html

# With metadata
pandoc input.md -o output.pdf --metadata title="Document Title"
```

## Troubleshooting

### PDF Generation Failures

- **PDF generation fails with encoding error**: 
  - Use `--pdf-engine=xelatex` for better Unicode support
  - Or use `--pdf-engine=wkhtmltopdf` as alternative
  - Or create sanitized markdown file and use pdflatex

- **PDF generation fails: LaTeX not found**: 
  - Install LaTeX: `apt-get install texlive-latex-recommended texlive-fonts-recommended`
  - Or use `--pdf-engine=wkhtmltopdf` instead
  - Or fall back to Python fpdf2 (Step 3 Option D)

- **PDF generation fails: wkhtmltopdf not found**: 
  - Install: `apt-get install wkhtmltopdf`
  - Or use xelatex or pdflatex with sanitization
  - Or fall back to Python fpdf2

### DOCX Formatting Issues

- Add `--reference-doc=template.docx` for custom styles
- Ensure pandoc version is 2.0+ for best DOCX support

### Unicode/Encoding Errors in Any Format

- Add `-f markdown+utf8` to pandoc command
- Ensure source file is UTF-8 encoded: `file -i source.md`
- For PDF: prefer xelatex engine over pdflatex

### Special Characters Not Rendering in PDF

- Use xelatex engine: `--pdf-engine=xelatex`
- Or use the character replacement table above
- Or create sanitized version before PDF conversion

### Missing pandoc

- Install via `apt-get install pandoc` or `brew install pandoc`
- Or use Python libraries directly (fpdf2, reportlab)

### Python PDF Library Fallback

If pandoc is unavailable or consistently failing:

```bash
# Using fpdf2
python3 -c "
from fpdf import FPDF
pdf = FPDF()
pdf.add_page()
pdf.set_font('Arial', size=12)
pdf.multi_cell(0, 10, open('input.md').read())
pdf.output('output.pdf')
"

# Using reportlab (more control)
python3 -c "
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph
from reportlab.lib.styles import getSampleStyleSheet
doc = SimpleDocTemplate('output.pdf', pagesize=letter)
styles = getSampleStyleSheet()
story = [Paragraph(open('input.md').read(), styles['Normal'])]
doc.build(story)
"
```

## Unicode Sanitization Script (Optional)

For repeated use, create a reusable sanitization script:

```bash
#!/bin/bash
# sanitize_for_pdf.sh - Replace problematic unicode chars for LaTeX/PDF
if [ -z "$1" ]; then
  echo "Usage: $0 <input.md> [output.md]"
  exit 1
fi
INPUT="$1"
OUTPUT="${2:-${1%.md}_sanitized.md}"

sed -e 's/—/--/g' \
    -e 's/–/-/g' \
    -e 's/"([^"]*)"/"\1"/g' \
    -e "s/'([^']*)/'\1'/g" \
    -e 's/…/.../g' \
    -e 's/→/->/g' \
    -e 's/←/<-/g' \
    -e 's/✓/[x]/g' \
    -e 's/✗/[ ]/g' \
    -e 's/©/(c)/g' \
    -e 's/®/(r)/g' \
    -e 's/™/(tm)/g' \
    "$INPUT" > "$OUTPUT"

echo "Sanitized: $INPUT -> $OUTPUT"
```

Save as `sanitize_for_pdf.sh`, make executable with `chmod +x sanitize_for_pdf.sh`, then use:

```
run_shell
command: ./sanitize_for_pdf.sh /tmp/document_source.md /tmp/document_source_sanitized.md
```

## Decision Matrix: Which Approach to Use

| Scenario | Recommended Approach |
|----------|---------------------|
| DOCX only, any content | pandoc (no sanitization needed) |
| HTML only, any content | pandoc (no sanitization needed) |
| PDF, simple ASCII content | pandoc + pdflatex |
| PDF, Unicode content | pandoc + xelatex (preferred) |
| PDF, Unicode, xelatex unavailable | pandoc + sanitization + pdflatex |
| PDF, pandoc unavailable | Python fpdf2 or reportlab |
| Multiple formats needed | pandoc for all, sanitization for PDF only |
| shell_agent failing repeatedly | Use this manual workflow |

## When to Return to shell_agent

After successfully completing the manual workflow once, you can attempt `shell_agent` again for similar tasks, now with a known-working fallback if errors recur. For documents with heavy Unicode content requiring PDF output, consider always using the manual workflow with xelatex or sanitization.

## Related Skills

- `write-file-fallback-report`: Use when web sourcing fails, create from embedded knowledge
- `spreadsheet-direct-python`: Use Python libraries directly for spreadsheet operations
- Use this skill when your documents contain special characters, symbols, or non-ASCII text that may cause LaTeX/PDF conversion issues
