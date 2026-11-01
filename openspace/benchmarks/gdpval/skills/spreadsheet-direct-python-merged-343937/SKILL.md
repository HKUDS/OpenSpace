---
name: document-python-direct-exec
description: Use direct Python execution for reliable spreadsheet and document/PDF generation operations
---

# Direct Python Execution for Spreadsheet and Document Tasks

## When to Use This Skill

Use direct `run_shell` with Python scripts for structured document operations when:

- **Spreadsheets**: Reading or writing complex Excel files with multiple sheets, applying formulas, formatting, or data transformations
- **PDFs**: Generating PDF checklists, reports, invoices, or forms with precise layout control
- **Documents**: Creating Word documents, HTML reports, or other structured output formats
- **Complex Operations**: The task involves multiple steps that could exceed agent step limits
- **Precision Needed**: You need precise control over error handling, debugging, and library imports

## Why Direct Execution?

The `shell_agent` tool can:

- Hit maximum step limits on complex multi-step operations
- Produce unexplained errors on formatting operations
- Fail on intricate reads/writes due to iterative parsing
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
# Your document/spreadsheet code here
EOF

# Step 2: Execute the script
python3 process_document.py
```

### Alternative Pattern: Inline Heredoc (Simple Scripts Only)

For short, simple scripts when NOT using shell_agent as the executor:

```bash
python3 << 'EOF'
# Your code here
EOF
```

## Spreadsheet Examples

### Example 1: Read and Transform Excel Data

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
    # Apply formatting or calculations
    for row in ws.iter_rows(min_row=2, max_col=5):
        # Process cells
        pass

wb.save('tour_data_processed.xlsx')
```

### Example 3: Complex Formatting Operations

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

## PDF Generation Examples

### Example 4: Generate PDF Checklist with ReportLab

```python
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib.units import inch

def create_checklist(pdf_path, items):
    c = canvas.Canvas(pdf_path, pagesize=letter)
    width, height = letter
    
    # Title
    c.setFont("Helvetica-Bold", 16)
    c.drawString(1*inch, height - 1*inch, "Safety Checklist")
    
    # Checklist items
    c.setFont("Helvetica", 12)
    y_position = height - 1.5*inch
    
    for i, item in enumerate(items, 1):
        checkbox_x = 1*inch
        text_x = 1.3*inch
        c.drawString(checkbox_x, y_position, "☐")  # Empty checkbox
        c.drawString(text_x, y_position, f"{i}. {item}")
        y_position -= 0.3*inch
        
        # New page if needed
        if y_position < 1*inch:
            c.showPage()
            y_position = height - 1*inch
    
    c.save()
    print(f"Created checklist: {pdf_path}")

# Usage
items = [
    "Verify equipment is powered off",
    "Check safety gear is available",
    "Inspect work area for hazards",
    "Confirm emergency contacts are posted"
]
create_checklist('safety_checklist.pdf', items)
```

### Example 5: Generate PDF Report with Tables

```python
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch

def create_report(pdf_path, title, data, column_headers):
    doc = SimpleDocTemplate(pdf_path, pagesize=letter,
                           rightMargin=0.75*inch, leftMargin=0.75*inch,
                           topMargin=0.75*inch, bottomMargin=0.75*inch)
    
    elements = []
    styles = getSampleStyleSheet()
    
    # Title
    title_style = ParagraphStyle('CustomTitle', parent=styles['Heading1'],
                                 fontSize=18, spaceAfter=30, alignment=1)
    elements.append(Paragraph(title, title_style))
    elements.append(Spacer(1, 0.25*inch))
    
    # Table
    table_data = [column_headers] + data
    table = Table(table_data, colWidths=[2*inch, 1.5*inch, 1.5*inch])
    
    # Table styling
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#4472C4')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 12),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#D6DCE4')]),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
    ]))
    
    elements.append(table)
    doc.build(elements)
    print(f"Created report: {pdf_path}")

# Usage
headers = ['Item', 'Quantity', 'Status']
data = [
    ['Widget A', '150', 'Complete'],
    ['Widget B', '200', 'In Progress'],
    ['Widget C', '75', 'Pending']
]
create_report('status_report.pdf', 'Weekly Status Report', data, headers)
```

### Example 6: PDF with Images and Text (FPDF Alternative)

