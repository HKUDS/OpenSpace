---
name: document-gen-dual-backend
description: Document generation with direct pandoc/ReportLab execution (lightweight default) and optional shell_agent fallback for complex scenarios
---

# Document Generation: Dual-Backend Workflow (Unicode-Safe)

## When to Use

**Default Approach: Lightweight Direct Execution (Recommended for 90% of tasks)**

For most document generation tasks, use direct `write_file` + `run_shell` **without** `shell_agent`:
- Generating documents in standard formats (`.docx`, `.pdf`, `.html`) from Markdown
- Content is straightforward with minimal special characters
- You already know the pandoc/ReportLab commands needed
- Quick single-format or multi-format output is needed

**Fallback Approach: shell_agent Delegation (For Complex Scenarios Only)**

Use `shell_agent` delegation only when:
- Automated fallback handling between backends requires complex logic
- Dynamic content generation needs programmatic decision-making
- You need to capture and analyze error messages for intelligent retry logic

### When shell_agent May Be Useful (Optional)

Only consider `shell_agent` delegation for:
- Complex error recovery requiring automated backend switching
- Dynamic workflows with conditional branching based on generation results

## Core Technique

**For simple tasks (default):** Use direct `write_file` + `run_shell` with pandoc or ReportLab (no shell_agent needed)

**For complex tasks requiring fallback logic:** Split the workflow into discrete, observable steps with **two PDF generation paths**:

**Path A (Pandoc)**: Best for Markdown-to-PDF conversion with rich text formatting
**Path B (ReportLab)**: Best for programmatic PDF generation without LaTeX dependencies

Workflow steps:
1. **Content creation** → Use `write_file` to create source document (Markdown)
2. **Choose PDF backend** → Decide between pandoc (rich formatting) or ReportLab (programmatic control)
3. **Unicode handling** → Apply sanitization for pandoc; ReportLab handles Unicode natively
4. **Format conversion** → Use `run_shell` with appropriate commands for each format
5. **Verification** → Check output files exist and are valid

## ⚠️ Backend Selection Guide

| Requirement | Recommended Backend | Rationale |
|-------------|---------------------|-----------|
| Markdown source with headers, lists, tables | **Pandoc** | Native Markdown parsing |
| Heavy Unicode/special characters | **ReportLab** | Native UTF-8 support, no sanitization needed |
| LaTeX not available | **ReportLab** | Pure Python, no external dependencies |
| Precise layout control (positions, graphics) | **ReportLab** | Programmatic canvas control |
| Quick DOCX + PDF + HTML batch | **Pandoc** | Single tool, multiple outputs |
| Tables with complex formatting | **Pandoc** | Better table rendering |
| Dynamic/charts/graphs in PDF | **ReportLab** | Drawing operations support |

## Unicode & LaTeX Compatibility Warning

**Critical for Pandoc Path**: PDF generation via pandoc typically uses LaTeX (`pdflatex` or `xelatex`), which has limited Unicode support. Common problematic characters include:

| Character | Issue | Safe Replacement |
|-----------|-------|------------------|
| `—` (em dash) | May not render | `--` or `-` |
| `–` (en dash) | May not render | `-` |
| `" "` (curly quotes) | Encoding errors | `" "` (straight quotes) |
| `' '` (curly apostrophe) | Encoding errors | `'` (straight apostrophe) |
| `…` (ellipsis) | May not render | `...` |
| `→` `←` `↑` `↓` (arrows) | LaTeX incompatibility | `->` `<-` `^` `v` |
| `✓` `✗` (checkmarks) | May not render | `[x]` `[ ]` |
| `★` `●` (symbols) | May not render | `*` `-` |
| `©` `®` `™` | May require packages | `(c)` `(r)` `(tm)` |
| Non-ASCII letters (é, ñ, ü) | Font-dependent | Use xeLaTeX or replace |

**ReportLab Path**: Handles Unicode natively. Pass content as UTF-8 strings; no character sanitization required.

## Step-by-Step Workflow

### Quick Start: Lightweight Direct Execution (Default for Most Tasks)

**This is the recommended approach for most document generation tasks. No shell_agent delegation needed:**

```
# Step 1: Write Markdown content
write_file
path: /tmp/doc.md
content: |
  # My Document
  
  Simple content here.

# Step 2: Convert with pandoc (direct)
run_shell
command: pandoc /tmp/doc.md -o output.docx

# Step 3: Verify
run_shell
command: ls -lh output.docx
```

**For PDF with potential Unicode issues:**

```
# Create sanitized version if needed
write_file
path: /tmp/doc_sanitized.md
content: |
  # My Document
  Content with safe ASCII characters only.

run_shell
command: pandoc /tmp/doc_sanitized.md -o output.pdf
```

