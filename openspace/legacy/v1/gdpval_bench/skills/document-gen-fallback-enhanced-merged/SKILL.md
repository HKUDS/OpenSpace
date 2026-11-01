---
name: resilient-document-pipeline
description: Unified document generation with tool failure detection, domain knowledge fallback, progressive conversion, and Unicode safety
---

# Resilient Document Generation Pipeline

## When to Use This Skill

Use this workflow when generating documents in **challenging environments** where:

- **Tool failures are likely or have occurred**: `read_file`, `search_web`, `read_webpage`, or `execute_code_sandbox` return errors
- **Multiple formats are needed**: Generate `.docx`, `.pdf`, `.html` from the same source
- **Unicode/special characters are present**: Documents contain em dashes, curly quotes, arrows, symbols, or non-ASCII text
- **Primary conversion methods may fail**: LaTeX/pandoc may not be available or misconfigured

This skill combines **early failure detection**, **domain-knowledge content creation**, **progressive conversion fallbacks**, and **Unicode safety** into a single resilient workflow.

## Core Workflow Overview

```
┌─────────────────────────────────────────────────────────────────┐
│  1. DETECT tool failures (2+ indicators → pivot immediately)   │
└────────────────┬────────────────────────────────────────────────┘
                 │
┌────────────────▼────────────────────────────────────────────────┐
│  2. CREATE content with write_file using embedded domain       │
│     knowledge (professionally structured markdown)              │
└────────────────┬────────────────────────────────────────────────┘
                 │
┌────────────────▼────────────────────────────────────────────────┐
│  3. SANITIZE unicode (create LaTeX-safe version for PDF)       │
└────────────────┬────────────────────────────────────────────────┘
                 │
┌────────────────▼────────────────────────────────────────────────┐
│  4. CONVERT with progressive fallbacks:                        │
│     pandoc → reportlab → fpdf2 → wkhtmltopdf                   │
└────────────────┬────────────────────────────────────────────────┘
                 │
┌────────────────▼────────────────────────────────────────────────┐
│  5. VERIFY outputs exist and are valid                          │
└─────────────────────────────────────────────────────────────────┘
```

## Step-by-Step Workflow

### Step 1: Detect Tool Failure Pattern

**Before attempting document generation**, check for failure indicators:

```
TOOL_FAILURE_INDICATORS = [
    "read_file returns binary/image data instead of text",
    "search_web returns unknown error or empty results",
    "read_webpage returns unknown error on multiple URLs",
    "execute_code_sandbox fails unexpectedly",
    "pandoc command fails with 'unknown error'",
    "Multiple consecutive tool failures on data retrieval"
]
```

**Decision point**: If **2+ indicators** are present within the first 2-3 iterations:

1. **Stop** attempting to fix the failing tools
2. **Acknowledge** the limitation briefly in your output
3. **Commit** to generating the document with available knowledge
4. **Proceed** to Step 2 immediately

### Step 2: Create Content with write_file

Write your document as **professionally structured Markdown** using embedded domain knowledge:

```
write_file
path: /tmp/document_source.md
content: |
  # [Document Title]
  
  ## Executive Summary
  [Brief overview of key content - use domain knowledge]
  
  ## Background
  [Context and scope - leverage embedded expertise]
  
  ## Main Content
  [Organized sections with headers, lists, tables]
  
  ## Limitations & Notes
  [Transparent about data source limitations if relevant]
  
  ## Recommendations/Next Steps
  [Actionable guidance based on available information]
```

**Best practices for content:**
- Use **general domain knowledge** appropriately when external data is unavailable
- **Clearly distinguish** between verified facts and general guidance
- Include **actionable frameworks** rather than specific unverified data
- Add **placeholder notes** where specific data would enhance the document:

```markdown
> **Note**: Specific [metric/data point] would typically be sourced from 
> [expected source]. The guidance below reflects established best practices.
```

### Step 3: Sanitize Unicode Characters (Pre-PDF Conversion)

**Critical**: PDF generation via pandoc typically uses LaTeX, which has limited Unicode support. Create a sanitized version:

#### Option A: Manual write_file (Recommended for control)

```
write_file
path: /tmp/document_source_sanitized.md
content: |
  [Same content as Step 2, but with these replacements:]
```

