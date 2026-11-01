---
name: openpyxl-merged-cell-safe-write
description: Safe pattern for writing values to merged cells in openpyxl spreadsheets
---

# OpenPyXL Merged Cell Safe Write Pattern

## Problem

When creating formatted spreadsheets with openpyxl, merging cells and then assigning values to them causes `MergedCell` errors. This is a common pitfall that can consume multiple failed iterations.

## Core Rule

**Always assign values to the top-left cell of a merge range BEFORE applying the merge**, or avoid assigning to merged cells entirely after the merge is applied.

## Correct Pattern

```python
from openpyxl import Workbook

wb = Workbook()
ws = wb.active

# CORRECT: Set value first, then merge
ws['A1'] = 'Quarterly Revenue Report'  # Assign to top-left cell first
ws.merge_cells('A1:D1')                # Then apply merge

# Also set formatting after merge
ws['A1'].font = Font(bold=True, size=16)
ws['A1'].alignment = Alignment(horizontal='center')
```

## Incorrect Pattern (Causes MergedCell Error)

```python
# WRONG: Merge first, then assign value
ws.merge_cells('A1:D1')
ws['A1'] = 'Quarterly Revenue Report'  # This raises MergedCell error!
```

## Step-by-Step Procedure

1. **Identify merge ranges** you need for headers, titles, or grouped data
2. **Write values to the top-left cell** of each range before merging
3. **Apply the merge** using `ws.merge_cells()`
4. **Apply formatting** (font, alignment, borders) to the top-left cell
5. **Verify** by checking that only the top-left cell holds the value

## Additional Guidelines

### For Multiple Merged Regions

```python
# Define all merges with their values
merges = [
    ('A1', 'D1', 'Quarterly Revenue Report'),
    ('A3', 'B3', 'Product'),
    ('C3', 'D3', 'Sales Data'),
]

for start_col, end_col, value in merges:
    cell_ref = f'{start_col}'  # Top-left cell
    ws[cell_ref] = value       # Set value first
    ws.merge_cells(f'{start_col}:{end_col}')  # Then merge
```

### When Modifying Existing Merged Cells

If you need to update a value in an already-merged range:

```python
# Get the top-left cell of the merged range
top_left = 'A1'  # Know your merge structure
ws[top_left] = 'New Value'  # Only modify the top-left cell
```

### Checking for Merged Cells

```python
# Check if a cell is within a merged range
def is_cell_merged(ws, cell_ref):
    for merged_range in ws.merged_cells.ranges:
        if cell_ref in merged_range:
            return True
    return False

# Or check before writing
if not is_cell_merged(ws, 'A1'):
    ws['A1'] = 'Value'
    ws.merge_cells('A1:D1')
```

## Common Use Cases

| Use Case | Pattern |
|----------|---------|
| Title headers | Write to A1, then merge A1:D1 |
| Section headers | Write to first cell, then merge across columns |
| Grouped labels | Write to top-left, merge the group range |
| Centered content | Write, merge, then apply center alignment |

## Quick Reference

```python
# ✅ DO: Value → Merge → Format
ws['A1'] = 'Title'
ws.merge_cells('A1:E1')
ws['A1'].alignment = Alignment(horizontal='center')

# ❌ DON'T: Merge → Value
ws.merge_cells('A1:E1')
ws['A1'] = 'Title'  # Error!
```

## Testing Your Spreadsheet

After creation, verify merged cells work correctly:

```python
# Save and reload to confirm no errors
wb.save('test.xlsx')

# Reload and check
wb2 = load_workbook('test.xlsx')
ws2 = wb2.active
print(ws2['A1'].value)  # Should show the title value
```