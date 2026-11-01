---
name: pdf-checklist-generation
description: Generate structured PDF checklists and reports using Python libraries in sandbox
---

# PDF Checklist Generation

This skill provides a workflow for creating professional PDF documents (checklists, reports, scorecards) using Python libraries within an execute_code_sandbox environment.

## When to Use

- Need to generate downloadable PDF reports or checklists
- Require structured documents with tables, sections, or scoring criteria
- Working in a sandbox environment without direct file system access

## Available Libraries

Choose one based on your needs:

| Library | Best For | Complexity |
|---------|----------|------------|
| `reportlab` | Professional layouts, tables, precise control | Medium |
| `fpdf` | Simple documents, quick generation | Low |
| `matplotlib` | Charts, graphs, visual reports | Medium |

## Step-by-Step Workflow

### Step 1: Install Required Library

```python
!pip install reportlab  # or fpdf, matplotlib
```

### Step 2: Create PDF Generation Script

**Example using reportlab:**

```python
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors

def create_checklist_pdf(filename, checklist_items):
    doc = SimpleDocTemplate(filename, pagesize=letter)
    styles = getSampleStyleSheet()
    elements = []
    
    # Title
    elements.append(Paragraph("Assessment Checklist", styles['Heading1']))
    elements.append(Spacer(1, 12))
    
    # Table data
    data = [['Item', 'Status', 'Score']]
    for item in checklist_items:
        data.append([item['name'], item['status'], item['score']])
    
    # Create table
    table = Table(data)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
    ]))
    elements.append(table)
    
    doc.build(elements)
    print(f"PDF created: {filename}")

# Usage
checklist = [
    {'name': 'Security Check', 'status': 'Pass', 'score': 10},
    {'name': 'Performance Test', 'status': 'Fail', 'score': 5},
]
create_checklist_pdf('report.pdf', checklist)
```

### Step 3: Execute in Sandbox

Run the script using `execute_code_sandbox`:

```
execute_code_sandbox(code="<your pdf generation code>")
```

### Step 4: Verify Output

After generation, verify the PDF was created:

```
read_file(path="report.pdf")
```

Or inspect via shell:

```
execute_code_sandbox(code="import os; print(os.path.exists('report.pdf'))")
```

## Best Practices

1. **Keep it simple**: Start with basic layouts before adding complexity
2. **Test incrementally**: Generate a minimal PDF first, then add features
3. **Handle errors**: Wrap PDF generation in try/except blocks
4. **Verify file existence**: Always confirm the PDF was created before proceeding
5. **Use UTF-8**: Ensure text encoding is proper for special characters

## Common Pitfalls

- Forgetting to install the library before import
- Not checking if file was created successfully
- Using paths that don't exist in sandbox environment
- Missing required dependencies for advanced features

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Import error | Run `!pip install <library>` first |
| File not found | Check working directory with `!pwd` |
| Blank PDF | Ensure `doc.build()` is called |
| Encoding issues | Use Unicode strings, specify encoding |