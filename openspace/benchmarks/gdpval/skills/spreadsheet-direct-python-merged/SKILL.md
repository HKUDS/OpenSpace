---
name: document-direct-python
description: Use direct Python execution for reliable document creation including spreadsheets, PDFs, and structured reports
---

# Direct Python Execution for Document Generation Tasks

## When to Use This Skill

Use direct `run_shell` with Python scripts for document operations when:

- Reading or writing complex Excel files with multiple sheets
- Generating PDF documents, checklists, or reports
- Applying formulas, formatting, or data transformations
- Working with `openpyxl`, `pandas`, `reportlab`, `FPDF`, or similar libraries
- The operation involves multiple steps that could exceed agent step limits
- You need precise control over error handling and debugging
- Complex scripts benefit from file-based execution for better reliability

## Why Direct Execution?

The `shell_agent` tool can:

- Hit maximum step limits on complex multi-step operations
- Produce unexplained errors on formatting operations
- Fail on intricate document reads/writes due to iterative parsing
- Fail to parse heredoc syntax correctly, causing 'unknown error' failures

Direct `run_shell` with Python is more reliable because it:

- Executes in a single step with no iteration limits
- Provides clearer, immediate error messages
- Handles complex operations without step constraints
- Gives full control over library imports and execution flow
- Writing scripts to `.py` files first avoids shell_agent parsing issues with heredocs

## How to Use


### Handling Web Research Failures

When web research tools (`search_web`, `read_webpage`) fail or return 'unknown error':

1. **Assess what you need**: Determine if the information is from well-documented regulatory frameworks, standards, or common knowledge that you can provide from internal knowledge.
2. **Proceed with available knowledge**: For pharmacy compliance, OSHA standards, GDPR requirements, and similar well-established frameworks, generate documents using your training knowledge rather than waiting for web access.
3. **Document assumptions**: Clearly note that materials were generated based on standard practices when source verification was unavailable.
4. **Use direct Python execution**: Continue with the recommended `run_shell` pattern to generate documents even without web-sourced content.

**Example contingency workflow for pharmacy compliance:**

```python
# When web research fails, proceed with established regulatory knowledge
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

checklist_items = [
    "Verify pharmacist license is current and displayed",
    "Maintain controlled substance inventory logs",
    "Ensure proper storage temperatures for medications",
    "Keep patient counseling records for controlled substances",
    "Display required pharmacy signage and notices"
]
# Generate PDF using these standard compliance items
```

This approach was successfully used in task 045aba2e-4093-42aa-ab7f-159cc538278c_phase2 where all web tools failed but pharmacy compliance PDFs were still created successfully.

### Recommended Pattern: Write Script to File First

For complex multi-line scripts, especially when using shell_agent as executor:

```bash
# Step 1: Write the Python script to a file
cat > generate_document.py << 'EOF'
import openpyxl
from openpyxl import Workbook

# Your document code here
wb = openpyxl.load_workbook('file.xlsx')
# ... operations ...
wb.save('output.xlsx')
print('Success')
EOF

# Step 2: Execute the script
python3 generate_document.py
```

### Alternative Pattern: Inline Heredoc (Simple Scripts Only)

For short, simple scripts when NOT using shell_agent as the executor:

```bash
python3 << 'EOF'
import pandas as pd
df = pd.read_excel('input.xlsx')
df.to_excel('output.xlsx', index=False)
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
    # Apply formatting or calculations
    for row in ws.iter_rows(min_row=2, max_col=5):
        # Process cells
        pass

wb.save('tour_data_processed.xlsx')
```

## PDF Generation Examples

### Example 3: Generate PDF Checklist with reportlab

**Write to file first, then execute:**

```python
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib.units import inch

def create_checklist(filename, items):
    c = canvas.Canvas(filename, pagesize=letter)
    width, height = letter
    
    # Title
    c.setFont("Helvetica-Bold", 20)
    c.drawString(1*inch, height - 1*inch, "Task Checklist")
    
    # Items
    c.setFont("Helvetica", 14)
    y_position = height - 1.5*inch
    for i, item in enumerate(items, 1):
        checkbox = "☐"  # Empty checkbox
        c.drawString(1*inch, y_position, f"{checkbox} {item}")
        y_position -= 0.3*inch
    
    c.save()
    print(f"Created {filename} with {len(items)} items")

# Usage
items = ["Review requirements", "Complete analysis", "Submit report", "Follow up"]
create_checklist("checklist.pdf", items)
```

