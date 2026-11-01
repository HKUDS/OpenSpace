---
name: excel-heredoc-workaround
description: Create Excel files with openpyxl by falling back to run_shell with inline Python heredoc when execute_code_sandbox fails
---

# Excel Heredoc Workaround

## Purpose

When creating Excel files with openpyxl, `execute_code_sandbox` may fail due to environment issues. This skill provides a robust workaround: fall back to `run_shell` with inline Python heredoc scripts to create properly formatted spreadsheets with styling.

## When to Use

- You need to create `.xlsx` files with openpyxl
- `execute_code_sandbox` fails with openpyxl-related errors
- You need styling, formatting, or complex spreadsheet features
- Direct shell execution with Python heredoc is available

## Step-by-Step Instructions

### Step 1: Attempt execute_code_sandbox First

Try creating the Excel file using `execute_code_sandbox`:

```python
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill

wb = Workbook()
ws = wb.active
ws.title = "Data"

# Add data and styling
ws['A1'] = "Header"
ws['A1'].font = Font(bold=True)

wb.save("output.xlsx")
print("ARTIFACT_PATH:output.xlsx")
```

### Step 2: If Sandbox Fails, Use run_shell with Heredoc

When `execute_code_sandbox` fails, switch to `run_shell` with a Python heredoc:

```bash
python3 << 'EOF'
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side

# Create workbook
wb = Workbook()
ws = wb.active
ws.title = "Schedule"

# Add headers with styling
headers = ["Task", "Date", "Status", "Priority"]
for col, header in enumerate(headers, 1):
    cell = ws.cell(row=1, column=col, value=header)
    cell.font = Font(bold=True, size=12)
    cell.fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    cell.font = Font(bold=True, color="FFFFFF")
    cell.alignment = Alignment(horizontal="center")

# Add data rows
data = [
    ["Cleanup Zone A", "2024-01-15", "Pending", "High"],
    ["Cleanup Zone B", "2024-01-16", "Complete", "Medium"],
]

for row_idx, row_data in enumerate(data, 2):
    for col_idx, value in enumerate(row_data, 1):
        cell = ws.cell(row=row_idx, column=col_idx, value=value)
        cell.alignment = Alignment(horizontal="left")

# Adjust column widths
for col in ws.columns:
    max_length = 0
    column = col[0].column_letter
    for cell in col:
        try:
            if len(str(cell.value)) > max_length:
                max_length = len(str(cell.value))
        except:
            pass
    ws.column_dimensions[column].width = max_length + 2

# Save the file
wb.save("Cleanup_Schedule.xlsx")
print("Created Cleanup_Schedule.xlsx successfully")
EOF
```

### Step 3: Verify File Creation

After running the heredoc script, verify the file was created:

```bash
ls -lh *.xlsx
```

### Step 4: Optional - Read Back to Confirm

Use `read_file` to confirm the Excel file is valid:

```
read_file with filetype="xlsx", file_path="Cleanup_Schedule.xlsx"
```

## Key Advantages

| Aspect | execute_code_sandbox | run_shell heredoc |
|--------|---------------------|-------------------|
| Reliability | May fail with openpyxl | More stable execution |
| Styling support | Limited | Full openpyxl support |
| File output | Via ARTIFACT_PATH | Direct file write |
| Debugging | Limited output | Full stdout/stderr |

## Common Styling Patterns

### Bold Headers with Colored Background

```python
from openpyxl.styles import Font, PatternFill

header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
header_font = Font(bold=True, color="FFFFFF", size=12)

cell = ws.cell(row=1, column=1, value="Header")
cell.fill = header_fill
cell.font = header_font
```

### Alternating Row Colors

```python
from openpyxl.styles import PatternFill

gray_fill = PatternFill(start_color="D9D9D9", end_color="D9D9D9", fill_type="solid")

for row in range(2, ws.max_row + 1):
    if row % 2 == 0:
        for col in range(1, ws.max_column + 1):
            ws.cell(row=row, column=col).fill = gray_fill
```

### Borders and Alignment

```python
from openpyxl.styles import Border, Side, Alignment

thin_border = Border(
    left=Side(style='thin'),
    right=Side(style='thin'),
    top=Side(style='thin'),
    bottom=Side(style='thin')
)

for row in ws.iter_rows():
    for cell in row:
        cell.border = thin_border
        cell.alignment = Alignment(horizontal="center", vertical="center")
```

## Troubleshooting

**Issue**: File not created after heredoc execution
- **Fix**: Check stdout for Python errors; ensure working directory is correct

**Issue**: openpyxl not found
- **Fix**: Install with `pip install openpyxl` before running heredoc

**Issue**: Styling not appearing
- **Fix**: Ensure you save the workbook after applying all styles

## Example Complete Workflow

```bash
# Create Excel with full styling
python3 << 'EOF'
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side

wb = Workbook()
ws = wb.active
ws.title = "Report"

# Header row
headers = ["ID", "Name", "Value", "Date"]
for col, h in enumerate(headers, 1):
    cell = ws.cell(row=1, column=col, value=h)
    cell.font = Font(bold=True, color="FFFFFF")
    cell.fill = PatternFill(start_color="2F5597", fill_type="solid")
    cell.alignment = Alignment(horizontal="center")

# Data
ws.append([1, "Item A", 100, "2024-01-01"])
ws.append([2, "Item B", 200, "2024-01-02"])

# Apply borders
thin = Side(style='thin')
border = Border(left=thin, right=thin, top=thin, bottom=thin)
for row in ws.iter_rows():
    for cell in row:
        cell.border = border

# Auto-width columns
for col in ws.columns:
    col_letter = col[0].column_letter
    max_len = max(len(str(cell.value)) if cell.value else 0 for cell in col)
    ws.column_dimensions[col_letter].width = min(max_len + 2, 50)

wb.save("Report.xlsx")
print("Success: Report.xlsx created")
EOF
```