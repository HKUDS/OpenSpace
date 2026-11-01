---
name: ppt-reliable-creation
description: Robust PowerPoint generation with shell_agent primary and python-pptx fallback, including directory verification and error debugging
---

# Reliable PowerPoint Creation Workflow

## When to Use This Skill

Use this workflow when:
- You need to create PowerPoint presentations with specific slide content and structure
- shell_agent may fail or produce opaque error messages
- Output files may be created in unexpected nested directories
- You need a reliable fallback when primary generation methods fail

## Overview: Dual-Path Strategy

This skill provides two paths:
1. **Primary Path**: shell_agent delegation (simpler, but may fail)
2. **Fallback Path**: Direct Python execution with python-pptx (more reliable, more control)

## Step 0: Pre-Check Working Directory

Before any generation, verify your current working directory:

```bash
# Check and record current directory
pwd
# Output should be saved for later reference

# List current contents to establish baseline
ls -la
```

**Important:** Save the pwd output. You'll need this to verify where files are created.

## Step 1: Primary Path - shell_agent Generation

Attempt PowerPoint creation via shell_agent first (quickest if successful):

```
Use shell_agent to create a PowerPoint presentation with:
- Specify the exact number of slides needed
- Define the content/topic for each slide
- Request the file be saved with a clear filename (e.g., presentation.pptx)
- Ask the agent to confirm the full file path after creation
```

**Example task description:**
```
Create a PowerPoint presentation with 10 slides covering [topic]. Each slide should have a title and bullet points. Save it as presentation.pptx and tell me the full file path where you saved it.
```

### shell_agent Error Handling

If shell_agent returns "unknown error" or fails:

```bash
# Capture actual error by adding stderr redirect
# Re-run the task or check previous error output
# Add 2>&1 to any shell commands to see real errors
```

**After 2 failures or opaque errors, switch to Fallback Path (Step 2).**

## Step 2: Fallback Path - Direct Python with python-pptx

When shell_agent fails, use direct Python execution. **Always write script to file first, then execute.**

### 2a: Create the Python Script

Write a complete Python script using python-pptx:

```python
from pptx import Presentation
from pptx.util import Inches, Pt

# Create presentation
prs = Presentation()

# Slide 1: Title slide
slide_layout = prs.slide_layouts[0]
slide = prs.slides.add_slide(slide_layout)
title = slide.shapes.title
subtitle = slide.placeholders[1]
title.text = "Title Here"
subtitle.text = "Subtitle Here"

# Slide 2+: Content slides
for i in range(2, 11):
    slide_layout = prs.slide_layouts[1]
    slide = prs.slides.add_slide(slide_layout)
    title = slide.shapes.title
    title.text = f"Slide {i} Title"
    
    # Add content
    content = slide.placeholders[1]
    content.text = f"Content for slide {i}"

# Save to current working directory
prs.save('presentation.pptx')
print("File saved to:", os.path.abspath('presentation.pptx'))
```

**Save this script as: create_presentation.py**

### 2b: Execute with Error Capture

```bash
# Write script to file first, then execute with stderr capture
python create_presentation.py 2>&1 | tee execution_log.txt

# Check for success
if [ -f "presentation.pptx" ]; then
    echo "SUCCESS: File created in current directory"
    ls -la presentation.pptx
else
    echo "FAILED: Check execution_log.txt for errors"
    cat execution_log.txt
fi
```

### 2c: Verify Working Directory Match

```bash
# Confirm file is where expected
CURRENT_PWD=$(pwd)
FILE_PATH=$(realpath presentation.pptx 2>/dev/null)

if [[ "$FILE_PATH" == "$CURRENT_PWD"* ]]; then
    echo "✓ File is in expected directory"
else
    echo "⚠ File created elsewhere: $FILE_PATH"
    echo "Current directory: $CURRENT_PWD"
fi
```

## Step 3: Locate the Output File

PowerPoint files may be created in nested directories. Always search before assuming location:

```bash
# Search for all .pptx files in workspace
find . -name "*.pptx" -type f 2>/dev/null

# Search for specific filename
find . -name "presentation.pptx" -type f 2>/dev/null

# If not found, broaden search
find . -name "*.pptx" -type f -mmin -5 2>/dev/null
```

## Step 4: Copy to Working Directory

Once located, bring the file to your known working directory:

```bash
# Found path example: ./workspace/task_123/output/presentation.pptx
FOUND_PATH=$(find . -name "presentation.pptx" -type f | head -1)

if [ -n "$FOUND_PATH" ]; then
    cp "$FOUND_PATH" .
    echo "Copied from: $FOUND_PATH"
else
    echo "ERROR: File not found - check Python script execution"
fi
```

