---
name: document-gen-fallback
description: Fallback workflow for multi-format document generation when shell_agent encounters errors
---

# Document Generation Fallback Workflow

## When to Use

Use this skill when `shell_agent` returns unknown or unclear errors on complex document generation tasks, especially when:
- Generating documents in multiple formats (e.g., `.docx`, `.pdf`, `.html`)
- The error message doesn't provide clear debugging information
- You need better visibility into each step of the generation process

## Core Technique

Instead of delegating the entire document generation to `shell_agent`, manually split the workflow into discrete, observable steps:
1. **Content creation** → Use `write_file` to create source document (Markdown)
2. **Format conversion** → Use `run_shell` with `pandoc` for each target format
3. **Verification** → Check output files exist and are valid

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

### Step 2: Convert to Target Formats with run_shell

Use `run_shell` with explicit `pandoc` commands for each format. This isolates conversion errors.

```
run_shell
command: pandoc /tmp/document_source.md -o output.docx
```

```
run_shell
command: pandoc /tmp/document_source.md -o output.pdf
```

### Step 3: Verify Outputs

Check that files were created successfully:

```
run_shell
command: ls -lh output.docx output.pdf
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
  [Content...]
  
  ## Resolution Path
  [Content...]
  
  ## BATNA Analysis
  [Content...]

## Step 2: Convert to DOCX
run_shell
command: pandoc /tmp/negotiation_strategy.md -o negotiation_strategy.docx

## Step 3: Convert to PDF
run_shell
command: pandoc /tmp/negotiation_strategy.md -o negotiation_strategy.pdf

## Step 4: Verify
run_shell
command: ls -lh negotiation_strategy.*
```

## Advantages Over shell_agent

| Aspect | shell_agent | Manual Workflow |
|--------|-------------|-----------------|
| Error visibility | Opaque, may retry silently | Each step shows explicit output |
| Recovery | Automatic but may loop | Manual intervention at specific step |
| Debugging | Hard to isolate failure point | Clear which step failed |
| Control | Agent decides approach | You control each conversion |

## Common pandoc Commands

```bash
# Markdown to Word
pandoc input.md -o output.docx

# Markdown to PDF (requires LaTeX or wkhtmltopdf)
pandoc input.md -o output.pdf

# Markdown to HTML
pandoc input.md -o output.html

# With custom template
pandoc input.md --template=template.html -o output.html

# With metadata
pandoc input.md -o output.pdf --metadata title="Document Title"
```

## Troubleshooting

- **PDF generation fails**: Ensure LaTeX is installed (`texlive` package) or use `--pdf-engine=wkhtmltopdf`
- **DOCX formatting issues**: Add `--reference-doc=template.docx` for custom styles
- **Unicode/encoding errors**: Add `-f markdown+utf8` to pandoc command
- **Missing pandoc**: Install via `apt-get install pandoc` or `brew install pandoc`

## When to Return to shell_agent

After successfully completing the manual workflow once, you can attempt `shell_agent` again for similar tasks, now with a known-working fallback if errors recur.