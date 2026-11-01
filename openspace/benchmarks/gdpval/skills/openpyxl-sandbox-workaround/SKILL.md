---
name: openpyxl-sandbox-workaround
description: Use run_shell with inline Python as a fallback when execute_code_sandbox fails for openpyxl/spreadsheet operations
---

# Openpyxl Sandbox Workaround

## When to Use

Use this pattern when `execute_code_sandbox` repeatedly fails for openpyxl or spreadsheet manipulation tasks. The sandbox environment may have compatibility issues with certain openpyxl operations, but running Python directly via `run_shell` often succeeds.

## Common Failure Indicators

- `execute_code_sandbox` returns errors related to openpyxl imports or operations
- Multiple retry attempts fail with similar errors
- Errors mention workbook creation, cell writing, or file saving issues

## The Workaround Pattern

Instead of using `execute_code_sandbox`, use `run_shell` with an inline Python script:

```bash
python3 << 'EOF'
from openpyxl import Workbook
from openpyxl.utils import get_column_letter

# Your openpyxl code here
wb = Workbook()
ws = wb.active
ws.title = "Sheet1"

# Add data
ws['A1'] = 'Header'
ws['B1'] = 'Value'

# Save file
wb.save('output.xlsx')
print('File created successfully')
EOF
```

## Multi-Step Spreadsheet Tasks

For complex operations involving multiple files or data processing:

```bash
python3 << 'EOF'
from openpyxl import Workbook, load_workbook
import os

# Load existing workbook if needed
if os.path.exists('input.xlsx'):
    wb = load_workbook('input.xlsx')
    ws = wb.active
    # Process data...
    
# Create new workbook
wb = Workbook()
ws = wb.active

# Add data with proper formatting
for row_idx, row_data in enumerate(data, start=1):
    for col_idx, value in enumerate(row_data, start=1):
        ws.cell(row=row_idx, column=col_idx, value=value)

# Auto-adjust column widths
for column in ws.columns:
    max_length = 0
    column_letter = get_column_letter(column[0].column)
    for cell in column:
        try:
            if len(str(cell.value)) > max_length:
                max_length = len(str(cell.value))
        except:
            pass
    adjusted_width = min(max_length + 2, 50)
    ws.column_dimensions[column_letter].width = adjusted_width

wb.save('output.xlsx')
EOF
```

## Best Practices

1. **Use heredoc syntax**: The `<< 'EOF'` pattern prevents variable expansion issues
2. **Print success messages**: Always include print statements to confirm completion
3. **Handle file paths**: Use absolute paths or ensure you're in the correct working directory
4. **Validate output**: After creation, verify the file exists and has expected content
5. **Error handling**: Add try/except blocks for robustness in complex scripts

## Verification Step

After running the script, verify the file was created:

```bash
ls -la *.xlsx
```

Or check file details:

```bash
python3 << 'EOF'
from openpyxl import load_workbook
wb = load_workbook('output.xlsx')
print(f"Sheets: {wb.sheetnames}")
print(f"Active sheet: {wb.active.title}")
EOF
```

## When to Fall Back to This Pattern

- First `execute_code_sandbox` attempt fails with openpyxl errors
- Error persists after 1-2 retries
- Task is time-sensitive and needs a reliable solution
- The operation is straightforward file creation/modification

## Limitations

- Output is limited to stdout/stderr (no direct artifact download from run_shell)
- Files are created in the current working directory
- More verbose than execute_code_sandbox for simple operations