## Step 5: Verify the File

Confirm the file exists and is valid:

```bash
# Check file exists and size
ls -la presentation.pptx

# Verify not empty (PowerPoint files should be >1KB minimum)
FILE_SIZE=$(stat -f%z presentation.pptx 2>/dev/null || stat -c%s presentation.pptx 2>/dev/null)
if [ "$FILE_SIZE" -gt 1000 ]; then
    echo "✓ File size OK: $FILE_SIZE bytes"
else
    echo "⚠ WARNING: File may be empty or corrupted ($FILE_SIZE bytes)"
fi

# Check file type
file presentation.pptx
```

## Debugging Commands

Use these when errors occur:

```bash
# Capture full error output from any command
python script.py 2>&1 | tee error_log.txt

# Check Python is available and python-pptx installed
python -c "from pptx import Presentation; print('python-pptx OK')" 2>&1

# List all .pptx files with details
find . -name "*.pptx" -type f -exec ls -la {} \; 2>/dev/null

# Check write permissions in current directory
touch test_write.txt && rm test_write.txt && echo "Write OK" || echo "Write FAILED"

# Show full path context
pwd
ls -la
```

## Common Pitfalls

| Pitfall | Solution |
|---------|----------|
| shell_agent returns opaque errors | Switch to Fallback Path after 2 failures; add 2>&1 for real errors |
| Assuming file is in current directory | Always run find command before proceeding |
| Script runs but file not found | Check python script's save path; verify working directory |
| File is 0 bytes | Re-run Python script; check for exceptions in error log |
| Multiple .pptx files found | Use specific search: `find . -name "*presentation*.pptx" -mmin -5` |
| No module named pptx | Install: `pip install python-pptx` or use execute_code_sandbox |

## Decision Tree

```
Start
  │
  ├─> Try shell_agent (Step 1)
  │     │
  │     ├─> Success with path confirmed → Go to Step 4
  │     │
  │     └─> Failure or opaque error (2×) → Switch to Fallback
  │           │
  │           └─> Direct Python (Step 2)
  │                 │
  │                 ├─> Success → Go to Step 4
  │                 │
  │                 └─> Failure → Debug with commands above
  │
  └─> Always: locate with find, copy, verify (Steps 3-5)
```

## Example Complete Workflow

```bash
# Pre-check
pwd  # Save output: /workspace/current_task

# Try shell_agent (via task delegation)
# If fails after 2 attempts:

# Create Python script
cat > create_presentation.py << 'EOF'
from pptx import Presentation
prs = Presentation()
# ... add slides ...
prs.save('presentation.pptx')
print("Saved to:", __import__('os').path.abspath('presentation.pptx'))
EOF

# Execute with error capture
python create_presentation.py 2>&1 | tee exec.log

# Locate (may be in subdirectory)
find . -name "presentation.pptx" -type f
# Output: ./generated/presentation.pptx

# Copy to working directory
cp ./generated/presentation.pptx .

# Verify
ls -la presentation.pptx
file presentation.pptx
```

## Troubleshooting Table

| Problem | Command | Expected Output | Action |
|---------|---------|-----------------|--------|
| python-pptx not installed | `python -c "from pptx import Presentation"` | ImportError | Install: `pip install python-pptx` |
| Permission denied | `touch test.txt && rm test.txt` | Success | Check directory permissions with `ls -la` |
| File not found | `find . -name "*.pptx" -type f` | Path or empty | Re-run script; check error log |
| Opaque error | `python script.py 2>&1` | Real exception | Fix Python code based on error message |
| Multiple files | `find . -name "*.pptx" -type f` | Multiple paths | Use `-mmin -5` to find recent files |

## Best Practices

1. **Always verify working directory** before and after generation
2. **Add 2>&1** to all Python/shell commands to capture real errors
3. **Write scripts to .py files first**, then execute (don't use inline execution for complex tasks)
4. **Search with find** before assuming file location
5. **Check file size** to ensure content was actually written
6. **Keep error logs** (tee to file) for debugging complex failures
7. **Fallback after 2 shell_agent failures** - don't waste iterations

## When to Skip shell_agent Entirely

Use direct Python (Step 2) immediately when:
- Presentation requires complex tables, charts, or custom layouts
- Previous shell_agent attempts failed on similar tasks
- You need precise control over slide structure
- You're creating presentations with data from spreadsheets/CSVs
- Task requires multiple iterations and you want faster feedback
