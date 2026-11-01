---
name: ppt-generation-resilient
description: Robust PowerPoint generation with shell_agent primary approach and python-pptx fallback, including working directory verification and inline error debugging
---

# Resilient PowerPoint Generation Workflow

## When to Use This Skill

Use this workflow when:
- You need to create PowerPoint presentations with specific slide content and structure
- Direct document generation tools fail or are unavailable
- shell_agent encounters opaque errors during PowerPoint generation
- Output files may be created in unexpected nested directories within the workspace

## Overview

This workflow provides a resilient approach with two tiers:
1. **Primary**: shell_agent delegation with clear specifications
2. **Fallback**: Direct Python execution using python-pptx library with proper error capture

## Step 0: Verify Working Directory

Before starting, establish your working directory context:

```bash
# Capture current working directory
pwd
# Store for later reference
WORK_DIR=$(pwd)
echo "Working directory: $WORK_DIR"
```

## Step 1: Attempt PowerPoint Generation via shell_agent (Primary)

Delegate the PowerPoint creation task to shell_agent with explicit specifications:

```
Use shell_agent to create a PowerPoint presentation with:
- Specify the exact number of slides needed
- Define the content/topic for each slide
- Request the file be saved with a clear filename (e.g., presentation.pptx)
- Ask the agent to confirm the file path after creation using: echo "FILE_PATH: $(pwd)/presentation.pptx"
- Request that any errors be printed to stderr with full tracebacks
```

**Example task description:**
```
Create a PowerPoint presentation with 10 slides covering [topic]. Each slide should have a title and bullet points. Save it as presentation.pptx in the current working directory. After creation, run: echo "FILE_PATH: $(pwd)/presentation.pptx" and report any errors with full tracebacks.
```

**Wait for shell_agent response. If successful with file path confirmed, proceed to Step 4. If shell_agent fails or returns opaque errors, proceed to Step 2.**

## Step 2: Fallback to Direct Python Execution with python-pptx

When shell_agent fails, create and execute a Python script directly:

### 2a: Create the Python Script

Write the PowerPoint generation script to a .py file first:

```python
# save as generate_pptx.py
from pptx import Presentation
from pptx.util import Inches, Pt
import os

# Verify working directory
print(f"Working directory: {os.getcwd()}")

# Create presentation
prs = Presentation()

# Slide 1: Title slide
slide_layout = prs.slide_layouts[0]
slide = prs.slides.add_slide(slide_layout)
title = slide.shapes.title
subtitle = slide.placeholders[1]
title.text = "Your Title"
subtitle.text = "Your Subtitle"

# Additional slides (add as needed)
for i in range(2, 11):
    slide_layout = prs.slide_layouts[1]
    slide = prs.slides.add_slide(slide_layout)
    title = slide.shapes.title
    content = slide.placeholders[1]
    title.text = f"Slide {i} Title"
    content.text = f"• Bullet point 1\n• Bullet point 2\n• Bullet point 3"

# Save with full path verification
output_path = os.path.join(os.getcwd(), "presentation.pptx")
prs.save(output_path)
print(f"FILE_PATH: {output_path}")
print(f"File size: {os.path.getsize(output_path)} bytes")
```

### 2b: Execute with Full Error Capture

Run the script with stderr redirected to capture actual errors:

```bash
# Execute with full error output capture
python generate_pptx.py 2>&1 | tee execution_log.txt

# Or if using run_shell tool, ensure 2>&1 is included
python generate_pptx.py 2>&1
```

**Critical**: The `2>&1` redirect ensures Python exceptions are captured, not masked as "unknown error".

### 2c: Verify Script Execution Output

Check the execution log for:
- `FILE_PATH:` confirmation line
- File size confirmation
- Any error tracebacks in `execution_log.txt`

## Step 3: Locate the Output File

PowerPoint files may be in nested workspace directories. Use `find` to locate them:

```bash
# Find all .pptx files in the workspace
find . -name "*.pptx" -type f 2>&1

# Or search for a specific filename
find . -name "presentation.pptx" -type f 2>&1

# If not found, search broader (may require elevated permissions)
find /workspace -name "*.pptx" -type f 2>/dev/null
```

**Expected output format:**
```
./presentation.pptx
or
./workspace/task_123/output/presentation.pptx
```

## Step 4: Copy to Working Directory

Once located, copy the file to your working directory:

```bash
# Copy the found file to current directory
cp /path/to/found/presentation.pptx .

# Or move it if you prefer
mv /path/to/found/presentation.pptx .

# Verify copy succeeded
ls -la presentation.pptx
```

## Step 5: Verify the File

Confirm the file exists and is valid:

```bash
# List the file with details
ls -la presentation.pptx

# Check file size to ensure it's not empty (should be > 0 bytes)
FILE_SIZE=$(stat -f%z presentation.pptx 2>/dev/null || stat -c%s presentation.pptx 2>/dev/null)
echo "File size: $FILE_SIZE bytes"

# Verify it's a valid PowerPoint file
file presentation.pptx
# Expected: "Microsoft PowerPoint 2007+"

# If python-pptx was used, verify slide count
python -c "from pptx import Presentation; prs=Presentation('presentation.pptx'); print(f'Slide count: {len(prs.slides)}')" 2>&1
```

## Common Pitfalls

1. **Don't assume the file is in the current directory** - shell_agent may create files in subdirectories
2. **Don't skip the find step** - always verify the file location before proceeding
3. **Check file size** - ensure the PowerPoint file is not empty (0 bytes)
4. **Always use 2>&1** - redirect stderr to capture actual Python exceptions, not "unknown error"
5. **Write scripts to .py files first** - don't rely on inline execution which may have path issues
6. **Verify working directory** - capture and confirm pwd before and after script execution

## Debugging Commands

When errors occur, use these commands to diagnose:

```bash
# Check if python-pptx is available
python -c "import pptx; print(pptx.__version__)" 2>&1

# Check current directory permissions
ls -la . 
ls -la ..

# Test write permissions
touch test_write.txt && rm test_write.txt && echo "Write permissions OK"

# Check Python path
which python
python --version

# Verify no stale files interfering
find . -name "*.pptx" -type f -mtime -1 2>&1
```

## Example Workflow (Complete)

```bash
# Step 0: Verify working directory
pwd
# Output: /workspace/task_abc

# Step 1: shell_agent attempt (via task delegation)
# If shell_agent fails with opaque errors...

# Step 2: Fallback to direct Python
cat > generate_pptx.py << 'EOF'
from pptx import Presentation
import os
print(f"Working directory: {os.getcwd()}")
prs = Presentation()
slide_layout = prs.slide_layouts[0]
slide = prs.slides.add_slide(slide_layout)
slide.shapes.title.text = "Title Slide"
output_path = os.path.join(os.getcwd(), "presentation.pptx")
prs.save(output_path)
print(f"FILE_PATH: {output_path}")
EOF

python generate_pptx.py 2>&1 | tee execution_log.txt
# Output: Working directory: /workspace/task_abc
# Output: FILE_PATH: /workspace/task_abc/presentation.pptx

# Step 3: Locate (if needed)
find . -name "*.pptx" -type f
# Output: ./presentation.pptx

# Step 4: Copy (if in nested directory)
# (skip if already in working directory)

# Step 5: Verify
ls -la presentation.pptx
# Output: -rw-r--r-- 1 user user 45632 Jan 15 10:30 presentation.pptx

python -c "from pptx import Presentation; prs=Presentation('presentation.pptx'); print(f'Slide count: {len(prs.slides)}')" 2>&1
# Output: Slide count: 1
```

## Troubleshooting

| Problem | Solution |
|---------|----------|
| shell_agent returns "unknown error" | Immediately fall back to Step 2; add `2>&1` to capture actual exceptions |
| File not found with find | Search broader: `find /workspace -name "*.pptx" 2>/dev/null`; check if script executed at all |
| File is 0 bytes | Re-run Python script; check for exceptions in execution_log.txt |
| Permission denied | Check directory permissions with `ls -la`; try creating in /tmp first |
| Multiple .pptx files found | Use more specific search: `find . -name "*presentation*.pptx" -type f` |
| python-pptx not installed | Try `pip install python-pptx` or use alternative library |
| Script executes but no file created | Check working directory mismatch; add explicit `os.chdir()` in script |

## Success Criteria

The workflow is complete when:
- [ ] PowerPoint file exists in working directory
- [ ] File size > 0 bytes (typically > 20KB for multi-slide presentations)
- [ ] `file` command identifies it as Microsoft PowerPoint format
- [ ] Slide count matches requirements (verify with python-pptx if needed)
- [ ] File is readable and not corrupted