**Character replacement table:**

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
| Non-ASCII letters (é, ñ, ü) | Font-dependent | Use xeLaTeX or keep for non-PDF formats |

#### Option B: Shell script sanitization

```
run_shell
command: sed -e 's/—/--/g' -e 's/–/-/g' -e 's/"([^"]*)"/"\1"/g' -e "s/'([^']*)/'\1'/g" -e 's/…/.../g' -e 's/→/->/g' -e 's/←/<-/g' -e 's/©/(c)/g' -e 's/®/(r)/g' -e 's/™/(tm)/g' /tmp/document_source.md > /tmp/document_source_sanitized.md
```

**Important**: Keep the original unsanitized file for DOCX/HTML conversion (these formats handle Unicode better).

### Step 4: Convert to Target Formats with Progressive Fallbacks

Use `run_shell` with **progressive fallback** strategy. Try each method; if it fails, move to the next.

#### For DOCX (from original markdown):

```
run_shell
command: pandoc /tmp/document_source.md -o output.docx
```

**Fallback if pandoc fails:**
```
execute_code_sandbox
code: |
  from docx import Document
  # Parse markdown and create DOCX with python-docx
  # (Implementation depends on complexity needs)
```

#### For PDF (from sanitized markdown):

**Attempt 1 - pandoc with default engine:**
```
run_shell
command: pandoc /tmp/document_source_sanitized.md -o output.pdf
```

**Attempt 2 - pandoc with xeLaTeX (better Unicode):**
```
run_shell
command: pandoc /tmp/document_source_sanitized.md -o output.pdf --pdf-engine=xelatex
```

**Attempt 3 - pandoc with wkhtmltopdf:**
```
run_shell
command: pandoc /tmp/document_source_sanitized.md -o output.pdf --pdf-engine=wkhtmltopdf
```

**Attempt 4 - Python fpdf2 (most reliable fallback):**
```
execute_code_sandbox
code: |
  from fpdf import FPDF
  import re
  
  # Read sanitized markdown
  with open('/tmp/document_source_sanitized.md', 'r') as f:
      content = f.read()
  
  # Simple markdown-to-PDF conversion
  pdf = FPDF()
  pdf.add_page()
  pdf.set_font('Arial', '', 12)
  
  # Process content (strip markdown, handle basic formatting)
  lines = content.split('\n')
  for line in lines:
      # Remove markdown syntax
      clean = re.sub(r'[#*_`]', '', line)
      if clean.strip():
          pdf.cell(0, 10, clean, ln=True)
  
  pdf.output('output.pdf')
  print('PDF created successfully')
```

**Attempt 5 - Python reportlab (alternative):**
```
execute_code_sandbox
code: |
  from reportlab.lib.pagesizes import letter
  from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
  from reportlab.lib.styles import getSampleStyleSheet
  import re
  
  doc = SimpleDocTemplate('output.pdf', pagesize=letter)
  styles = getSampleStyleSheet()
  story = []
  
  with open('/tmp/document_source_sanitized.md', 'r') as f:
      content = f.read()
  
  lines = content.split('\n')
  for line in lines:
      clean = re.sub(r'[#*_`]', '', line)
      if clean.strip():
          story.append(Paragraph(clean, styles['Normal']))
          story.append(Spacer(1, 6))
  
  doc.build(story)
  print('PDF created with reportlab')
```

#### For HTML (from original markdown):

```
run_shell
command: pandoc /tmp/document_source.md -o output.html
```

**Fallback if pandoc fails:**
```
execute_code_sandbox
code: |
  import markdown
  with open('/tmp/document_source.md', 'r') as f:
      md_content = f.read()
  html_content = markdown.markdown(md_content)
  with open('output.html', 'w') as f:
      f.write(f'<!DOCTYPE html><html><body>{html_content}</body></html>')
```

### Step 5: Verify Outputs

Check that files were created successfully and have reasonable sizes:

```
run_shell
command: ls -lh output.* 2>/dev/null || echo "Some outputs missing"
```

```
run_shell
command: file output.pdf output.docx output.html 2>/dev/null
```

**Validation criteria:**
- [ ] File exists
- [ ] File size > 0 bytes
- [ ] File command recognizes the format correctly
- [ ] (Optional) Open/preview the file to confirm content renders

## Complete Example

```markdown
# Generate Project Status Report

