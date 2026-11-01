---
name: sandbox-fallback-execution-5eda7b
description: Fallback pattern for executing Python code when sandbox execution fails by writing scripts to disk and running via shell
---

# Sandbox Fallback Execution

## Overview

When `execute_code_sandbox` fails due to e2b initialization errors or sandbox unavailability, use this fallback pattern to execute Python code by writing it to disk and running it via `run_shell`. This approach is particularly useful for PDF generation, data processing, and other Python-intensive tasks.

## When to Use

Use this skill when you encounter errors like:
- `e2b initialization error`
- `sandbox not available`
- `execute_code_sandbox` timeout or connection failures
- Any sandbox execution that consistently fails

## Step-by-Step Instructions

### Step 1: Attempt Sandbox Execution First

Always try `execute_code_sandbox` first, as it provides isolation and artifact handling:

```python
execute_code_sandbox(code="your_python_code_here")
```

### Step 2: Detect Failure and Switch to Fallback

When sandbox execution fails with initialization errors, switch to the fallback pattern:

1. **Write the Python script to disk** using `write_file`:
   - Choose a descriptive filename (e.g., `generate_pdf.py`, `process_data.py`)
   - Include the complete Python code with all necessary imports

2. **Execute via shell** using `run_shell`:
   - Run the script with `python` or `python3`
   - Capture stdout/stderr for verification

### Step 3: Example Implementation

```python
# Write the script to disk
write_file(
    path="generate_pdf.py",
    content="""
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

def create_pdf(filename, content):
    c = canvas.Canvas(filename, pagesize=letter)
    c.drawString(100, 750, content)
    c.save()

create_pdf('output.pdf', 'Hello World')
"""
)

# Execute the script via shell
run_shell(command="python generate_pdf.py")
```

### Step 4: Handle Dependencies

If the script requires external packages:

```python
# Install dependencies first
run_shell(command="pip install reportlab pillow")

# Then execute the script
run_shell(command="python generate_pdf.py")
```

### Step 5: Verify Output

After execution, verify the output was created:

```python
# Check if file was created
run_shell(command="ls -la output.pdf")

# Optionally read the file to confirm
read_file(file_path="output.pdf", filetype="pdf")
```

## Best Practices

1. **Keep scripts self-contained**: Include all imports and logic in the written file
2. **Use descriptive filenames**: Make it clear what each script does
3. **Clean up when done**: Remove temporary scripts if not needed for debugging
4. **Capture errors**: Always check stdout/stderr from `run_shell` for debugging
5. **Handle paths carefully**: Use relative paths or absolute paths consistently

## Complete Example Pattern

```python
# Primary: Try sandbox execution
try:
    result = execute_code_sandbox(code=python_code)
except Exception as e:
    if "e2b" in str(e).lower() or "sandbox" in str(e).lower():
        # Fallback: Write to disk and execute via shell
        script_path = "task_script.py"
        
        write_file(
            path=script_path,
            content=python_code
        )
        
        # Install any required dependencies
        run_shell(command="pip install -q reportlab")
        
        # Execute the script
        result = run_shell(command=f"python {script_path}")
        
        # Verify output
        run_shell(command="ls -la")
```

## Applicable Use Cases

- PDF generation (reportlab, fpdf, etc.)
- Image processing (PIL, OpenCV)
- Data analysis (pandas, numpy)
- File manipulation tasks
- Any Python script that doesn't require special sandbox features