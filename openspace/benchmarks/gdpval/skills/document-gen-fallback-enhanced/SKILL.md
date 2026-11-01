---
name: document-gen-unicode-safe
description: Unicode-safe fallback workflow for multi-format document generation with character sanitization and explicit error capture
---

# Document Generation Fallback Workflow (Unicode-Safe)

## When to Use

Use this skill when `shell_agent` returns unknown or unclear errors on complex document generation tasks, especially when:
- Generating documents in multiple formats (e.g., `.docx`, `.pdf`, `.html`)
- The error message doesn't provide clear debugging information
- You need to handle special characters, symbols, or non-ASCII text safely
- PDF generation fails due to LaTeX encoding issues

## Core Technique

Instead of delegating the entire document generation to `shell_agent`, manually split the workflow into discrete, observable steps:
1. **Content creation** → Use `write_file` to create source document (Markdown)
2. **Unicode sanitization** → Preprocess markdown to replace problematic characters before PDF conversion
3. **Format conversion with stderr capture** → Use `run_shell` with `pandoc` for each target format, capturing full stderr output
4. **Verification** → Check output files exist and are valid

## ⚠️ Unicode & LaTeX Compatibility Warning

**Critical**: PDF generation via pandoc typically uses LaTeX (`pdflatex` or `xelatex`), which has limited Unicode support. Common problematic characters include:

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

## Step-by-Step Workflow

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

### Step 2: Sanitize Unicode Characters (Pre-PDF Conversion)

**Before PDF conversion**, create a sanitized version of your markdown with LaTeX-safe characters:

```
write_file
path: /tmp/document_source_sanitized.md
content: |
  # Document Title
  
  ## Section 1
  Content here... (with all special chars replaced per table above)
```

**Alternative**: Use a shell script to sanitize:

```
run_shell
command: sed -e 's/—/--/g' -e 's/–/-/g' -e 's/"([^"]*)"/"\1"/g' -e 's/'([^']*)/'\1'/g' /tmp/document_source.md > /tmp/document_source_sanitized.md
```

**Note**: Keep the original unsanitized file for DOCX/HTML conversion (these formats handle Unicode better).

### Step 3: Convert to Target Formats with run_shell

Use `run_shell` with explicit `pandoc` commands for each format. Use sanitized source for PDF, original for other formats.

**Critical**: Always capture stderr output (`2>&1`) to diagnose failures before retrying with alternative engines.

```
run_shell
command: pandoc /tmp/document_source.md -o output.docx
```

```
run_shell
command: pandoc /tmp/document_source.md -o output.html
```

### Step 3a: PDF Conversion - Try xelatex First (Best Unicode Support)

**Use xelatex as the first engine** - it has better Unicode support and is commonly available in containerized environments:

```
run_shell
command: pandoc /tmp/document_source_sanitized.md --pdf-engine=xelatex -o output.pdf 2>&1
```

### Step 3b: If xelatex Fails - Capture Full Error and Retry with wkhtmltopdf

**Before retrying**, examine the full stderr output from Step 3a to understand the failure cause (missing engine, font issues, etc.). Then try wkhtmltopdf as fallback:

```
run_shell
command: pandoc /tmp/document_source_sanitized.md --pdf-engine=wkhtmltopdf -o output.pdf 2>&1
```

### Step 4: Verify Outputs

Check that files were created successfully:

```
run_shell
command: ls -lh output.docx output.pdf output.html
```

## Complete Example