**For Unicode-heavy content (use ReportLab directly):**

```
write_file
path: /tmp/gen.py
content: |
  from reportlab.platypus import SimpleDocTemplate, Paragraph
  from reportlab.lib.styles import getSampleStyleSheet
  doc = SimpleDocTemplate("output.pdf")
  styles = getSampleStyleSheet()
  story = [Paragraph("Unicode: é ñ ü 日本語", styles["Normal"])]
  doc.build(story)

run_shell
command: python /tmp/gen.py
```

---

### Full Workflow: Dual-Backend with Fallback (For Complex Cases)

### Step 1: Create Source Content with write_file

Write your document content as Markdown to a temporary source file:

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

### Step 2A: Pandoc Path - Sanitize Unicode (Pre-PDF Conversion)

**Before PDF conversion with pandoc**, create a sanitized version:

```
write_file
path: /tmp/document_source_sanitized.md
content: |
  # Document Title
  
  ## Section 1
  Content here... (with all special chars replaced per table above)
```

### Step 2B: ReportLab Path - Create Python Script

For ReportLab, create a Python script that generates PDF directly:

```
write_file
path: /tmp/generate_pdf.py
content: |
  from reportlab.lib.pagesizes import letter
  from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
  from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
  from reportlab.lib import colors
  from reportlab.pdfbase import pdfmetrics
  from reportlab.pdfbase.ttfonts import TTFont
  
  # Create PDF
  doc = SimpleDocTemplate("output.pdf", pagesize=letter)
  styles = getSampleStyleSheet()
  story = []
  
  # Add content
  story.append(Paragraph("Document Title", styles["Heading1"]))
  story.append(Spacer(1, 12))
  story.append(Paragraph("Section 1 content with Unicode: é ñ ü → ✓ …", styles["Normal"]))
  
  # Build PDF
  doc.build(story)
```

### Step 3: Convert to Target Formats

#### Option A: Pandoc Conversion (Use sanitized source for PDF)

```
run_shell
command: pandoc /tmp/document_source.md -o output.docx
```

```
run_shell
command: pandoc /tmp/document_source_sanitized.md -o output.pdf
```

```
run_shell
command: pandoc /tmp/document_source.md -o output.html
```

#### Option B: ReportLab Conversion (Direct PDF generation)

```
run_shell
command: python /tmp/generate_pdf.py
```

Then use pandoc for other formats from original source:

```
run_shell
command: pandoc /tmp/document_source.md -o output.docx
```

```
run_shell
command: pandoc /tmp/document_source.md -o output.html
```

### Step 4: Verify Outputs

```
run_shell
command: ls -lh output.docx output.pdf output.html
```

## Complete Examples

### Example 1: Pandoc-First Approach

```markdown
# Generate Report with Pandoc

## Step 1: Write Markdown source
write_file
path: /tmp/report.md
content: |
  # Quarterly Report
  
  ## Executive Summary
  Performance improved by 15% this quarter...
  
  ## Key Metrics
  - Revenue: $1.2M
  - Growth: +15%
  - Customers: 500+

## Step 2: Create sanitized version for PDF
write_file
path: /tmp/report_sanitized.md
content: |
  # Quarterly Report
  
  ## Executive Summary
  Performance improved by 15% this quarter...
  
  ## Key Metrics
  - Revenue: $1.2M
  - Growth: +15%
  - Customers: 500+

## Step 3: Convert to DOCX (from original)
run_shell
command: pandoc /tmp/report.md -o quarterly_report.docx

## Step 4: Convert to PDF (from sanitized)
run_shell
command: pandoc /tmp/report_sanitized.md -o quarterly_report.pdf

## Step 5: Convert to HTML (from original)
run_shell
command: pandoc /tmp/report.md -o quarterly_report.html

## Step 6: Verify
run_shell
command: ls -lh quarterly_report.*
```

### Example 2: ReportLab-First Approach (Heavy Unicode)

