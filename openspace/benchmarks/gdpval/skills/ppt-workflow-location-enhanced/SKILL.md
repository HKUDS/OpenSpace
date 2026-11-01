---
name: ppt-generation-resilient
description: Resilient PowerPoint generation with shell_agent primary path and python-pptx fallback
---

# Resilient PowerPoint Generation Workflow

## When to Use This Skill

Use this workflow when:
- You need to create PowerPoint presentations programmatically
- shell_agent may fail or produce opaque error messages
- You need robust error handling and debugging capabilities
- Output files may be created in unexpected nested directories

## Overview

This workflow provides two paths:
1. **Primary**: Use shell_agent with clear specifications (preferred for simple presentations)
2. **Fallback**: Direct Python execution with python-pptx library (for complex or failed shell_agent attempts)

Both paths include automatic error capture and file location verification.

---

## Path 1: shell_agent (Primary)

### Step 1.1: Delegate with Clear Specifications

```
Use shell_agent to create a PowerPoint presentation with:
- Specify exact number of slides needed
- Define content/topic for each slide
- Request file saved with clear filename (e.g., presentation.pptx)
- Ask agent to confirm the full file path after creation
- Request agent report any errors with full output
```

**Example task description:**
```
Create a PowerPoint presentation with 10 slides covering [topic]. Each slide should have a title and bullet points. Save it as presentation.pptx in the current working directory and report the full file path. If you encounter any errors, show the complete error message.
```

### Step 1.2: Verify shell_agent Success

Check if shell_agent completed successfully:
- Did it report a file path?
- Can you access the file at that path?
- Is the file size > 0 bytes?

If any check fails, proceed to **Path 2 (Fallback)**.

---

## Path 2: Direct Python with python-pptx (Fallback)

### Step 2.1: Create Python Script File

First, write the PowerPoint generation script to a .py file (do NOT use inline execution):

```python
# save as: generate_presentation.py
from pptx import Presentation
from pptx.util import Inches, Pt
import os

# CRITICAL: Explicitly set and verify working directory
working_dir = os.getcwd()
print(f"Working directory: {working_dir}")

# Create presentation
prs = Presentation()

# Example: Add slides with content
slides_content = [
    {"title": "Slide 1 Title", "content": ["Bullet 1", "Bullet 2"]},
    {"title": "Slide 2 Title", "content": ["Point A", "Point B", "Point C"]},
    # Add more slides as needed
]

for slide_data in slides_content:
    slide_layout = prs.slide_layouts[1]  # Title and Content layout
    slide = prs.slides.add_slide(slide_layout)
    
    # Set title
    title = slide.shapes.title
    title.text = slide_data["title"]
    
    # Set content
    content = slide.placeholders[1]
    tf = content.text_frame
    tf.clear()  # Clear existing content
    
    for i, point in enumerate(slide_data["content"]):
        if i == 0:
            p = tf.paragraphs[0]
        else:
            p = tf.add_paragraph()
        p.text = point
        p.font.size = Pt(18)

# Save to current working directory with explicit path
output_filename = "presentation.pptx"
output_path = os.path.join(working_dir, output_filename)
prs.save(output_path)

print(f"Presentation saved to: {output_path}")
print(f"File exists: {os.path.exists(output_path)}")
print(f"File size: {os.path.getsize(output_path)} bytes")
```

### Step 2.2: Execute with Error Output Capture

**CRITICAL**: Always redirect stderr to stdout to capture actual error messages:

```bash
# Execute the script with full error output
python generate_presentation.py 2>&1

# Or if using run_shell tool, ensure 2>&1 is included
```

**Why this matters:** Without `2>&1`, Python exceptions are sent to stderr and you only see "unknown error" instead of the actual exception message.

### Step 2.3: Debug Common Issues

If execution fails, add these debugging commands:

```bash
# Check if python-pptx is installed
python -c "import pptx; print(pptx.__version__)" 2>&1

# Check working directory
pwd
ls -la

# Check Python version
python --version

# Run with verbose error output
python -u generate_presentation.py 2>&1 | tee execution_log.txt
```

---

## Step 3: Locate the Output File (Both Paths)

PowerPoint files may be in nested workspace directories. Always verify location:

```bash
# Find all .pptx files in workspace
find . -name "*.pptx" -type f 2>&1

# Or search for specific filename
find . -name "presentation.pptx" -type f 2>&1

# If not found locally, search broader (last resort)
find /workspace -name "*.pptx" -type f 2>/dev/null
```

---

## Step 4: Copy to Working Directory

Once located, copy the file to your working directory:

```bash
# Get the full path from find output
FILE_PATH=$(find . -name "presentation.pptx" -type f | head -1)

# Copy to current directory
cp "$FILE_PATH" . 2>&1

# Or move it
mv "$FILE_PATH" . 2>&1
```