## Step 1: Detect failures (if applicable)
Tool check: read_webpage failed 3x, search_web failed 2x → PIVOT to fallback workflow

## Step 2: Write Markdown source
write_file
path: /tmp/project_status.md
content: |
  # Project Status Report
  
  ## Executive Summary
  This report summarizes project progress using established methodologies.
  
  ## Current Status
  - Phase 1: Complete
  - Phase 2: In Progress (75%)
  - Phase 3: Not Started
  
  ## Risks & Mitigation
  Standard risk framework applied...
  
  ## Recommendations
  1. Continue current trajectory
  2. Schedule stakeholder review

> **Note**: Specific metrics from project management tool were unavailable; guidance reflects standard practices.

## Step 3: Sanitize for PDF
write_file
path: /tmp/project_status_sanitized.md
content: |
  [Same content with any special chars replaced per table above]

## Step 4: Convert (try each, proceed on failure)
run_shell
command: pandoc /tmp/project_status.md -o project_status.docx

run_shell
command: pandoc /tmp/project_status_sanitized.md -o project_status.pdf

run_shell
command: pandoc /tmp/project_status.md -o project_status.html

## Step 5: Verify
run_shell
command: ls -lh project_status.*
```

## Decision Tree for Conversion Failures

```
pandoc PDF fails?
  ├─→ Try --pdf-engine=xelatex
  │   └─→ Fails?
  │       ├─→ Try --pdf-engine=wkhtmltopdf
  │       │   └─→ Fails?
  │       │       └─→ Use Python fpdf2 (most reliable)
  │       └─→ Success → Done
  └─→ Success → Done
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| PDF generation fails with encoding error | Use sanitized markdown; try `--pdf-engine=xelatex` |
| Pandoc not found | Install via `apt-get install pandoc` or use Python fallbacks |
| LaTeX not found | Use `--pdf-engine=wkhtmltopdf` or Python fpdf2 |
| DOCX formatting issues | Add `--reference-doc=template.docx` for custom styles |
| Unicode in any format | Use `-f markdown+utf8`; ensure UTF-8 encoding with `file -i source.md` |
| All pandoc commands fail | Fall back to Python libraries (fpdf2, reportlab, python-docx, markdown) |
| Python libraries missing | Install via `pip install fpdf2 reportlab python-docx markdown` |

## Advantages Over Standard Approaches

| Aspect | Standard Approach | Resilient Pipeline |
|--------|------------------|-------------------|
| Tool failure response | Retry same approach | Detect pattern, pivot immediately |
| Content source | External data only | Embedded domain knowledge when needed |
| Conversion robustness | Single method | Progressive fallbacks (5+ options) |
| Unicode handling | May cause failures | Proactive sanitization |
| Verification | Optional | Built-in validation step |
| Transparency | May hide limitations | Explicit about constraints |

## Best Practices

| Do | Don't |
|----|-------|
| Pivot after 2+ tool failures | Retry failing tools 5+ times |
| Sanitize before PDF conversion | Send raw Unicode to LaTeX |
| Try multiple conversion methods | Give up after first failure |
| Be transparent about limitations | Claim unverified facts as certain |
| Verify all outputs | Assume files were created |
| Use progressive fallbacks | Hard-code single approach |

## When to Return to Standard Methods

After successfully completing the resilient pipeline:

1. **For similar future tasks**: You can attempt standard `shell_agent` delegation first
2. **Keep this skill ready**: Use immediately if tool failures recur
3. **For Unicode-heavy documents**: Consider always using this workflow with sanitization
4. **For critical deliverables**: This workflow provides maximum reliability

## Related Skills

- `document-gen-fallback-enhanced`: Original Unicode-safe fallback (parent)
- `write-file-fallback-report`: Domain-knowledge report generation (parent)
- Use this unified skill when you need **all capabilities combined** in one workflow
*** End Files
*** Add File: sanitize_for_pdf.sh
#!/bin/bash
# sanitize_for_pdf.sh - Replace problematic unicode chars for LaTeX/PDF
# Usage: ./sanitize_for_pdf.sh <input.md> [output.md]

if [ -z "$1" ]; then
  echo "Usage: $0 <input.md> [output.md]"
  exit 1
