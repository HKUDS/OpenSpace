---
name: pdf-debug-unbuffered
description: Debug Python PDF generation errors by using unbuffered output and stderr inspection to reveal actual tracebacks
---

# PDF Generation Debug Technique

When Python PDF libraries (reportlab, fpdf, etc.) fail with generic "unknown error" messages, the actual cause is often hidden due to output buffering or error swallowing. This skill provides a technique to expose the real traceback.

## Core Technique

Run the Python script with the `-u` flag (unbuffered output) and pipe stderr through `head` to capture the actual error:

```bash
python -u your_script.py 2>&1 | head -100
```

Or more specifically, to focus on stderr:

```bash
python -u your_script.py 2>&1 | head -50
```

## Why This Works

1. **`-u` flag**: Forces Python to run in unbuffered mode, ensuring output (including errors) is flushed immediately rather than held in buffers that may be lost on crash
2. **`2>&1`**: Redirects stderr to stdout so both streams are captured together
3. **`head`**: Limits output to show the most relevant error messages at the top

## Step-by-Step Debugging

### Step 1: Identify the failing script
Locate the Python script that generates the PDF and is producing generic errors.

### Step 2: Run with unbuffered output
```bash
python -u generate_pdf.py 2>&1 | head -100
```

### Step 3: Analyze the traceback
Look for:
- Import errors (missing dependencies)
- File path issues (invalid paths, permission errors)
- Font registration problems (common with reportlab)
- Memory or resource constraints

### Step 4: Fix the underlying issue
Address the specific error revealed in the traceback.

### Step 5: Re-run normally
Once fixed, run the script without the debug flags to confirm it works:
```bash
python generate_pdf.py
```

## Common Issues Revealed

| Symptom | Likely Cause |
|---------|--------------|
| "unknown error" on PDF save | File path doesn't exist or no write permissions |
| Generic failure during build | Missing font files or font registration issues |
| Silent crash | ImportError for missing dependencies |
| Incomplete PDF | Script terminated early due to unhandled exception |

## Alternative Approaches

If the above doesn't work, try:

```bash
# Capture full stderr to a file
python -u script.py 2> error.log

# Use Python's traceback module explicitly
python -c "import traceback; exec(open('script.py').read())" 2>&1

# Run with verbose import tracing
python -v -u script.py 2>&1 | head -200
```

## Example: Debugging reportlab

```bash
# Before: Generic error
python create_checklist.py
# Output: "Error: unknown"

# After: Actual traceback  
python -u create_checklist.py 2>&1 | head -50
# Output: "FileNotFoundError: [Errno 2] No such file or directory: '/fonts/Helvetica.ttf'"
```

## Notes

- This technique applies to any Python script with opaque error messages, not just PDF generation
- The `-u` flag is available in Python 2.7+ and all Python 3 versions
- For production debugging, consider logging to a file instead of using `head`