```markdown
# Generate Unicode-Heavy Document with ReportLab

## Step 1: Write Markdown source (for DOCX/HTML)
write_file
path: /tmp/intl_report.md
content: |
  # International Market Analysis
  
  ## Région EMEA
  Performance in Europe: ↑ 12%
  Markets: Deutschland, França, España
  
  ## Région APAC
  Growth: ✓ Exceeded targets
  Key: 日本，中国，한국

## Step 2: Create ReportLab Python script for PDF
write_file
path: /tmp/generate_intl_pdf.py
content: |
  from reportlab.lib.pagesizes import letter
  from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
  from reportlab.lib.styles import getSampleStyleSheet
  from reportlab.lib import colors
  
  doc = SimpleDocTemplate("intl_analysis.pdf", pagesize=letter)
  styles = getSampleStyleSheet()
  story = []
  
  # Title - Unicode handled natively
  story.append(Paragraph("International Market Analysis", styles["Heading1"]))
  story.append(Spacer(1, 12))
  
  # EMEA Section
  story.append(Paragraph("Région EMEA", styles["Heading2"]))
  story.append(Paragraph("Performance in Europe: ↑ 12%", styles["Normal"]))
  story.append(Paragraph("Markets: Deutschland, França, España", styles["Normal"]))
  story.append(Spacer(1, 12))
  
  # APAC Section
  story.append(Paragraph("Région APAC", styles["Heading2"]))
  story.append(Paragraph("Growth: ✓ Exceeded targets", styles["Normal"]))
  story.append(Paragraph("Key: 日本，中国，한국", styles["Normal"]))
  
  doc.build(story)

## Step 3: Generate PDF with ReportLab
run_shell
command: python /tmp/generate_intl_pdf.py

## Step 4: Generate DOCX with pandoc
run_shell
command: pandoc /tmp/intl_report.md -o intl_analysis.docx

## Step 5: Generate HTML with pandoc
run_shell
command: pandoc /tmp/intl_report.md -o intl_analysis.html

## Step 6: Verify
run_shell
command: ls -lh intl_analysis.*
```

### Example 3: Hybrid Approach with Fallback

```markdown
# Generate with Pandoc, Fallback to ReportLab

## Step 1: Create source document
write_file
path: /tmp/analysis.md
content: |
  # Analysis Document
  [Content...]

## Step 2: Try pandoc first
run_shell
command: pandoc /tmp/analysis.md -o analysis.pdf

## Step 3: If pandoc fails, use ReportLab fallback
[If Step 2 returns error...]
write_file
path: /tmp/fallback_pdf.py
content: |
  from reportlab.lib.pagesizes import letter
  from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
  from reportlab.lib.styles import getSampleStyleSheet
  from reportlab.pdfbase import pdfmetrics
  from reportlab.pdfbase.ttfonts import TTFont
  
  # Register Unicode font if needed
  # pdfmetrics.registerFont(TTFont('UnicodeFont', '/path/to/font.ttf'))
  
  doc = SimpleDocTemplate("analysis.pdf", pagesize=letter)
  styles = getSampleStyleSheet()
  story = []
  story.append(Paragraph("Analysis Document", styles["Heading1"]))
  story.append(Spacer(1, 12))
  story.append(Paragraph("[Content from analysis.md parsed here]", styles["Normal"]))
  doc.build(story)

run_shell
command: python /tmp/fallback_pdf.py
```

## Common pandoc Commands

```bash
# Markdown to Word
pandoc input.md -o output.docx

# Markdown to PDF (requires LaTeX)
pandoc input.md -o output.pdf

# Markdown to PDF with Unicode-safe engine (better Unicode support)
pandoc input.md -o output.pdf --pdf-engine=xelatex

# Markdown to PDF with wkhtmltopdf (alternative, no LaTeX)
pandoc input.md -o output.pdf --pdf-engine=wkhtmltopdf

# Markdown to HTML
pandoc input.md -o output.html

# With custom template
pandoc input.md --template=template.html -o output.html

# With metadata
pandoc input.md -o output.pdf --metadata title="Document Title"
```

## Common ReportLab Patterns

```python
# Basic PDF creation
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet

doc = SimpleDocTemplate("output.pdf", pagesize=letter)
styles = getSampleStyleSheet()
story = []
story.append(Paragraph("Title", styles["Heading1"]))
story.append(Spacer(1, 12))
story.append(Paragraph("Content", styles["Normal"]))
doc.build(story)

# With tables
from reportlab.platypus import Table, TableStyle
from reportlab.lib import colors

data = [['Header1', 'Header2'], ['Row1-Col1', 'Row1-Col2']]
table = Table(data)
table.setStyle(TableStyle([
    ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
    ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
    ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
]))
story.append(table)

# With custom fonts for Unicode
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

pdfmetrics.registerFont(TTFont('NotoSans', 'NotoSans-Regular.ttf'))
custom_style = ParagraphStyle('Custom', fontName='NotoSans', fontSize=12)
story.append(Paragraph("Unicode: 日本語 العربية", custom_style))
```

## Troubleshooting

### Pandoc Issues

- **PDF generation fails with encoding error**: 
  - Use the sanitized markdown file
  - Try `--pdf-engine=xelatex` for better Unicode support
  - Try `--pdf-engine=wkhtmltopdf` as alternative