fi

INPUT="$1"
OUTPUT="${2:-${1%.md}_sanitized.md}"

# Check input file exists
if [ ! -f "$INPUT" ]; then
  echo "Error: Input file not found: $INPUT"
  exit 1
fi

# Apply sanitization replacements
sed -e 's/—/--/g' \
    -e 's/–/-/g' \
    -e 's/"([^"]*)"/"\1"/g' \
    -e "s/'([^']*)/'\1'/g" \
    -e 's/…/.../g' \
    -e 's/→/->/g' \
    -e 's/←/<-/g' \
    -e 's/↑/^^/g' \
    -e 's/↓/vv/g' \
    -e 's/✓/[x]/g' \
    -e 's/✗/[ ]/g' \
    -e 's/★/*/g' \
    -e 's/●/-/g' \
    -e 's/©/(c)/g' \
    -e 's/®/(r)/g' \
    -e 's/™/(tm)/g' \
    "$INPUT" > "$OUTPUT"

echo "Sanitized: $INPUT -> $OUTPUT"
echo "Verify with: diff $INPUT $OUTPUT"
*** Add File: convert_with_fallbacks.sh
#!/bin/bash
# convert_with_fallbacks.sh - Progressive fallback document conversion
# Usage: ./convert_with_fallbacks.sh <input.md> <output_format>
# Formats: pdf, docx, html

if [ -z "$1" ] || [ -z "$2" ]; then
  echo "Usage: $0 <input.md> <pdf|docx|html>"
  exit 1
fi

INPUT="$1"
FORMAT="$2"
BASENAME="${INPUT%.*}"
OUTPUT="${BASENAME}.${FORMAT}"

echo "Converting $INPUT to $FORMAT..."

if [ "$FORMAT" = "pdf" ]; then
  echo "Attempt 1: pandoc default"
  if pandoc "$INPUT" -o "$OUTPUT" 2>/dev/null; then
    echo "Success with pandoc default"
    exit 0
  fi
  
  echo "Attempt 2: pandoc with xeLaTeX"
  if pandoc "$INPUT" -o "$OUTPUT" --pdf-engine=xelatex 2>/dev/null; then
    echo "Success with xeLaTeX"
    exit 0
  fi
  
  echo "Attempt 3: pandoc with wkhtmltopdf"
  if pandoc "$INPUT" -o "$OUTPUT" --pdf-engine=wkhtmltopdf 2>/dev/null; then
    echo "Success with wkhtmltopdf"
    exit 0
  fi
  
  echo "Attempt 4: Python fpdf2"
  if python3 -c "
from fpdf import FPDF
import re
pdf = FPDF()
pdf.add_page()
pdf.set_font('Arial', '', 12)
with open('$INPUT', 'r') as f:
    for line in f:
        clean = re.sub(r'[#*_\`]', '', line)
        if clean.strip():
            pdf.cell(0, 10, clean.strip(), ln=True)
pdf.output('$OUTPUT')
" 2>/dev/null; then
    echo "Success with fpdf2"
    exit 0
  fi
  
  echo "All PDF conversion methods failed"
  exit 1

elif [ "$FORMAT" = "docx" ]; then
  echo "Attempt 1: pandoc"
  if pandoc "$INPUT" -o "$OUTPUT" 2>/dev/null; then
    echo "Success with pandoc"
    exit 0
  fi
  
  echo "Pandoc failed for DOCX - consider Python fallback (python-docx)"
  exit 1

elif [ "$FORMAT" = "html" ]; then
  echo "Attempt 1: pandoc"
  if pandoc "$INPUT" -o "$OUTPUT" 2>/dev/null; then
    echo "Success with pandoc"
    exit 0
  fi
  
  echo "Attempt 2: Python markdown"
  if python3 -c "
import markdown
with open('$INPUT', 'r') as f:
    content = f.read()
html = markdown.markdown(content)
with open('$OUTPUT', 'w') as f:
    f.write(f'<!DOCTYPE html><html><body>{html}</body></html>')
" 2>/dev/null; then
    echo "Success with markdown library"
    exit 0
  fi
  
  echo "All HTML conversion methods failed"
  exit 1

else
  echo "Unknown format: $FORMAT (use pdf, docx, or html)"
  exit 1
fi
