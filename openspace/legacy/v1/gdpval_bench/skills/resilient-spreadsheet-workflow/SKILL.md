---
name: resilient-spreadsheet-workflow
description: Resilient multi-step workflow for spreadsheet processing when execute_code_sandbox fails, using shell_agent exploration, file-based scripts, and verification steps
---

# Resilient Spreadsheet Workflow

This skill provides a robust workflow for processing spreadsheet files (CSV, XLSX) when `execute_code_sandbox` encounters failures. It uses a combination of tools to ensure reliable execution and clear error diagnosis.

## When to Use

- Processing spreadsheet files when `execute_code_sandbox` returns unknown errors
- Need to debug why spreadsheet operations fail
- Require reliable file I/O with verification at each step

## Step-by-Step Workflow

### Step 1: Initial Exploration with shell_agent

Start by using `shell_agent` to explore the file structure and understand what files exist:

```
Use shell_agent to:
- List files in the working directory
- Identify spreadsheet files (CSV, XLSX)
- Examine file sizes and basic structure
```

**Example task for shell_agent:**
```
Explore the current directory to find all spreadsheet files (CSV, XLSX). 
List their sizes and identify which files need processing.
```

### Step 2: Write Processing Script with write_file

Create a standalone script file rather than executing inline code:

```python
# Use write_file to create a script like "process_sheet.py"
content = """
import pandas as pd
import sys

try:
    df = pd.read_csv('input.csv')  # or pd.read_excel for XLSX
    # Perform your processing
    result = df.describe()
    result.to_csv('output.csv', index=False)
    print("Processing complete")
except Exception as e:
    print(f"Error: {e}", file=sys.stderr)
    sys.exit(1)
"""

write_file(path="process_sheet.py", content=content)
```

### Step 3: Execute with run_shell and Error Redirection

Execute the script with stderr redirected to stdout for complete error capture:

```bash
run_shell command="python process_sheet.py 2>&1"
```

The `2>&1` redirection ensures both stdout and stderr are captured together, making error messages visible.

### Step 4: Isolate Errors with Verification Commands

If Step 3 fails, add targeted verification commands to diagnose the actual problem:

```bash
# Check if Python is available
run_shell command="which python 2>&1"

# Check if pandas is installed
run_shell command="python -c 'import pandas' 2>&1"

# Verify input file exists and is readable
run_shell command="ls -la input.csv 2>&1"

# Check file encoding/content
run_shell command="file input.csv 2>&1"
run_shell command="head -5 input.csv 2>&1"
```

### Step 5: Verify Output Before Reading

Before reading results with `read_file`, verify the output file exists:

```
list_dir(path=".")
# Confirm output file appears in the listing
# Then proceed to read_file
```

## Complete Example

```
1. shell_agent(task="Find and describe all CSV/XLSX files in current directory")
2. write_file(path="analyze_data.py", content="<processing script>")
3. run_shell(command="python analyze_data.py 2>&1")
4. If error: run_shell(command="python -c 'import pandas; print(pandas.__version__)' 2>&1")
5. list_dir(path=".")  # Verify output exists
6. read_file(filetype="csv", file_path="output.csv")
```

## Common Error Patterns

| Symptom | Diagnostic Command | Likely Cause |
|---------|-------------------|--------------|
| Module not found | `python -c 'import pandas'` | Missing dependency |
| File not found | `ls -la <filename>` | Wrong path or name |
| Permission denied | `ls -la <filename>` | File permissions |
| Encoding error | `file <filename>` | Wrong file format |

## Best Practices

1. **Always use 2>&1** with run_shell for shell commands to capture full error output
2. **Verify before reading** - use list_dir to confirm file existence before read_file
3. **Break into small steps** - separate exploration, script writing, execution, and verification
4. **Use shell_agent for unknowns** - when directory structure or file types are unclear
5. **Write scripts to files** - more reliable than inline code execution for complex operations