---
name: reliable-excel-workbook-creation
description: Use run_shell with cd and heredoc syntax for complex Excel operations when read_file fails on XLSX or execute_code_sandbox fails on multi-step workbook creation
---

# Reliable Excel Workbook Creation via Shell

## When to Use This Pattern

Use this technique when you need to:
- Create complex Excel workbooks with multiple sheets
- Perform multi-step workbook operations (create, populate, format, save)
- Work with XLSX files that `read_file` cannot handle properly
- Avoid failures from `execute_code_sandbox` on persistent file operations

**Avoid:** `read_file` for XLSX files, `execute_code_sandbox` for multi-step workbook creation

## Core Pattern

```bash
cd /workspace && python3 << 'EOF'
from openpyxl import Workbook
# Your Excel operations here
wb = Workbook()
# ... create sheets, add data, save
wb.save('/workspace/filename.xlsx')
EOF
```

## Step-by-Step Instructions

### Step 1: Change to Workspace Directory
Always start with `cd /workspace &&` to ensure file paths resolve correctly.

### Step 2: Use Heredoc with Single-Quoted EOF
Use `<< 'EOF'` (single quotes) to prevent shell variable expansion within the Python code.

### Step 3: Write Complete Python Script
Include all imports, workbook creation, data population, and save operations in one script.

### Step 4: Use Absolute Paths for Save Operations
Always save with full path: `/workspace/your_filename.xlsx`

## Complete Example

```bash
cd /workspace && python3 << 'EOF'
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill

# Create workbook
wb = Workbook()

# Create multiple sheets
sheets = ['Summary', 'Data', 'Analysis', 'Settings']
for sheet_name in sheets:
    wb.create_sheet(title=sheet_name)

# Remove default sheet if needed
if 'Sheet' in wb.sheetnames:
    del wb['Sheet']

# Populate data
ws = wb['Data']
ws['A1'] = 'Item'
ws['B1'] = 'Value'
ws['A2'] = 'Revenue'
ws['B2'] = 100000

# Apply formatting
header_font = Font(bold=True)
for cell in ws['1:1']:
    cell.font = header_font

# Save workbook
wb.save('/workspace/report.xlsx')
print('Workbook created successfully: /workspace/report.xlsx')
EOF
```

## Why This Works

| Approach | Problem | Solution |
|----------|---------|----------|
| `read_file` | Cannot parse XLSX binary format | Don't use for Excel files |
| `execute_code_sandbox` | File persistence issues, multi-step failures | Use `run_shell` instead |
| `run_shell` with heredoc | ✓ Reliable, full control, persistent files | **Use this pattern** |

## Common Operations

### Create Multi-Sheet Workbook
```bash
cd /workspace && python3 << 'EOF'
from openpyxl import Workbook
wb = Workbook()
sheet_names = ['Sheet1', 'Sheet2', 'Sheet3']
for name in sheet_names:
    wb.create_sheet(title=name)
wb.save('/workspace/multi_sheet.xlsx')
EOF
```

### Read and Modify Existing Workbook
```bash
cd /workspace && python3 << 'EOF'
from openpyxl import load_workbook
wb = load_workbook('/workspace/existing.xlsx')
ws = wb.active
ws['A1'] = 'Updated Value'
wb.save('/workspace/existing.xlsx')
EOF
```

### Add Formulas
```bash
cd /workspace && python3 << 'EOF'
from openpyxl import Workbook
wb = Workbook()
ws = wb.active
ws['A1'] = 100
ws['A2'] = 200
ws['A3'] = '=SUM(A1:A2)'
wb.save('/workspace/with_formulas.xlsx')
EOF
```

## Troubleshooting

- **File not found**: Ensure you use `/workspace/` prefix in all paths
- **Permission errors**: Verify workspace directory is writable
- **Module not found**: openpyxl is pre-installed in the environment
- **Heredoc issues**: Use single-quoted `'EOF'` to prevent shell interpretation

## Best Practices

1. **Always use absolute paths** (`/workspace/filename.xlsx`)
2. **Single-quoted heredoc** (`<< 'EOF'`) prevents variable expansion
3. **Complete script in one heredoc** - avoid splitting operations
4. **Print confirmation** after save to verify success
5. **Handle exceptions** for production scripts