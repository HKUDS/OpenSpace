---
name: direct-reportlab-pdf-generation
description: Generate complex multi-page PDFs by running reportlab Python code directly via run_shell when shell_agent fails on document creation
---

# Direct ReportLab PDF Generation via run_shell

## When to Use This Skill

Use this pattern when:
- You need to create a complex, multi-page PDF document with structured content
- Delegating PDF generation to `shell_agent` fails or produces unreliable results
- You need fine-grained control over PDF layout, styling, and pagination

## Core Technique

Instead of asking `shell_agent` to handle PDF creation, write inline Python code using the `reportlab` library and execute it directly via `run_shell`. This gives you deterministic control over the document structure.

## Step-by-Step Instructions

### Step 1: Prepare Your PDF Content

Organize your content into logical sections that will become pages or page groups:
- Title page
- Table of contents (optional)
- Main content sections
- Appendices or references

### Step 2: Write the ReportLab Python Script

Create a Python script that uses these key reportlab components:

```python
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, Image
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY

def create_pdf(filename, content_data):
    doc = SimpleDocTemplate(filename, pagesize=letter,
                           rightMargin=72, leftMargin=72,
                           topMargin=72, bottomMargin=72)
    
    story = []
    styles = getSampleStyleSheet()
    
    # Custom styles
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=24,
        textColor=colors.HexColor('#1a1a1a'),
        spaceAfter=30,
        alignment=TA_CENTER
    )
    
    heading_style = ParagraphStyle(
        'CustomHeading',
        parent=styles['Heading2'],
        fontSize=16,
        textColor=colors.HexColor('#2c3e50'),
        spaceBefore=20,
        spaceAfter=12
    )
    
    body_style = ParagraphStyle(
        'CustomBody',
        parent=styles['Normal'],
        fontSize=11,
        leading=16,
        alignment=TA_JUSTIFY
    )
    
    # Build document content
    for section in content_data:
        if section['type'] == 'title':
            story.append(Paragraph(section['text'], title_style))
        elif section['type'] == 'heading':
            story.append(Paragraph(section['text'], heading_style))
        elif section['type'] == 'paragraph':
            story.append(Paragraph(section['text'], body_style))
            story.append(Spacer(1, 12))
        elif section['type'] == 'table':
            table = Table(section['data'], colWidths=section.get('col_widths'))
            table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 12),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
                ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
                ('GRID', (0, 0), (-1, -1), 1, colors.black),
            ]))
            story.append(table)
            story.append(Spacer(1, 20))
        elif section['type'] == 'pagebreak':
            story.append(PageBreak())
    
    doc.build(story)
```

### Step 3: Execute via run_shell

Run the Python script directly using `run_shell`:

```bash
python3 << 'EOF'
# Your complete reportlab script here
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak

# ... rest of your script
EOF
```

Or save to a file and execute:

```bash
cat > generate_pdf.py << 'SCRIPT'
# Your complete script
SCRIPT

python3 generate_pdf.py
```

### Step 4: Handle Complex Content

For lengthy documents, structure content as a data structure:

```python
document_sections = [
    {'type': 'title', 'text': 'Document Title'},
    {'type': 'pagebreak'},
    {'type': 'heading', 'text': 'Section 1'},
    {'type': 'paragraph', 'text': 'Content here...'},
    {'type': 'heading', 'text': 'Section 2'},
    {'type': 'table', 'data': [['Header1', 'Header2'], ['Row1-Col1', 'Row1-Col2']]},
    {'type': 'pagebreak'},
    # Continue for all sections
]
```

### Step 5: Verify Output

Check that the PDF was created successfully:

```bash
ls -la your_document.pdf
# Optionally check page count
pdfinfo your_document.pdf 2>/dev/null || echo "PDF created, pdfinfo not available"
```

## Common Patterns

### Multi-Section Documents
Use `PageBreak()` between major sections to ensure clean pagination.

### Tables with Data
Structure tabular data as nested lists:
```python
table_data = [
    ['Column 1', 'Column 2', 'Column 3'],
    ['Value 1', 'Value 2', 'Value 3'],
    ['Value 4', 'Value 5', 'Value 6'],
]
```

### Styled Text
Create custom `ParagraphStyle` objects for consistent formatting across sections.

### Long Paragraphs
ReportLab automatically handles text wrapping. For very long content, consider breaking into multiple paragraphs.

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Import errors | Ensure reportlab is installed: `pip install reportlab` |
| Layout issues | Adjust margins in `SimpleDocTemplate` constructor |
| Text overflow | Use `Spacer` elements to add vertical space |
| Table width problems | Set explicit `colWidths` parameter |
| Page breaks in wrong places | Insert `PageBreak()` explicitly before new sections |

## Advantages Over shell_agent

- **Deterministic**: Code executes exactly as written
- **Debuggable**: Errors are immediate and clear
- **Controllable**: Full access to reportlab's API
- **Reliable**: No intermediate agent interpretation layer
- **Efficient**: Single execution, no retry loops