```markdown
# Generate Negotiation Strategy Document

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

## Step 2: Create sanitized version for PDF
write_file
path: /tmp/negotiation_strategy_sanitized.md
content: |
  # Negotiation Strategy
  
  ## Executive Summary
  [Content with unicode replaced: em-dash -> --, curly quotes -> straight, etc.]
  
  ## Resolution Path
  [Content...]
  
  ## BATNA Analysis
  [Content...]

## Step 3: Convert to DOCX (from original)
run_shell
command: pandoc /tmp/negotiation_strategy.md -o negotiation_strategy.docx

## Step 4: Convert to HTML (from original)
run_shell
command: pandoc /tmp/negotiation_strategy.md -o negotiation_strategy.html

## Step 5: Convert to PDF (try xelatex first, capture stderr)
run_shell
command: pandoc /tmp/negotiation_strategy_sanitized.md --pdf-engine=xelatex -o negotiation_strategy.pdf 2>&1

## Step 6: If xelatex fails, retry with wkhtmltopdf (after examining error)
run_shell
command: pandoc /tmp/negotiation_strategy_sanitized.md --pdf-engine=wkhtmltopdf -o negotiation_strategy.pdf 2>&1

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
| Control | Agent decides approach | You control each conversion |
| **Stderr capture** | Often lost in retries | Explicit `2>&1` capture at each step |

## Common pandoc Commands

```bash
# Markdown to Word
pandoc input.md -o output.docx

# Markdown to PDF with xelatex (better Unicode support - TRY FIRST)
pandoc input.md -o output.pdf --pdf-engine=xelatex

# Markdown to PDF with wkhtmltopdf (fallback if xelatex unavailable)
pandoc input.md -o output.pdf --pdf-engine=wkhtmltopdf

# Markdown to HTML
pandoc input.md -o output.html

# With custom template
pandoc input.md --template=template.html -o output.html

# With metadata
pandoc input.md -o output.pdf --metadata title="Document Title"
```

## Troubleshooting

- **PDF generation fails with encoding error**: 
  - Use the sanitized markdown file (Step 2)
  - **Try xelatex first** (better Unicode support, more common in containers): `--pdf-engine=xelatex`
  - **Capture full stderr** with `2>&1` and examine the error before retrying
  - **Then retry with wkhtmltopdf** as fallback: `--pdf-engine=wkhtmltopdf`

- **PDF generation fails: LaTeX not found**: 
  - Install LaTeX: `apt-get install texlive-latex-recommended texlive-fonts-recommended`
  - **Preferred**: Use `--pdf-engine=xelatex` (often pre-installed in containers)
  - **Fallback**: Use `--pdf-engine=wkhtmltopdf` if xelatex unavailable

- **xelatex not found**: Install with `apt-get install texlive-xetex` or use wkhtmltopdf fallback

- **wkhtmltopdf not found**: Install with `apt-get install wkhtmltopdf`

- **DOCX formatting issues**: Add `--reference-doc=template.docx` for custom styles

- **Unicode/encoding errors in any format**: 
  - Add `-f markdown+utf8` to pandoc command
  - Ensure source file is UTF-8 encoded: `file -i source.md`

- **Special characters not rendering in PDF**: 
  - Use the character replacement table above
  - Create sanitized version before PDF conversion
  - Use xelatex engine for better Unicode font support

- **Missing pandoc**: Install via `apt-get install pandoc` or `brew install pandoc`

- **Engine failures not diagnosed**: Always append `2>&1` to capture stderr, examine output before retrying with different engine

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
    -e 's/'([^']*)/'\1'/g' \
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

## When to Return to shell_agent

After successfully completing the manual workflow once, you can attempt `shell_agent` again for similar tasks, now with a known-working fallback if errors recur. For documents with heavy Unicode content, consider always using the manual workflow with sanitization.

## Related Skills

- `document-gen-fallback`: Original fallback workflow without Unicode guidance
- Use this skill when your documents contain special characters, symbols, or non-ASCII text that may cause LaTeX/PDF conversion issues
Return to `shell_agent` in these scenarios:

1. **Environment diagnostics fail** - If pandoc or PDF engines are unavailable and installation isn't possible
2. **Multiple tools returning 'unknown error'** - Suggests broader environment issue; shell_agent may have different access
3. **After successful manual completion** - For similar future tasks, try shell_agent first with this workflow as fallback
4. **Simple documents** - For straightforward documents without complex Unicode, shell_agent is often sufficient
5. **Time-sensitive tasks** - When you need quick results and can accept some formatting uncertainty

**For documents with heavy Unicode content**, consider always using the manual workflow with sanitization, but only after confirming the toolchain is available.
