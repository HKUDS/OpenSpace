---
name: document-gen-unicode-safe
description: Unicode-safe fallback workflow for multi-format document generation with character sanitization
---

# Document Generation Fallback Workflow (Unicode-Safe)

## When to Use

**First**, assess whether to delegate to `shell_agent` or use manual workflow:

### ✅ Use shell_agent Directly When:
- Document is straightforward (no special Unicode characters, symbols, or non-ASCII text)
- Single format output needed (e.g., just `.docx`)
- Content is primarily ASCII text with standard punctuation
- No complex formatting requirements (tables, equations, custom templates)
- You want quick iteration and `shell_agent` has succeeded on similar tasks before

### ⚠️ Use Manual Workflow When:
- `shell_agent` returns unknown or unclear errors
- **Document contains special characters, symbols, or non-ASCII text** (see Unicode table below)
- Generating multiple formats simultaneously (e.g., `.docx`, `.pdf`, `.html`)
- PDF generation fails due to LaTeX encoding issues
- You need fine-grained control over each conversion step
- Previous `shell_agent` attempts failed on similar content

### 📋 Decision Checklist

```markdown
1. Does the document contain: em-dashes (—), curly quotes (" "), arrows (→), 
   checkmarks (✓), or other special symbols? → Use Manual Workflow

2. Is non-ASCII text present (é, ñ, ü, 中文，العربية)? → Use Manual Workflow with sanitization

3. Did shell_agent already fail on this or similar content? → Use Manual Workflow

4. Need multiple output formats with guaranteed success? → Use Manual Workflow

5. Simple ASCII document, single format, first attempt? → Try shell_agent First
```

## Core Technique

**Quick Path** (for straightforward documents): Delegate directly to `shell_agent` with clear instructions:

```
shell_agent
task: Create a Word document from markdown content. Write the markdown to 
      /tmp/document.md, then convert to .docx using pandoc, and verify the output exists.
```

**Manual Path** (for complex/Unicode-heavy documents): Split the workflow into discrete, observable steps:
1. **Content creation** → Use `write_file` to create source document (Markdown)
2. **Unicode sanitization** → Preprocess markdown to replace problematic characters before PDF conversion
3. **Format conversion** → Use `run_shell` with `pandoc` for each target format
4. **Verification** → Check output files exist and are valid

## Why This Adaptive Approach?

| Scenario | Recommended Approach | Rationale |
|----------|---------------------|-----------|
| Simple ASCII doc, single format | shell_agent | Faster, less manual overhead |
| Unicode symbols present | Manual workflow | Prevents LaTeX encoding failures |
| Previous shell_agent failure | Manual workflow | Avoids repeat failures |
| Multiple formats needed | Manual workflow | Granular control per format |
| Time-sensitive, low-risk | shell_agent first | Try fast path, fallback if needed |
1. **Content creation** → Use `write_file` to create source document (Markdown)
2. **Unicode sanitization** → Preprocess markdown to replace problematic characters before PDF conversion
3. **Format conversion** → Use `run_shell` with `pandoc` for each target format
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

### Step 4: Verify Outputs

Check that files were created successfully:

```
run_shell
command: ls -lh output.docx output.pdf output.html
```

## Complete Example

### Quick Path Example (Simple Document)

```markdown
# Generate Simple Meeting Notes

## Delegate to shell_agent
shell_agent
task: |
  Create meeting notes document:
  1. Write to /tmp/meeting_notes.md with content:
     "# Team Meeting Notes
     
     ## Date: 2024-01-15
     ## Attendees: Alice, Bob, Carol
     
     ## Action Items
     - Alice: Complete report by Friday
     - Bob: Schedule follow-up meeting
     
     ## Next Meeting: 2024-01-22"
  2. Convert to Word: pandoc /tmp/meeting_notes.md -o meeting_notes.docx
  3. Verify: ls -lh meeting_notes.docx
```

### Manual Path Example (Unicode-Heavy Document)

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

## Step 4: Convert to PDF (from sanitized)
run_shell
command: pandoc /tmp/negotiation_strategy_sanitized.md -o negotiation_strategy.pdf

## Step 5: Convert to HTML (from original)
run_shell
command: pandoc /tmp/negotiation_strategy.md -o negotiation_strategy.html

## Step 6: Verify
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

## Common pandoc Commands

```bash
# Markdown to Word
pandoc input.md -o output.docx

# Markdown to PDF (requires LaTeX or wkhtmltopdf)
pandoc input.md -o output.pdf

# Markdown to PDF with Unicode-safe engine (better Unicode support)
pandoc input.md -o output.pdf --pdf-engine=xelatex

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
  - Or try `--pdf-engine=xelatex` for better Unicode support
  - Or use `--pdf-engine=wkhtmltopdf` as alternative

- **PDF generation fails: LaTeX not found**: 
  - Install LaTeX: `apt-get install texlive-latex-recommended texlive-fonts-recommended`
  - Or use `--pdf-engine=wkhtmltopdf` instead

- **DOCX formatting issues**: Add `--reference-doc=template.docx` for custom styles

- **Unicode/encoding errors in any format**: 
  - Add `-f markdown+utf8` to pandoc command
  - Ensure source file is UTF-8 encoded: `file -i source.md`

- **Special characters not rendering in PDF**: 
  - Use the character replacement table above
  - Create sanitized version before PDF conversion

- **Missing pandoc**: Install via `apt-get install pandoc` or `brew install pandoc`

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

**After successful shell_agent completion**: Continue using shell_agent for similar straightforward documents.

**After shell_agent failure on Unicode content**: Switch to manual workflow for that document and similar future documents. Document the failure pattern.

**After successful manual workflow**: You now have a proven approach. For future similar documents:
- If content is similar (same Unicode patterns): Use manual workflow directly
- If content is simpler: Try shell_agent first, keep manual workflow as backup
- Build a library of sanitized templates for repeated use

## Related Skills

- `document-gen-fallback`: Original fallback workflow without Unicode guidance
- Use this skill as your **primary** document generation skill—it adapts to document complexity
- Fall back to `document-gen-unicode-safe` (parent) if you need the original prescriptive manual-only approach
name: document-gen-adaptive-workflow
description: Adaptive document generation with smart fallback to manual Unicode-safe workflow
### Quick Path: Delegate to shell_agent

For straightforward documents without Unicode concerns:

```
shell_agent
task: |
  1. Write markdown content to /tmp/report.md using write_file
  2. Convert to Word format: pandoc /tmp/report.md -o report.docx
  3. Convert to PDF: pandoc /tmp/report.md -o report.pdf --pdf-engine=xelatex
  4. Verify both files exist with: ls -lh report.docx report.pdf
  5. Report the file sizes and confirm successful generation
```

**If shell_agent succeeds**: Task complete.

**If shell_agent fails**: Switch to Manual Path below.

### Manual Path: Step-by-Step Workflow
