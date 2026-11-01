---
name: safe-script-execution
description: Fallback workflow for code execution when automated tools fail, using shell heredoc with verification
---

# Safe Script Execution Fallback

## When to Use This Skill

Use this pattern when `execute_code_sandbox` or `shell_agent` repeatedly fails to produce expected outputs. This manual fallback provides explicit control over script creation and execution with built-in verification.

## Step-by-Step Instructions

### Step 1: Verify Working Directory

Before creating any scripts, confirm your current location and permissions:

```bash
pwd
ls -la
```

Document the absolute path. All subsequent file operations should use explicit paths from this point.

### Step 2: Create Script via Heredoc

Use shell heredoc syntax to create scripts. This avoids issues with multi-line string escaping:

```bash
cat > /full/path/to/script.py << 'HEREDOC_END'
#!/usr/bin/env python3
# Your script content here
# Single quotes around delimiter prevent variable expansion
print("Hello from script")
HEREDOC_END
```

**Key escaping rules:**
- Use single quotes around heredoc delimiter (`'EOF'`) to prevent shell variable expansion
- If script contains the delimiter string, choose a different unique delimiter
- For bash scripts containing special characters, escape `$`, backticks, and `!`

### Step 3: Make Executable and Run with Explicit Path

```bash
chmod +x /full/path/to/script.py
/full/path/to/script.py
```

Always use the full absolute path, never rely on `.` or relative paths.

### Step 4: Verify Output

Confirm files were created and inspect their properties:

```bash
# Check file exists with size
ls -lh /full/path/to/output.file

# For PDFs, inspect metadata
pdfinfo /full/path/to/output.pdf 2>/dev/null || file /full/path/to/output.pdf

# For Word docs, check file type
file /full/path/to/output.docx
```

### Step 5: Error Handling

If execution fails:
1. Check script syntax: `python3 -m py_compile /path/to/script.py`
2. Check permissions: `ls -l /path/to/script.py`
3. Check available disk space: `df -h .`
4. Review stderr output carefully

## Example: PDF Generation Fallback

```bash
# Step 1: Verify directory
pwd
ls -la

# Step 2: Create Python script
cat > /workspace/generate_pdf.py << 'SCRIPT_END'
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

c = canvas.Canvas("/workspace/output.pdf", pagesize=letter)
c.drawString(100, 750, "Generated PDF")
c.save()
SCRIPT_END

# Step 3: Execute
chmod +x /workspace/generate_pdf.py
python3 /workspace/generate_pdf.py

# Step 4: Verify
ls -lh /workspace/output.pdf
pdfinfo /workspace/output.pdf 2>/dev/null || echo "PDF created, pdfinfo unavailable"
```

## Anti-Patterns to Avoid

- ❌ Don't use relative paths like `./script.py` - always use absolute paths
- ❌ Don't rely on unquoted heredoc delimiters if script contains variables
- ❌ Don't skip verification steps - always confirm output exists and has expected size
- ❌ Don't assume working directory - always `pwd` first

## Troubleshooting Checklist

- [ ] Working directory confirmed with `pwd`
- [ ] Script file exists after heredoc creation (`ls -l`)
- [ ] Script has execute permissions (`chmod +x`)
- [ ] Output file exists with non-zero size (`ls -lh`)
- [ ] File type verified (`file` command or type-specific inspection)