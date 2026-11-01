---
name: fallback-file-creation
description: Fallback pattern for reliable file creation when code execution tools fail
---

# Fallback File Creation Workflow

Use this skill when `execute_code_sandbox` or `shell_agent` fails repeatedly for file creation tasks. This pattern provides a manual but reliable approach to create and verify files.

## When to Use

- Code execution tools fail after multiple retries
- You need to create specific file formats (PDF, DOCX, etc.)
- Direct shell commands are more reliable than generated scripts
- You need explicit verification of file outputs

## Step-by-Step Instructions

### Step 1: Verify Working Directory

Before any file operations, confirm your current location:

```bash
pwd
ls -la
```

This ensures you're creating files in the correct workspace directory.

### Step 2: Create Scripts via Heredoc

Use shell heredocs to create scripts with properly escaped syntax:

```bash
cat > /path/to/script.sh << 'EOF'
#!/bin/bash
# Your script content here
# Variables are NOT expanded inside 'EOF' (quoted)
echo "Creating file..."
# Commands to generate output
EOF
```

**Key escaping rules:**
- Use `'EOF'` (quoted) to prevent variable expansion inside the heredoc
- Use `EOF` (unquoted) if you want shell variables to expand
- Escape any `$`, backticks, or special characters if using unquoted EOF

### Step 3: Execute with Explicit Path

Always run scripts with their full or explicit relative path:

```bash
chmod +x /path/to/script.sh
/path/to/script.sh
# or
bash /path/to/script.sh
```

Avoid relying on `.` or implicit paths.

### Step 4: Verify Output

After execution, verify files were created correctly:

```bash
# Check file exists and size
ls -lh /path/to/output.file

# For PDFs: inspect metadata
pdfinfo /path/to/output.pdf

# For DOCX: check structure
unzip -l /path/to/output.docx | head -20

# For any file: check content preview
file /path/to/output.file
head -c 500 /path/to/output.file
```

### Step 5: Iterative Debugging

If verification fails:

1. Check error messages from script execution
2. Verify all dependencies are available (`which command`)
3. Test individual commands from the script manually
4. Re-run with verbose output (`bash -x script.sh`)

## Example: Creating a PDF Document

```bash
# Step 1: Verify directory
pwd
ls -la

# Step 2: Create conversion script
cat > /workspace/create_pdf.sh << 'EOF'
#!/bin/bash
cd /workspace
libreoffice --headless --convert-to pdf checklist.odt --outdir .
EOF

# Step 3: Execute
chmod +x /workspace/create_pdf.sh
/workspace/create_pdf.sh

# Step 4: Verify
ls -lh /workspace/checklist.pdf
pdfinfo /workspace/checklist.pdf
```

## Example: Creating a DOCX Document

```bash
# Step 1: Verify directory
pwd
ls -la

# Step 2: Create document script
cat > /workspace/create_docx.sh << 'EOF'
#!/bin/bash
cd /workspace
pandoc -f markdown -t docx action_tracker.md -o action_tracker.docx
EOF

# Step 3: Execute
chmod +x /workspace/create_docx.sh
/workspace/create_docx.sh

# Step 4: Verify
ls -lh /workspace/action_tracker.docx
unzip -l /workspace/action_tracker.docx | head -20
```

## Best Practices

1. **Always verify before and after** - Check directory state before starting and after completion
2. **Use absolute or explicit paths** - Avoid ambiguity in file locations
3. **Quote heredoc delimiters** - Use `'EOF'` to prevent unwanted variable expansion
4. **Make scripts executable** - Always `chmod +x` before running
5. **Inspect file metadata** - Use format-specific tools (pdfinfo, unzip -l, file) to verify content
6. **Keep scripts simple** - Each script should do one thing well
7. **Clean up temporary files** - Remove intermediate files after successful completion

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Script not found | Use full path: `/workspace/script.sh` |
| Permission denied | Run `chmod +x script.sh` |
| File not created | Check script output, verify working directory in script |
| Wrong file format | Verify conversion tool supports target format |
| Corrupted output | Re-run with verbose mode, check intermediate files |