---

## Step 5: Verify the File

Confirm the file exists and is valid:

```bash
# List file with details
ls -la presentation.pptx

# Check file size (should NOT be 0 bytes)
file presentation.pptx
stat presentation.pptx | grep Size

# Verify it's a valid PowerPoint file
python -c "from pptx import Presentation; prs = Presentation('presentation.pptx'); print(f'Slides: {len(prs.slides)}')" 2>&1
```

---

## Common Pitfalls & Solutions

| Problem | Solution |
|---------|----------|
| shell_agent returns "unknown error" | Switch to Path 2 (direct Python) immediately |
| Python script fails silently | Add `2>&1` to capture stderr; use `python -u` for unbuffered output |
| File not found after generation | Use `find . -name "*.pptx"` - don't assume current directory |
| File is 0 bytes | Re-run generation; check for exceptions in error output |
| Working directory confusion | Always `print(os.getcwd())` in Python scripts; use absolute paths |
| Module not found (pptx) | Install with `pip install python-pptx` or use alternative environment |
| Permission denied | Check directory permissions: `ls -la` on parent dirs; use `/workspace/` prefix |

---

## Decision Tree

```
Start
  │
  └─→ Try shell_agent with clear specs
        │
        ├─→ Success + File path reported? ──→ Verify file → Done
        │
        └─→ Failed or opaque error?
              │
              └─→ Switch to direct Python (Path 2)
                    │
                    ├─→ Write script to .py file
                    ├─→ Execute with 2>&1 redirect
                    ├─→ Capture and analyze errors
                    ├─→ Fix and retry (max 3 attempts)
                    │
                    └─→ Success? → Locate → Copy → Verify → Done
                    └─→ Still failing? → Report actual error messages
```

---

## Example: Complete Workflow

```bash
# Attempt 1: shell_agent (via task delegation)
# [Agent creates presentation, reports path or error]

# If shell_agent failed, proceed to Path 2:

# Step 1: Write Python script
cat > generate_presentation.py << 'EOF'
from pptx import Presentation
import os
print(f"Working directory: {os.getcwd()}")
prs = Presentation()
slide = prs.slides.add_slide(prs.slide_layouts[0])
slide.shapes.title.text = "Title Slide"
prs.save("presentation.pptx")
print(f"Saved: presentation.pptx")
EOF

# Step 2: Execute with error capture
python generate_presentation.py 2>&1
# Output: Working directory: /workspace/task_123
#         Saved: presentation.pptx

# Step 3: Locate (in case it's nested)
find . -name "presentation.pptx" -type f
# Output: ./presentation.pptx

# Step 4: Verify
ls -la presentation.pptx
# Output: -rw-r--r-- 1 user user 45632 Jan 15 10:30 presentation.pptx

python -c "from pptx import Presentation; print(len(Presentation('presentation.pptx').slides))"
# Output: 1
```

---

## Troubleshooting: Detailed Error Diagnosis

### When you see "unknown error" from run_shell:

```bash
# This is NOT helpful - stderr is hidden
python script.py

# This CAPTURES the actual error
python script.py 2>&1

# This CAPTURES and LOGS for analysis
python script.py 2>&1 | tee error_log.txt
```

### When working directory is unclear:

```python
# Always include this at script start
import os
print(f"CWD: {os.getcwd()}")
print(f"Files in CWD: {os.listdir('.')}")

# Use absolute paths for output
output_path = os.path.join(os.getcwd(), "presentation.pptx")
```

### When multiple .pptx files exist:

```bash
# Find most recently created
find . -name "*.pptx" -type f -printf '%T@ %p\n' | sort -n | tail -1

# Or find by name pattern
find . -name "*presentation*.pptx" -type f
```

---

## Tool-Specific Notes

### run_shell
- ALWAYS include `2>&1` to capture Python exceptions
- Consider `python -u` for unbuffered output (real-time error display)
- Use `| tee logfile.txt` to preserve error output for analysis

### shell_agent
- Request explicit file path confirmation in task description
- Ask agent to report full error messages if generation fails
- Be prepared to switch to Path 2 if errors are opaque

### execute_code_sandbox
- May have module availability issues (e.g., missing python-pptx)
- Prefer run_shell with explicit script file for complex tasks
- Verify module availability before relying on this tool

---

## Success Criteria

A successful PowerPoint generation workflow:
- [ ] File exists at known path
- [ ] File size > 0 bytes (typically > 10KB for multi-slide)
- [ ] File can be opened by python-pptx without errors
- [ ] Expected number of slides present
- [ ] Slide titles match specifications
*** End Files
