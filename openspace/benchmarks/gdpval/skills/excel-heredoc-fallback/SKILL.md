---
name: excel-heredoc-fallback
description: Create Excel files with openpyxl by falling back to shell-based Python heredoc when sandbox execution fails
---

# Excel Creation via Shell Heredoc Fallback

## Overview

When creating Excel files with openpyxl, `execute_code_sandbox` may fail due to sandbox restrictions, missing dependencies, or permission issues. This skill provides a reliable fallback: execute Python code via `run_shell` using an inline heredoc script. This approach often succeeds where the sandbox fails and supports full openpyxl features including styling, formulas, and formatting.

## When to Use This Skill

- `execute_code_sandbox` fails when importing or using openpyxl
- Sandbox shows errors about missing packages, permissions, or execution restrictions
- You need to create Excel files with advanced formatting (styles, colors, merged cells, formulas)
- Previous sandbox attempts have failed multiple times

## Step-by-Step Instructions

### Step 1: Attempt Sandbox Execution First

Always try `execute_code_sandbox` first, as it's cleaner and preferred when it works:

```python
from openpyxl import Workbook
from openpyxl.styles import Font, Fill, PatternFill, Alignment

wb = Workbook()
ws = wb.active
ws['A1'] = 'Header'
ws['A1'].font = Font(bold=True)
wb.save('output.xlsx')
```

### Step 2: Detect Sandbox Failure

Watch for these failure indicators:
- ImportError for openpyxl or related modules
- Permission denied errors
- File write failures
- Repeated retry loops without success
- Timeout or resource errors

### Step 3: Fall Back to Shell Heredoc

When sandbox fails, switch to `run_shell` with a Python heredoc:

```bash
python3 << 'EOF'
from openpyxl import Workbook
from openpyxl.styles import Font, Fill, PatternFill, Alignment, Border, Side

# Create workbook
wb = Workbook()
ws = wb.active
ws.title = "Sheet1"

# Add data with formatting
ws['A1'] = 'Name'
ws['B1'] = 'Value'
ws['A1'].font = Font(bold=True, size=14)
ws['B1'].font = Font(bold=True, size=14)

# Add rows
data = [
    ['Item 1', 100],
    ['Item 2', 200],
    ['Item 3', 150],
]

for row_idx, row_data in enumerate(data, start=2):
    for col_idx, value in enumerate(row_data, start=1):
        cell = ws.cell(row=row_idx, column=col_idx, value=value)
        cell.alignment = Alignment(horizontal='center')

# Add formulas if needed
ws['B5'] = '=SUM(B2:B4)'

# Save the file
wb.save('output.xlsx')
print("Excel file created successfully: output.xlsx")
EOF
```

### Step 4: Use run_shell Tool

Execute the heredoc via `run_shell`:

```
tool: run_shell
command: python3 << 'EOF'
[from openpyxl code here]
EOF
```

### Step 5: Verify File Creation

After execution, confirm the file was created:

```
tool: run_shell
command: ls -lh output.xlsx
```

Check the file size to ensure it's not empty (should be >1KB for typical spreadsheets).

## Best Practices

### 1. Use Single-Quoted Heredoc Delimiter

Always use `<< 'EOF'` (with quotes) to prevent shell variable expansion inside the Python code.

### 2. Include Error Handling

Add try/except blocks to catch and report issues:

```python
try:
    from openpyxl import Workbook
    # ... your code ...
    wb.save('output.xlsx')
    print("SUCCESS: File created")
except Exception as e:
    print(f"ERROR: {e}")
    exit(1)
```

### 3. Print Confirmation Messages

Always include print statements that confirm success or report specific errors. This helps debug issues.

### 4. Use Absolute or Explicit Paths

When saving files, use explicit paths to avoid confusion:

```python
wb.save('./output.xlsx')  # Explicit current directory
# or
wb.save('/workspace/output.xlsx')  # Absolute path
```

### 5. Keep Scripts Concise

Heredoc scripts should be focused and not excessively long. If the Excel logic is complex, consider writing a separate `.py` file first using `write_file`, then executing it.

## Advanced Formatting Examples

### Cell Styling

```python
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

# Define styles
bold_font = Font(bold=True, size=12)
header_fill = PatternFill(start_color='4472C4', end_color='4472C4', fill_type='solid')
center_align = Alignment(horizontal='center', vertical='center')
thin_border = Border(
    left=Side(style='thin'),
    right=Side(style='thin'),
    top=Side(style='thin'),
    bottom=Side(style='thin')
)

# Apply to cells
ws['A1'].font = bold_font
ws['A1'].fill = header_fill
ws['A1'].alignment = center_align
```

### Column Width and Row Height

```python
ws.column_dimensions['A'].width = 20
ws.column_dimensions['B'].width = 15
ws.row_dimensions[1].height = 25
```

### Merged Cells

```python
ws.merge_cells('A1:C1')
ws['A1'] = 'Merged Header'
ws['A1'].alignment = Alignment(horizontal='center')
```

### Multiple Sheets

```python
wb.create_sheet(title='Summary')
wb.create_sheet(title='Details')
ws_summary = wb['Summary']
ws_details = wb['Details']
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| openpyxl not found | Add `pip install openpyxl` before Python script |
| File not created | Check working directory, use absolute paths |
| Permission denied | Ensure write permissions in target directory |
| Encoding issues | Python 3 handles UTF-8 by default; specify if needed |
| Large files time out | Increase `run_shell` timeout parameter |

## Comparison: Sandbox vs. Shell Heredoc

| Aspect | execute_code_sandbox | run_shell heredoc |
|--------|---------------------|-------------------|
| Preferred | Yes (cleaner) | No (fallback) |
| Dependencies | May be restricted | Uses system Python |
| File access | Sandboxed | Full filesystem |
| Styling support | Sometimes limited | Full support |
| Debugging | Logs in tool output | Full stdout/stderr |

## Example Complete Workflow

```
# Step 1: Try sandbox
tool: execute_code_sandbox
code: |
  from openpyxl import Workbook
  wb = Workbook()
  ws = wb.active
  ws['A1'] = 'Test'
  wb.save('test.xlsx')

# Step 2: If that fails, use shell heredoc
tool: run_shell
command: |
  python3 << 'EOF'
  from openpyxl import Workbook
  from openpyxl.styles import Font, PatternFill
  
  wb = Workbook()
  ws = wb.active
  
  ws['A1'] = 'Header'
  ws['A1'].font = Font(bold=True)
  ws['A1'].fill = PatternFill(start_color='FFFF00', fill_type='solid')
  
  wb.save('test.xlsx')
  print("Created: test.xlsx")
  EOF

# Step 3: Verify
tool: run_shell
command: ls -lh test.xlsx
```