---
name: spreadsheet-python-file-exec
description: Execute Python scripts from files for reliable spreadsheet operations, avoiding heredoc issues with shell_agent
---

# Direct Python Execution for Spreadsheet Tasks

## When to Use This Skill

Use direct `run_shell` with Python scripts for spreadsheet operations when:

- Reading or writing complex Excel files with multiple sheets
- Applying formulas, formatting, or data transformations
- Working with `openpyxl`, `pandas`, or similar libraries
- The operation involves multiple steps that could exceed agent step limits
- You need precise control over error handling and debugging
- Complex scripts benefit from file-based execution for better reliability

## Why Direct Execution?

The `shell_agent` tool can:

- Hit maximum step limits on complex multi-step operations
- Produce unexplained errors on formatting operations
- Fail on intricate spreadsheet reads/writes due to iterative parsing
- Fail to parse heredoc syntax correctly, causing 'unknown error' failures

Direct `run_shell` with Python is more reliable because it:

- Executes in a single step with no iteration limits
- Provides clearer, immediate error messages
- Handles complex operations without step constraints
- Gives full control over library imports and execution flow
- Writing scripts to `.py` files first avoids shell_agent parsing issues with heredocs

## How to Use

### Recommended Pattern: Write Script to File First

For complex multi-line scripts, especially when using shell_agent as executor:

```bash
# Step 1: Write the Python script to a file
cat > process_spreadsheet.py << 'EOF'
import openpyxl
from openpyxl import Workbook

# Your spreadsheet code here
wb = openpyxl.load_workbook('file.xlsx')
# ... operations ...
wb.save('output.xlsx')
print('Success')
EOF

# Step 2: Execute the script
python3 process_spreadsheet.py
```

### Alternative Pattern: Inline Heredoc (Simple Scripts Only)

For short, simple scripts when NOT using shell_agent as the executor:

```bash
python3 << 'EOF'
import openpyxl
from openpyxl import Workbook

# Your spreadsheet code here
wb = openpyxl.load_workbook('file.xlsx')
# ... operations ...
wb.save('output.xlsx')
print('Success')
EOF
```

### Example 1: Read and Transform Excel Data

**Write to file first, then execute:**

```python
import pandas as pd

# Load data from specific sheet
df = pd.read_excel('input.xlsx', sheet_name='Revenue')

# Apply transformations
df['Net_Revenue'] = df['Gross_Revenue'] * (1 - df['Tax_Rate'])

# Save results
df.to_excel('output.xlsx', index=False, sheet_name='Processed')
```

### Example 2: Multi-Sheet Operations with openpyxl

**Write to file first, then execute:**

```python
from openpyxl import load_workbook

wb = load_workbook('tour_data.xlsx')

# Iterate through sheets
for sheet_name in wb.sheetnames:
    ws = wb[sheet_name]
    # Apply formatting or calculations
    for row in ws.iter_rows(min_row=2, max_col=5):
        # Process cells
        pass

wb.save('tour_data_processed.xlsx')
```

### Example 3: Complex Formatting Operations

**Write to file first, then execute:**

```python
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment

wb = load_workbook('report.xlsx')
ws = wb.active

# Apply header styling
header_fill = PatternFill(start_color='4472C4', fill_type='solid')
header_font = Font(bold=True, color='FFFFFF')

for cell in ws[1]:
    cell.fill = header_fill
    cell.font = header_font
    cell.alignment = Alignment(horizontal='center')

wb.save('report_formatted.xlsx')
```

### Example 4: Error Handling Pattern

**Write to file first, then execute:**

```python
import sys
from openpyxl import load_workbook

try:
    wb = load_workbook('data.xlsx')
    ws = wb.active
    
    # Your operations here
    value = ws['A1'].value
    
    wb.save('output.xlsx')
    print(f"Success: Processed {ws.max_row} rows")
    
except Exception as e:
    print(f"Error: {str(e)}", file=sys.stderr)
    sys.exit(1)
```

## Best Practices

1. **Prefer file-based execution** for complex scripts: write to `.py` file first, then execute via `run_shell`
2. **Import only needed libraries** to reduce execution time
3. **Print clear success/error messages** for debugging
4. **Save intermediate results** for complex multi-step transformations
5. **Test with small data** before scaling to large spreadsheets
6. **Use pandas for data manipulation** and openpyxl for formatting when both are needed
7. **Clean up temporary script files** after execution if they won't be reused

## When NOT to Use This Skill

- Simple single-cell reads/writes (use shell_agent or basic commands)
- Operations that require interactive user input
- Tasks where you need the agent to iteratively refine the approach

## Common Libraries

| Library | Best For |
|---------|----------|
| `openpyxl` | Reading/writing .xlsx files, formatting, formulas |
| `pandas` | Data manipulation, analysis, merging datasets |
| `xlrd` | Reading older .xls files (read-only) |
| `xlsxwriter` | Creating new .xlsx files with advanced formatting |

## Troubleshooting

**Issue**: Heredoc syntax fails with 'unknown error' when using shell_agent
- **Solution**: Write the Python script to a `.py` file first, then execute it with `python3 script.py`. This pattern is significantly more reliable than inline heredoc execution when shell_agent is the executor.

**Issue**: FileNotFoundError
- **Solution**: Verify the file path is absolute or relative to the working directory

**Issue**: PermissionError
- **Solution**: Ensure the file is not open in another application

**Issue**: MemoryError on large files
- **Solution**: Process data in chunks using pandas `chunksize` parameter

**Issue**: Formatting not applying
- **Solution**: Ensure you're modifying cell styles before saving, and use `.copy()` for style objects
