---
name: pdf-checklist-generator
description: Generate structured PDF checklists and reports with tables, sections, and scoring criteria using Python libraries
---

# PDF Checklist/Report Generator

This skill provides a reusable pattern for creating professional PDF documents (checklists, reports, scorecards) using Python libraries within `execute_code_sandbox`.

## When to Use This Skill

- Need to generate structured PDF documents programmatically
- Creating checklists with scoring criteria
- Building reports with tables, sections, and formatted content
- Producing downloadable artifacts for workflow tasks

## Library Selection

Choose the appropriate library based on your needs:

| Library | Best For | Installation |
|---------|----------|--------------|
| **reportlab** | Complex layouts, tables, precise control | `pip install reportlab` |
| **fpdf2** | Simple documents, easy API | `pip install fpdf2` |
| **matplotlib** | Charts/graphs in PDFs | `pip install matplotlib` |

## Step-by-Step Procedure

### Step 1: Plan Document Structure

Define the sections, tables, and scoring criteria before coding:
- Document title and metadata
- Section headers
- Table columns and rows
- Scoring rubric (if applicable)

### Step 2: Write Python Code in execute_code_sandbox

Use `execute_code_sandbox` with the appropriate library. Here are templates:

#### Template A: Using reportlab (recommended for tables)

```python
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch

# Create PDF
doc = SimpleDocTemplate("checklist.pdf", pagesize=letter)
elements = []
styles = getSampleStyleSheet()

# Title
title_style = ParagraphStyle('CustomTitle', parent=styles['Heading1'], alignment=1)
elements.append(Paragraph("Compliance Checklist", title_style))
elements.append(Spacer(1, 0.25*inch))

# Create table data
data = [
    ['Criteria', 'Status', 'Score', 'Notes'],
    ['Security Review', 'Pass', '10', 'All checks passed'],
    ['Code Quality', 'Pass', '8', 'Minor improvements'],
    ['Documentation', 'Pending', '5', 'Needs updates'],
]

# Create and style table
table = Table(data, colWidths=[3*inch, 1*inch, 1*inch, 2*inch])
table.setStyle(TableStyle([
    ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
    ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
    ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
    ('GRID', (0, 0), (-1, -1), 1, colors.black),
    ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
]))
elements.append(table)

# Build PDF
doc.build(elements)
print("PDF created: checklist.pdf")
print(f"ARTIFACT_PATH:/workspace/checklist.pdf")
```

#### Template B: Using fpdf2 (simpler API)

```python
from fpdf import FPDF

pdf = FPDF()
pdf.add_page()
pdf.set_font("Arial", "B", 16)
pdf.cell(0, 10, "Project Report", ln=True, align='C')

pdf.set_font("Arial", size=12)
pdf.ln(10)

# Add sections
sections = [
    ("Executive Summary", "Project completed successfully."),
    ("Key Findings", "All milestones achieved on time."),
    ("Recommendations", "Continue current practices."),
]

for title, content in sections:
    pdf.set_font("Arial", "B", 14)
    pdf.cell(0, 10, title, ln=True)
    pdf.set_font("Arial", size=12)
    pdf.multi_cell(0, 8, content)
    pdf.ln(5)

pdf.output("report.pdf")
print("PDF created: report.pdf")
print(f"ARTIFACT_PATH:/workspace/report.pdf")
```

#### Template C: Checklist with Scoring