- **PDF generation fails: LaTeX not found**: 
  - Install LaTeX: `apt-get install texlive-latex-recommended texlive-fonts-recommended`
  - **Switch to ReportLab backend** (no LaTeX needed)

- **DOCX formatting issues**: Add `--reference-doc=template.docx` for custom styles

- **Missing pandoc**: Install via `apt-get install pandoc` or `brew install pandoc`

### ReportLab Issues

- **Missing reportlab**: Install via `pip install reportlab`

- **Unicode characters not rendering**: 
  - ReportLab handles UTF-8 natively - ensure Python script uses UTF-8
  - For CJK/Arabic/etc., register a Unicode-capable font:
    ```python
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    pdfmetrics.registerFont(TTFont('NotoSans', 'NotoSans-Regular.ttf'))
    ```

- **Complex layouts needed**: Use `reportlab.lib.canvas` for direct drawing operations

- **Tables breaking across pages**: Use `reportlab.platypus.LongTable` instead of `Table`

### General Issues

- **Unicode/encoding errors in any format**: 
  - Add `-f markdown+utf8` to pandoc command
  - Ensure source file is UTF-8 encoded: `file -i source.md`

- **Special characters not rendering in PDF**: 
  - For pandoc: Use character replacement table or ReportLab
  - For ReportLab: Register appropriate Unicode fonts

## Installation Commands

```bash
# Install pandoc
apt-get install pandoc  # Debian/Ubuntu
brew install pandoc     # macOS

# Install LaTeX (for pdflatex)
apt-get install texlive-latex-recommended texlive-fonts-recommended

# Install wkhtmltopdf (alternative PDF engine)
apt-get install wkhtmltopdf

# Install ReportLab
pip install reportlab

# Install fonts for Unicode support
apt-get install fonts-noto-core  # Comprehensive Unicode font
```

## Unicode Sanitization Script (For Pandoc Path)

For repeated use with pandoc, create a reusable sanitization script:

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
    -e "s/'([^']*)'/\1'/g" \
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

## Decision Flowchart

```
Need PDF generation?
    │
    ├─ Heavy Unicode/symbols? ──YES──> Use ReportLab
    │         │
    │         NO
    │         │
    ├─ LaTeX available? ──NO──> Use ReportLab
    │         │
    │         YES
    │         │
    ├─ Need Markdown formatting? ──YES──> Use Pandoc (with sanitization)
    │         │
    │         NO
    │         │
    └─ Need programmatic layout? ──YES──> Use ReportLab
```

## When to Return to shell_agent

After successfully completing the manual workflow once, you can attempt `shell_agent` again for similar tasks, now with a known-working fallback if errors recur. For documents with heavy Unicode content or when LaTeX is unavailable, prefer the ReportLab path.

## Related Skills

- `document-gen-unicode-safe`: Parent skill with pandoc-only approach
- `document-gen-fallback`: Original fallback workflow without Unicode guidance
- Use this skill when you need flexible PDF generation with multiple backend options
### Example 0: Lightweight Single-Format Generation

```markdown
# Quick DOCX Generation (No shell_agent needed)

## Step 1: Write content
write_file
path: /tmp/brief.md
content: |
  # Meeting Notes
  
  ## Attendees
  - Alice
  - Bob
  
  ## Decisions
  Project approved.

## Step 2: Convert directly
run_shell
command: pandoc /tmp/brief.md -o meeting_notes.docx

## Step 3: Verify
run_shell
command: ls -lh meeting_notes.docx
```

---

### Example 1: Pandoc-First Approach (Multi-Format with Sanitization)

### Example 2: ReportLab-First Approach (Heavy Unicode — Direct Execution)

## Mode Selection Quick Reference

| Scenario | Recommended Mode | Why |
|----------|------------------|-----|
| Simple Markdown → DOCX | **Lightweight** | Direct pandoc call is fastest |
| Single PDF from ASCII content | **Lightweight** | No sanitization needed |
| Multi-format batch (DOCX + PDF + HTML) | **Full Workflow** | Coordinated conversion with error handling |
| Content with Unicode/symbols | **Lightweight + ReportLab** | Direct Python script avoids LaTeX issues |
| Uncertain about LaTeX availability | **Full Workflow** | Built-in fallback detection |
| Need programmatic layout control | **Full Workflow** | ReportLab integration with fallback logic |
| Production pipeline with error recovery | **Full Workflow** | Automated retry and backend switching |

---

## Troubleshooting

- **Missing reportlab**: Install
### ReportLab Issues

- **Missing reportlab**: Install via `pip install reportlab`
