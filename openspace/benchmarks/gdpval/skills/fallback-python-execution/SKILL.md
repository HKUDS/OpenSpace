---
name: fallback-python-execution
description: Reliable Python execution workflow when execute_code_sandbox or shell_agent fail
---

# Fallback Python Execution Pattern

## When to Use

Use this pattern when:
- `execute_code_sandbox` returns unknown errors or fails repeatedly
- `shell_agent` cannot successfully execute Python code
- You need to create files (spreadsheets, documents, data files) via Python
- Direct delegated approaches prove unreliable in the current environment

## Core Technique

Instead of delegating Python execution to agents, use this two-step inline approach:

1. **Write** Python code to a `.py` file using `write_file`
2. **Execute** the file using `run_shell` with `python <script.py>`

## Step-by-Step Instructions

### Step 1: Write Python Code to File

Use `write_file` to create a Python script with all necessary code inline:

```
write_file
path: /path/to/script.py
content: |
    import pandas as pd
    # Your complete Python code here
    df = pd.DataFrame({...})
    df.to_excel('output.xlsx', index=False)
```

### Step 2: Execute via run_shell

Run the script directly:

```
run_shell
command: python /path/to/script.py
```

### Step 3: Verify and Clean Up

- Check the output for success/errors
- Verify the expected files were created
- Optionally remove the temporary script if no longer needed

## Why This Works

This approach is more reliable because:
- Avoids agent interpretation layers that can introduce errors
- Provides direct control over execution environment
- Gives clear error output for debugging
- Bypasses sandbox delegation issues

## Example: Excel File Creation

```yaml
# Step 1: Write the script
write_file:
  path: create_report.py
  content: |
    import pandas as pd
    from openpyxl import Workbook
    
    # Create data
    data = {'Column1': [1, 2, 3], 'Column2': ['A', 'B', 'C']}
    df = pd.DataFrame(data)
    
    # Save to Excel
    df.to_excel('report.xlsx', index=False)
    print('Excel file created successfully')

# Step 2: Execute
run_shell:
  command: python create_report.py
```

## Tips

- Include error handling in your Python code for better debugging
- Use absolute paths when possible to avoid working directory issues
- Add print statements to track execution progress
- Keep scripts self-contained with all imports at the top
- For complex tasks, break into multiple scripts if needed

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Module not found | Add pip install commands before python command |
| Permission errors | Check file paths are writable |
| Script not found | Use absolute path or cd to directory first |
| Output not created | Check for Python errors in run_shell output |