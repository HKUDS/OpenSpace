---
name: excel-creation-fallback
description: Use shell_agent as fallback when execute_code_sandbox fails for Excel file operations
---

# Excel File Creation Fallback

When `execute_code_sandbox` fails for openpyxl or Excel operations, delegate to `shell_agent` which can autonomously handle dependency and environment issues.

## When to Use

- `execute_code_sandbox` fails with import errors for openpyxl or related libraries
- The sandbox environment lacks required dependencies
- You need to create or modify Excel files with complex requirements
- Repeated sandbox execution failures for file I/O operations

## Steps

### Step 1: Attempt with execute_code_sandbox first

Try creating the Excel file using Python's openpyxl library in the code sandbox:

```python
from openpyxl import Workbook

wb = Workbook()
ws = wb.active
ws.append(['Column1', 'Column2', 'Column3'])
wb.save('output.xlsx')
```

### Step 2: If it fails, switch to shell_agent

Delegate the task to shell_agent with a clear, comprehensive task description:

```
Create an Excel file named 'output.xlsx' with the following structure:
- Sheet 1: Data with columns [Date, Metric, Value]
- Include sample data rows with realistic values
- Apply basic formatting (bold headers, cell borders)
- Add a summary section with totals or averages
- Save the file in the current directory
```

### Step 3: Let shell_agent handle the environment

The shell_agent will:
- Decide whether to use Python or Bash
- Install dependencies if needed (e.g., `pip install openpyxl`)
- Write and execute the code
- Automatically retry and fix errors (up to several rounds)
- Confirm the file was created successfully

## Example Task Descriptions

### Basic Excel file:
```
Create an Excel file named 'sales_report.xlsx' with headers: Date, Product, Quantity, Price, Total. Add 10 sample rows of data and a formula column for Total (Quantity * Price).
```

### Complex Excel with formatting:
```
Create an Excel file with multiple sheets:
- Sheet 'Summary': Key metrics and totals
- Sheet 'Details': Full transaction data with columns [ID, Date, Customer, Amount, Status]
- Apply conditional formatting to highlight amounts over 1000
- Add borders to all cells and bold headers
```

### Tiered pricing structure:
```
Create an Excel file with a tiered pricing table:
- Column A: Quantity thresholds (0, 100, 500, 1000)
- Column B: Unit price at each tier
- Column C: Discount percentage (e.g., 15% discount over 1000 units)
- Include a financial summary section with projections
```

## Why This Pattern Works

`shell_agent` has several advantages over `execute_code_sandbox` for file creation tasks:

| Feature | execute_code_sandbox | shell_agent |
|---------|---------------------|-------------|
| Dependency installation | Manual/preset only | Autonomous |
| Error recovery | Returns error | Auto-retries and fixes |
| Tool selection | Python only | Python, Bash, or other |
| Filesystem access | Sandbox-limited | Full workspace access |
| Verification | None | Can verify file creation |

## Troubleshooting

If shell_agent also struggles:

1. **Be more specific** - Include exact column names, data types, and formatting requirements
2. **Provide sample data** - Include example rows to clarify expected output
3. **Break it down** - For complex files, request creation in stages
4. **Check permissions** - Ensure the target directory is writable

## Alternative Approaches

If shell_agent is unavailable or unsuitable:

- Use `run_shell` with explicit commands (if you know the exact syntax)
- Check for alternative libraries (xlsxwriter, pandas with openpyxl engine)
- Use CSV as intermediate format, then convert to Excel