```python
from fpdf import FPDF

class PDF(FPDF):
    def header(self):
        self.set_font('Arial', 'B', 15)
        self.cell(0, 10, 'Project Documentation', 0, 1, 'C')
        self.ln(10)
    
    def footer(self):
        self.set_y(-15)
        self.set_font('Arial', 'I', 8)
        self.cell(0, 10, f'Page {self.page_no()}', 0, 0, 'C')

def create_document(pdf_path, title, sections):
    pdf = PDF()
    pdf.add_page()
    pdf.set_font('Arial', 'B', 16)
    pdf.cell(0, 10, title, 0, 1, 'L')
    pdf.ln(5)
    
    pdf.set_font('Arial', '', 12)
    for section in sections:
        pdf.set_font('Arial', 'B', 14)
        pdf.cell(0, 10, section['title'], 0, 1)
        pdf.set_font('Arial', '', 12)
        pdf.multi_cell(0, 6, section['content'])
        pdf.ln(5)
    
    pdf.output(pdf_path)
    print(f"Created document: {pdf_path}")

# Usage
sections = [
    {'title': 'Overview', 'content': 'This document provides...'},
    {'title': 'Requirements', 'content': 'The following requirements...'},
    {'title': 'Timeline', 'content': 'Project phases are...'}
]
create_document('project_doc.pdf', 'Project Alpha', sections)
```

## Error Handling Pattern

```python
import sys
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

try:
    c = canvas.Canvas('output.pdf', pagesize=letter)
    
    # Your document operations here
    c.drawString(100, 750, "Document content")
    
    c.save()
    print("Success: PDF created")
    
except Exception as e:
    print(f"Error: {str(e)}", file=sys.stderr)
    sys.exit(1)
```

## Best Practices

1. **Prefer file-based execution** for complex scripts: write to `.py` file first, then execute via `run_shell`
2. **Import only needed libraries** to reduce execution time and avoid dependency conflicts
3. **Print clear success/error messages** for debugging
4. **Save intermediate results** for complex multi-step transformations
5. **Test with small data** before scaling to large documents or spreadsheets
6. **Use appropriate libraries for the task**: pandas/openpyxl for spreadsheets, reportlab/fpdf for PDFs
7. **Clean up temporary script files** after execution if they won't be reused
8. **Use absolute paths or verify working directory** to avoid file not found errors
9. **For PDFs**: Build content in memory first, then write to file atomically

## When NOT to Use This Skill

- Simple single-cell reads/writes (use shell_agent or basic commands)
- Operations that require interactive user input
- Tasks where you need the agent to iteratively refine the approach
- When the target format has a simpler CLI tool available (e.g., `pandoc` for document conversion)

## Common Libraries

| Library | Best For |
|---------|----------|
| `openpyxl` | Reading/writing .xlsx files, formatting, formulas |
| `pandas` | Data manipulation, analysis, merging datasets |
| `xlrd` | Reading older .xls files (read-only) |
| `xlsxwriter` | Creating new .xlsx files with advanced formatting |
| `reportlab` | Professional PDF generation with precise layout control |
| `fpdf` | Simple PDF creation, easier learning curve |
| `python-docx` | Creating and editing Word documents |
| `weasyprint` | HTML/CSS to PDF conversion |

## Troubleshooting

**Issue**: Heredoc syntax fails with 'unknown error' when using shell_agent
- **Solution**: Write the Python script to a `.py` file first, then execute it with `python3 script.py`. This pattern is significantly more reliable than inline heredoc execution when shell_agent is the executor.

**Issue**: FileNotFoundError
- **Solution**: Verify the file path is absolute or relative to the working directory. Use `os.getcwd()` to confirm current directory if needed.

**Issue**: PermissionError
- **Solution**: Ensure the file is not open in another application. On Linux/Mac, check file permissions with `ls -la`.

**Issue**: ModuleNotFoundError
- **Solution**: Install required libraries first: `pip install reportlab openpyxl pandas`. Some environments may need `pip3` instead.

**Issue**: MemoryError on large files
- **Solution**: Process data in chunks using pandas `chunksize` parameter. For PDFs, create multi-page documents instead of single massive pages.

**Issue**: Formatting not applying (spreadsheets)
- **Solution**: Ensure you're modifying cell styles before saving, and use `.copy()` for style objects to avoid reference issues.

**Issue**: PDF text rendering incorrectly
- **Solution**: Check font encoding. For special characters, use Unicode-compatible fonts or escape special characters. ReportLab supports UTF-8 with proper font configuration.

**Issue**: PDF layout breaks across pages
- **Solution**: Use ReportLab's flowable elements (Paragraph, Spacer) which handle page breaks automatically, or manually check y-position and call `showPage()` when needed.

## Quick Reference: PDF vs Spreadsheet Choice

| Need | Recommended Library |
|------|---------------------|
| Data analysis, calculations | `pandas` + Excel output |
| Complex cell formatting | `openpyxl` |
| Professional reports with tables | `reportlab` (PDF) |
| Simple checklists, forms | `fpdf` or `reportlab` |
| Word document output | `python-docx` |
| HTML to PDF | `weasyprint` |
| Both data + formatted output | Generate data with pandas, format with reportlab |
