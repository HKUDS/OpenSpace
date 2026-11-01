---
name: verified-script-execution
description: Fallback workflow for reliable file creation and verification when automated code execution fails
---

# Verified Script Execution Workflow

When `execute_code_sandbox` or `shell_agent` fails repeatedly, use this manual fallback pattern to reliably create and verify files through explicit shell commands.

## When to Activate This Pattern

- After 3+ consecutive failures with `execute_code_sandbox`
- When `shell_agent` produces repeated errors without progress
- For critical deliverables (PDFs, Word docs, reports) that must be created reliably
- When working directory confusion causes file creation failures

## Step 1: Verify Working Directory

Always confirm your current location before creating any files:

```bash
pwd
ls -la
```

This ensures you're writing to the correct workspace directory and reveals any existing files that might conflict.

## Step 2: Create Scripts via Heredoc

Use shell heredoc syntax to create scripts with proper escaping. Choose the heredoc delimiter style based on your needs:

**For literal content (no variable expansion):**
```bash
cat > script_name.sh << 'EOF'
#!/bin/bash
# Your script content here
# Variables like $VAR will NOT be expanded
echo "Literal text with $symbols"
EOF
```

**For content requiring variable expansion:**
```bash
cat > script_name.sh << EOF
#!/bin/bash
OUTPUT_DIR="$PWD/output"
# Variables WILL be expanded
echo "Working in $OUTPUT_DIR"
EOF
```

**Key escaping rules:**
- Use `<< 'EOF'` (quoted delimiter) to prevent variable expansion and command substitution
- Use `<< EOF` (unquoted delimiter) when you need shell variables expanded
- Escape single quotes inside quoted heredoc as `'\''`
- For nested heredocs, use different delimiters (e.g., `EOF` and `INNER`)

## Step 3: Make Executable and Run with Explicit Path

```bash
chmod +x script_name.sh
./script_name.sh
```

Or use the explicit full path for certainty:

```bash
bash /full/path/to/script_name.sh
```

## Step 4: Verify Output

Always verify file creation and inspect content:

```bash
# Check file exists and see size
ls -lh expected_output.pdf
ls -lh expected_output.docx

# For PDFs, verify structure and page count
pdfinfo expected_output.pdf

# For Word docs, inspect internal structure
unzip -l expected_output.docx | head -20
```

## Complete Example: PDF and Word Document Generation

```bash
# Step 1: Verify directory
pwd
ls -la

# Step 2: Create document generation script
cat > generate_deliverables.sh << 'EOF'
#!/bin/bash
set -e

# Create PDF checklist
cat > checklist_content.txt << 'CONTENT'
Safety Checklist
================
Page 1: Overview
Page 2: Requirements  
Page 3: Verification Steps
Page 4: Sign-off
CONTENT

# Create Word action tracker
cat > tracker_content.txt << 'CONTENT'
Action Tracker
==============
Item,Owner,Due,Status
Task 1,Team A,2024-01-15,Pending
Task 2,Team B,2024-01-20,In Progress
CONTENT

# Convert to target formats (adapt to available tools)
echo "Files created in: $(pwd)"
ls -lh *.txt
EOF

# Step 3: Execute with explicit path
chmod +x generate_deliverables.sh
./generate_deliverables.sh

# Step 4: Verify all outputs
ls -lh checklist_content.txt tracker_content.txt
# For PDFs: pdfinfo output.pdf
# For DOCX: unzip -l output.docx | head -20
```

## Efficiency Tips

1. **Combine related operations** in a single script to reduce iteration count
2. **Verify incrementally** - check each file before proceeding to the next step
3. **Use explicit paths** throughout to avoid directory confusion
4. **Test heredoc syntax** in isolation before building complex nested scripts
5. **Document expected outputs** (filenames, sizes, page counts) before running
6. **Use `set -e`** in scripts to fail fast on errors

## Common Pitfalls to Avoid

- **Unquoted heredoc delimiters** when you need literal `$` symbols
- **Missing `chmod +x`** before script execution
- **Not verifying output** before assuming success
- **Nested heredocs with same delimiter** causing premature termination
- **Assuming current directory** without explicit `pwd` verification

## Migration Back to Automated Tools

Once files are created successfully with this pattern:
1. Study what worked in the manual script
2. Translate the working approach back to `execute_code_sandbox` format
3. Test incrementally with smaller code blocks
4. Revert to automated tools for subsequent iterations

This pattern prioritizes reliability over iteration efficiency, ensuring deliverables are created correctly even when automated tools struggle.