### Example 4: Generate PDF Report with FPDF

```python
from fpdf import FPDF

class PDF(FPDF):
    def header(self):
        self.set_font('Arial', 'B', 15)
        self.cell(0, 10, 'Monthly Report', 0, 1, 'C')
        self.ln(10)
    
    def footer(self):
        self.set_y(-15)
        self.set_font('Arial', 'I', 8)
        self.cell(0, 10, f'Page {self.page_no()}', 0, 0, 'C')

def create_report(filename, data):
    pdf = PDF()
    pdf.add_page()
    pdf.set_font('Arial', '', 12)
    
    # Add content
    for section, content in data.items():
        pdf.set_font('Arial', 'B', 12)
        pdf.cell(0, 10, section, 0, 1)
        pdf.set_font('Arial', '', 12)
        pdf.multi_cell(0, 8, content)
        pdf.ln(5)
    
    pdf.output(filename)
    print(f"Report saved to {filename}")

# Usage
data = {
    "Executive Summary": "This report covers Q4 performance metrics.",
    "Key Findings": "Revenue increased by 15% compared to Q3.",
    "Recommendations": "Continue current strategy with minor adjustments."
}
create_report("report.pdf", data)
```

### Example 5: Combined Spreadsheet to PDF Workflow

```python
import pandas as pd
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle
from reportlab.lib import colors

# Step 1: Process spreadsheet data
df = pd.read_excel('sales_data.xlsx')
summary = df.groupby('Region')['Revenue'].sum().reset_index()

# Step 2: Generate PDF report
doc = SimpleDocTemplate("sales_report.pdf", pagesize=letter)
elements = []

# Create table from data
data = [['Region', 'Revenue']]
for _, row in summary.iterrows():
    data.append([row['Region'], f"${row['Revenue']:,.2f}"])

table = Table(data)
table.setStyle(TableStyle([
    ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
    ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
    ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
    ('GRID', (0, 0), (-1, -1), 1, colors.black),
]))

elements.append(table)
doc.build(elements)
print("PDF report generated successfully")
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
5. **Test with small data** before scaling to large documents
6. **Use pandas for data manipulation** and openpyxl for spreadsheet formatting
7. **Use reportlab for professional PDF layouts** and FPDF for simple PDFs
8. **Clean up temporary script files** after execution if they won't be reused

## When NOT to Use This Skill

- Simple single-cell reads/writes (use shell_agent or basic commands)
- Operations that require interactive user input
- Tasks where you need the agent to iteratively refine the approach
- Basic text file operations (use shell commands directly)

## Common Libraries

### Spreadsheets
| Library | Best For |
|---------|----------|
| `openpyxl` | Reading/writing .xlsx files, formatting, formulas |
| `pandas` | Data manipulation, analysis, merging datasets |
| `xlrd` | Reading older .xls files (read-only) |
| `xlsxwriter` | Creating new .xlsx files with advanced formatting |

### PDF Generation
| Library | Best For |
|---------|----------|
| `reportlab` | Professional PDF reports with complex layouts |
| `FPDF` | Simple PDF generation with basic formatting |
| `PyPDF2` / `pypdf` | Reading, merging, splitting existing PDFs |
| `pdfplumber` | Extracting text and tables from PDFs |

## Troubleshooting

**Issue**: Heredoc syntax fails with 'unknown error' when using shell_agent
- **Solution**: Write the Python script to a `.py` file first, then execute it with `python3 script.py`. This pattern is significantly more reliable than inline heredoc execution when shell_agent is the executor.

**Issue**: FileNotFoundError
- **Solution**: Verify the file path is absolute or relative to the working directory. Use `os.getcwd()` to check current directory if needed.

**Issue**: PermissionError
- **Solution**: Ensure the file is not open in another application. Close Excel/PDF readers before writing.

**Issue**: MemoryError on large files
- **Solution**: Process data in chunks using pandas `chunksize` parameter. For PDFs, generate in sections and merge.

**Issue**: Formatting not applying
- **Solution**: Ensure you're modifying cell styles before saving, and use `.copy()` for style objects in openpyxl.

**Issue**: PDF text rendering issues
- **Solution**: For non-ASCII characters, use appropriate fonts (e.g., `DejaVuSans` for Unicode support in reportlab).

**Issue**: Agent fails before first iteration (0 steps)
- **Solution**: Ensure the skill is properly loaded and the task description clearly indicates document generation needs. Complex initialization may require explicit Python script patterns.
