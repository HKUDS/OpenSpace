---
name: document-python-direct
description: Use direct Python execution for reliable spreadsheet and document/PDF generation operations
---

# Direct Python Execution for Document and Spreadsheet Tasks

## When to Use This Skill

Use direct `run_shell` with Python scripts for document operations when:

- Reading or writing complex Excel files with multiple sheets
- Generating PDF reports, checklists, or forms
- Applying formulas, formatting, or data transformations
- Working with `openpyxl`, `pandas`, `reportlab`, `fpdf2`, or similar libraries
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
cat > process_document.py << 'EOF'
import openpyxl
from openpyxl import Workbook

# Your spreadsheet code here
wb = openpyxl.load_workbook('file.xlsx')
# ... operations ...
wb.save('output.xlsx')
print('Success')
EOF

# Step 2: Execute the script
python3 process_document.py
```

### Alternative Pattern: Inline Heredoc (Simple Scripts Only)

For short, simple scripts when NOT using shell_agent as the executor:

```bash
python3 << 'EOF'
import openpyxl
# Simple operation
wb = openpyxl.load_workbook('file.xlsx')
wb.save('output.xlsx')
print('Done')
EOF
```

## Spreadsheet Examples

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

```python
from openpyxl import load_workbook

wb = load_workbook('tour_data.xlsx')

# Iterate through sheets
for sheet_name in wb.sheetnames:
    ws = wb[sheet_name]
    for row in ws.iter_rows(min_row=2, max_col=5):
        pass

wb.save('tour_data_processed.xlsx')
```

### Example 3: Complex Formatting Operations

```python
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment

wb = load_workbook('report.xlsx')
ws = wb.active

header_fill = PatternFill(start_color='4472C4', fill_type='solid')
header_font = Font(bold=True, color='FFFFFF')

for cell in ws[1]:
    cell.fill = header_fill
    cell.font = header_font
    cell.alignment = Alignment(horizontal='center')

wb.save('report_formatted.xlsx')
```

## PDF Generation Examples

### Example 4: Basic PDF Report with reportlab

```python
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

# Create PDF document
doc = SimpleDocTemplate('report.pdf', pagesize=letter)
styles = getSampleStyleSheet()
story = []

# Add content
story.append(Paragraph('Monthly Report', styles['Heading1']))
story.append(Spacer(1, 12))
story.append(Paragraph('Generated successfully.', styles['Normal']))

# Build PDF
doc.build(story)
print('PDF created successfully')
```

### Example 5: PDF Checklist with fpdf2

```python
from fpdf import FPDF

# Create PDF instance
pdf = FPDF()
pdf.add_page()
pdf.set_font('Arial', 'B', 16)

# Title
pdf.cell(0, 10, 'Safety Checklist', ln=True, align='C')
pdf.ln(10)

# Checklist items
pdf.set_font('Arial', '', 12)
items = ['Check equipment', 'Verify connections', 'Test system', 'Document results']
for i, item in enumerate(items, 1):
    pdf.cell(5, 10, f'[ ] {item}', ln=True)

# Save
pdf.output('checklist.pdf')
print('Checklist PDF generated')
```

### Example 6: Combined Spreadsheet + PDF Workflow

```python
import pandas as pd
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

# Step 1: Process spreadsheet data
df = pd.read_excel('data.xlsx')
summary = df.describe()

# Step 2: Generate PDF report
c = canvas.Canvas('summary_report.pdf', pagesize=letter)
c.drawString(100, 750, 'Data Summary Report')
c.drawString(100, 730, f'Total Records: {len(df)}')
c.drawString(100, 710, f'Mean Value: {df.iloc[:, 0].mean():.2f}')
c.save()

print('Spreadsheet processed and PDF report generated')
```

## Error Handling Pattern

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
5. **Test with small data** before scaling to large spreadsheets or documents
6. **Use pandas for data manipulation** and openpyxl for formatting when both are needed
7. **Clean up temporary script files** after execution if they won't be reused
8. **Use PDF libraries appropriate to your needs**: reportlab for complex layouts, fpdf2 for simple documents

## When NOT to Use This Skill

- Simple single-cell reads/writes (use shell_agent or basic commands)
- Operations that require interactive user input
- Tasks where you need the agent to iteratively refine the approach
- Very simple text files (use basic shell commands instead)

## Common Libraries

| Library | Best For |
|---------|----------|
| `openpyxl` | Reading/writing .xlsx files, formatting, formulas |
| `pandas` | Data manipulation, analysis, merging datasets |
| `xlrd` | Reading older .xls files (read-only) |
| `xlsxwriter` | Creating new .xlsx files with advanced formatting |
| `reportlab` | Professional PDF reports with complex layouts |
| `fpdf2` | Simple PDF generation, checklists, basic documents |
| `python-docx` | Creating and editing Word documents |

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

**Issue**: PDF text encoding errors
- **Solution**: Use Unicode-compatible fonts and ensure text is properly encoded (UTF-8)

**Issue**: PDF library not found
- **Solution**: Install required library with `pip install reportlab` or `pip install fpdf2`

**Issue**: Agent fails before tool execution (skill selector returns empty)
- **Solution**: Ensure the skill is properly registered and the task matches the skill's description. For document generation tasks, explicitly mention "PDF" or "spreadsheet" in the task description to trigger skill selection.
