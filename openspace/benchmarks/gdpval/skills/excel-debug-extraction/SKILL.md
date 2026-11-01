---
name: excel-debug-extraction
description: Iteratively debug Excel structure with exploratory scripts before writing extraction logic
---

# Excel Debug-First Extraction Workflow

When working with poorly-structured, complex, or unfamiliar Excel files, use this iterative debugging approach to map the data layout before writing your final extraction logic.

## When to Use This Skill

- Excel files with inconsistent formatting or merged cells
- Files received from external sources with unknown structure
- Complex workbooks with multiple sheets and interdependencies
- When initial parsing attempts fail or produce unexpected results

## Workflow Steps

### Step 1: Initial Structure Reconnaissance

Before writing extraction logic, create a debug script to explore the file structure:

```python
# debug_structure.py
from openpyxl import load_workbook

wb = load_workbook('file.xlsx')
print(f"Sheets: {wb.sheetnames}")

for sheet_name in wb.sheetnames:
    ws = wb[sheet_name]
    print(f"\n=== Sheet: {sheet_name} ===")
    print(f"Dimensions: {ws.dimensions}")
    
    # Print first 10 rows to understand header structure
    for row in ws.iter_rows(min_row=1, max_row=10, values_only=True):
        print([str(cell)[:50] for cell in row])
```

### Step 2: Map Column Positions

Identify where key data fields are located:

```python
# debug_columns.py
from openpyxl import load_workbook

wb = load_workbook('file.xlsx')
ws = wb['Sheet1']

# Examine header row to find column indices
header_row = 1
column_map = {}

for col in ws.iter_cols(min_row=header_row, max_row=header_row):
    for cell in col:
        if cell.value:
            column_map[str(cell.value)] = cell.column_letter

print("Column mapping:", column_map)

# Sample data rows to verify structure
for row_num in range(2, min(6, ws.max_row + 1)):
    row_data = [ws.cell(row=row_num, column=col).value 
                for col in range(1, ws.max_column + 1)]
    print(f"Row {row_num}: {row_data}")
```

### Step 3: Identify Row Patterns

Understand how data rows are structured (e.g., summary rows, detail rows, blank separators):

```python
# debug_rows.py
from openpyxl import load_workbook

wb = load_workbook('file.xlsx')
ws = wb['Sheet1']

row_types = []
for row_num in range(1, min(30, ws.max_row + 1)):
    row_values = [ws.cell(row=row_num, column=col).value 
                  for col in range(1, ws.max_column + 1)]
    
    non_empty = sum(1 for v in row_values if v is not None and str(v).strip())
    
    # Classify row type
    if non_empty == 0:
        row_type = "blank"
    elif non_empty == 1:
        row_type = "summary/label"
    elif non_empty == ws.max_column:
        row_type = "full_data"
    else:
        row_type = "partial"
    
    row_types.append((row_num, row_type, row_values[:5]))

for rt in row_types:
    print(f"Row {rt[0]} ({rt[1]}): {rt[2]}")
```

### Step 4: Document Findings

Before writing extraction logic, summarize:
- Sheet names and their purposes
- Header row location and column mappings
- Data row patterns (which rows contain actual data vs. headers/summaries)
- Any special formatting (merged cells, blank separators, grouping rows)

### Step 5: Write Extraction Logic

Incorporate findings into your final processing script:

```python
# extract_data.py
from openpyxl import load_workbook
import pandas as pd

wb = load_workbook('file.xlsx')
ws = wb['Sheet1']

# Use column mappings from debug phase
STORE_COL = 'B'  # Column 2
WEEK1_COL = 'D'  # Column 4
WEEK2_COL = 'E'  # Column 5

# Skip header rows and summary rows based on debug findings
data_rows = []
for row_num in range(5, ws.max_row + 1):  # Start after header based on debug
    # Skip summary/blank rows
    if ws.cell(row=row_num, column=2).value is None:
        continue
    if 'TOTAL' in str(ws.cell(row=row_num, column=2).value).upper():
        continue
    
    row_data = {
        'store': ws.cell(row=row_num, column=2).value,
        'week1': ws.cell(row=row_num, column=4).value,
        'week2': ws.cell(row=row_num, column=5).value,
    }
    data_rows.append(row_data)

df = pd.DataFrame(data_rows)
print(df.head())
```

## Best Practices

1. **Always start with exploration** - Never assume Excel structure matches expectations
2. **Save debug scripts** - Keep them in your project for future reference and debugging
3. **Print generously** - Use verbose output during exploration to catch edge cases
4. **Verify row-by-row** - Don't assume all data rows follow the same pattern
5. **Handle merged cells** - Check for merged cells that span multiple rows/columns

## Common Pitfalls to Avoid

- Assuming header is always row 1
- Assuming all rows between first and last contain data
- Not checking for hidden sheets or protected ranges
- Ignoring cell formatting that indicates row type (bold, indentation)
- Not handling None values or empty strings consistently

## File Naming Convention

Use descriptive names for debug scripts:
- `debug_structure.py` - Overall file/sheet structure
- `debug_columns.py` - Column positions and headers
- `debug_rows.py` - Row patterns and data boundaries
- `debug_values.py` - Value patterns and edge cases

Keep debug scripts alongside your extraction script for maintainability.