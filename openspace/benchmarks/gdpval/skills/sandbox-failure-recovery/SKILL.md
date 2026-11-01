---
name: sandbox-failure-recovery
description: Recover from execute_code_sandbox failures by writing code to file and executing via run_shell
---

# Sandbox Failure Recovery Workflow

When `execute_code_sandbox` fails (often due to infrastructure issues, timeouts, or complex dependencies), use this recovery pattern to achieve identical results by writing the code to a file and executing it directly.

## When to Use This Skill

- `execute_code_sandbox` returns an error or times out
- The task involves spreadsheet generation (Excel, CSV) or file output
- You have working Python code that needs to be executed
- The sandbox environment appears unstable

## Step-by-Step Instructions

### Step 1: Preserve the Failed Code

When `execute_code_sandbox` fails, capture the Python code that was attempted. If the code was generated but not saved, reconstruct it from the execution attempt.

### Step 2: Write Code to File

Use `write_file` to save the Python script to a `.py` file:

```
write_file with:
- path: "script_name.py" (e.g., "generate_report.py", "process_data.py")
- content: <the full Python code>
```

**Example:**
```python
# Content for write_file
import pandas as pd
from openpyxl import Workbook

# Your spreadsheet generation logic here
df = pd.DataFrame({'Revenue': [100, 200, 300]})
df.to_excel('output.xlsx', index=False)
```

### Step 3: Execute via run_shell

Run the saved script using `run_shell` with Python 3:

```
run_shell with:
- command: "python3 script_name.py"
- timeout: 60 (or higher for complex operations)
```

### Step 4: Verify Output

Check that the expected output files were created:

```
list_dir with:
- path: "."
```

Or read the generated file to confirm correctness:

```
read_file with:
- file_path: "output.xlsx"
- filetype: "xlsx"
```

## Complete Example

**Scenario:** Generate an Excel P&L report after sandbox failure.

```yaml
# Step 1: Write the Python script
write_file:
  path: "generate_pnl_report.py"
  content: |
    import pandas as pd
    from openpyxl import Workbook
    
    # Create revenue data
    data = {
        'Tour Stop': ['London', 'Paris', 'Berlin'],
        'Revenue': [50000, 45000, 38000],
        'Withholding Tax': [5000, 4500, 3800],
        'Expenses': [12000, 11000, 9500]
    }
    df = pd.DataFrame(data)
    df['Net Income'] = df['Revenue'] - df['Withholding Tax'] - df['Expenses']
    
    # Export to Excel
    df.to_excel('pnl_report.xlsx', index=False)
    print("Report generated successfully")

# Step 2: Execute the script
run_shell:
  command: "python3 generate_pnl_report.py"
  timeout: 60

# Step 3: Verify
list_dir:
  path: "."
```

## Best Practices

1. **Use descriptive filenames** - Name files after their purpose (e.g., `generate_report.py`, `process_spreadsheet.py`)

2. **Set appropriate timeouts** - Complex operations may need 60+ seconds

3. **Include error handling in your Python code** - Add try/except blocks to capture and report issues:
   ```python
   try:
       # Your code here
   except Exception as e:
       print(f"Error: {e}")
       raise
   ```

4. **Clean up temporary files** - After successful execution, you may remove the `.py` file if not needed

5. **Check dependencies** - Ensure required packages (pandas, openpyxl, etc.) are available in the shell environment

## Why This Works

- `run_shell` executes in a more stable environment than the sandbox
- File-based execution avoids sandbox memory/resource constraints
- The Python interpreter in the shell has full access to installed packages
- Output files are written directly to the workspace

<THIS_SKILL_EVOLVED>