```python
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors

doc = SimpleDocTemplate("scorecard.pdf", pagesize=letter)
elements = []
styles = getSampleStyleSheet()

# Title
elements.append(Paragraph("Evaluation Scorecard", styles['Heading1']))
elements.append(Spacer(1, 0.25*inch))

# Scoring criteria table
criteria = [
    ['Category', 'Weight', 'Score (0-10)', 'Weighted'],
    ['Functionality', '30%', '9', '2.7'],
    ['Performance', '25%', '8', '2.0'],
    ['Security', '25%', '10', '2.5'],
    ['Documentation', '20%', '7', '1.4'],
    ['TOTAL', '100%', '-', '8.6/10'],
]

table = Table(criteria, colWidths=[2.5*inch, 1*inch, 1.5*inch, 1.5*inch])
table.setStyle(TableStyle([
    ('BACKGROUND', (0, 0), (-1, 0), colors.darkblue),
    ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
    ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
    ('GRID', (0, 0), (-1, -1), 1, colors.black),
    ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
    ('BACKGROUND', (0, -1), (-1, -1), colors.lightgrey),
]))
elements.append(table)

doc.build(elements)
print("ARTIFACT_PATH:/workspace/scorecard.pdf")
```

### Step 3: Include ARTIFACT_PATH in Output

Always include the artifact path in your print output so the system can locate the generated file:

```python
print(f"ARTIFACT_PATH:/workspace/your_filename.pdf")
```

### Step 4: Verify Output

After generation, verify the PDF was created successfully:

**Option A: Use read_file**
```
read_file with filetype="pdf" and file_path="/workspace/checklist.pdf"
```

**Option B: Use shell inspection**
```
run_shell with command="ls -la /workspace/*.pdf"
```

## Common Patterns

### Pattern 1: Multi-Section Report
```python
sections = [
    ("Introduction", "Overview text..."),
    ("Methodology", "Process description..."),
    ("Results", "Findings summary..."),
    ("Conclusion", "Final recommendations..."),
]

for title, content in sections:
    elements.append(Paragraph(title, styles['Heading2']))
    elements.append(Paragraph(content, styles['Normal']))
    elements.append(Spacer(1, 0.2*inch))
```

### Pattern 2: Status Indicator Table
```python
status_colors = {
    'Pass': colors.green,
    'Fail': colors.red,
    'Pending': colors.orange,
    'N/A': colors.grey,
}

# Apply conditional coloring in table style
for row_idx, row_data in enumerate(data[1:], 1):
    status = row_data[1]
    if status in status_colors:
        table.setStyle(TableStyle([
            ('BACKGROUND', (1, row_idx), (1, row_idx), status_colors[status]),
        ]))
```

### Pattern 3: Page Breaks for Long Documents
```python
from reportlab.platypus import PageBreak

elements.append(PageBreak())  # Insert page break
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Library not found | Add `pip install <library>` at start of code |
| Font errors | Use built-in fonts: Arial, Helvetica, Times-Roman |
| Table overflow | Adjust `colWidths` or split into multiple tables |
| PDF not created | Check ARTIFACT_PATH is correct and file is written |

## Best Practices

1. **Always verify**: Check the PDF exists and is readable after generation
2. **Use ARTIFACT_PATH**: Include it in output for system discovery
3. **Keep it readable**: Use adequate spacing (`Spacer`) between elements
4. **Test incrementally**: Build complex PDFs section by section
5. **Handle errors**: Wrap file operations in try/except blocks

## Example Complete Workflow

```python
# 1. Install library
!pip install reportlab -q

# 2. Generate PDF
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors

doc = SimpleDocTemplate("/workspace/checklist.pdf", pagesize=letter)
elements = []
styles = getSampleStyleSheet()

elements.append(Paragraph("Task Completion Checklist", styles['Heading1']))
elements.append(Spacer(1, 0.25*inch))

data = [['Task', 'Complete', 'Notes'],
        ['Setup environment', 'Yes', ''],
        ['Run tests', 'Yes', 'All passed'],
        ['Documentation', 'No', 'In progress']]

table = Table(data)
table.setStyle(TableStyle([('GRID', (0,0), (-1,-1), 1, colors.black)]))
elements.append(table)

doc.build(elements)
print("ARTIFACT_PATH:/workspace/checklist.pdf")

# 3. Verify
import os
print(f"File exists: {os.path.exists('/workspace/checklist.pdf')}")
print(f"File size: {os.path.getsize('/workspace/checklist.pdf')} bytes")
```