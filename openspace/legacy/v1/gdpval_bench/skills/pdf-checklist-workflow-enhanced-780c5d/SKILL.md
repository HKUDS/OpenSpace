---
name: unified-deliverable-workflow
description: Generate spreadsheets, diagrams, and PDF reports with iteration budgeting and error recovery
---

# Unified Multi-Deliverable Workflow

This skill provides a structured pattern for creating multiple document artifacts (spreadsheets, diagrams, PDF reports) in a single workflow with explicit iteration budgeting and robust error handling.

## Overview

Use this workflow when you need to:
- Generate multiple related deliverables (Excel, PNG diagrams, PDF reports)
- Manage iteration budget across different artifact types
- Handle execute_code_sandbox failures gracefully
- Ensure all deliverables are completed before budget exhaustion
- Create cohesive documentation packages with cross-referenced content

## Iteration Budget Template

**Default Allocation (adjust based on task complexity):**

| Phase | Deliverable | Iterations | Validation |
|-------|-------------|------------|------------|
| 1 | Spreadsheet (.xlsx) | 8-10 | File exists, readable, correct structure |
| 2 | Diagram (.png) | 8-10 | File exists, viewable, correct dimensions |
| 3 | PDF Report (.pdf) | 8-10 | File exists, downloadable, proper formatting |
| Buffer | Error recovery | 4-6 | Retry failed phases |

**Total recommended budget: 30-36 iterations**

## Pre-Flight Checks (Iteration 1-2)

Before generating any deliverables:

1. **Verify workspace path**: Always use `/workspace/` as base directory
2. **Test execute_code_sandbox**: Run a simple print statement to confirm tool works
3. **Check required libraries**: Plan to install via `!pip install` if needed
4. **Standardize filenames**: Use consistent naming pattern (e.g., `projectname_deliverable.ext`)

```python
# Quick sandbox test
print("SANDBOX_OK")
print(f"WORKSPACE_PATH:/workspace")
```

If this fails, use `run_shell` as fallback for file operations.

## Phase 1: Spreadsheet Creation (Iterations 3-12)

### Step 1.1: Plan Structure
Define your spreadsheet structure before coding:
- Sheet names
- Column headers
- Data types
- Any formulas or formatting

### Step 1.2: Write Generation Code

**Using openpyxl (recommended for .xlsx):**

```python
# Install if needed
# !pip install openpyxl

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment

wb = Workbook()
ws = wb.active
ws.title = "Data"

# Header row with styling
headers = ["Item", "Description", "Value", "Status"]
for col, header in enumerate(headers, 1):
    cell = ws.cell(row=1, column=col, value=header)
    cell.font = Font(bold=True)
    cell.fill = PatternFill(start_color="CCCCCC", end_color="CCCCCC", fill_type="solid")
    cell.alignment = Alignment(horizontal="center")

# Data rows
data = [
    ["Item 1", "Description 1", 100, "Complete"],
    ["Item 2", "Description 2", 200, "Pending"],
]
for row_idx, row_data in enumerate(data, 2):
    for col_idx, value in enumerate(row_data, 1):
        ws.cell(row=row_idx, column=col_idx, value=value)

# Auto-adjust column widths
for column in ws.columns:
    max_length = 0
    column_letter = column[0].column_letter
    for cell in column:
        try:
            if len(str(cell.value)) > max_length:
                max_length = len(str(cell.value))
        except:
            pass
    adjusted_width = min(max_length + 2, 50)
    ws.column_dimensions[column_letter].width = adjusted_width

# Save with standard path
output_path = "/workspace/hardware_selection_table.xlsx"
wb.save(output_path)
print(f"ARTIFACT_PATH:{output_path}")
print("SPREADSHEET_GENERATED")
```

### Step 1.3: Execute and Validate

```
execute_code_sandbox
  code: <your spreadsheet code>
  language: python
```

**Validation checklist:**
- [ ] ARTIFACT_PATH output present
- [ ] File exists: `run_shell command: ls -lh /workspace/*.xlsx`
- [ ] File size > 1KB
- [ ] No error messages in output

### Step 1.4: Error Recovery

| Error | Recovery Action |
|-------|-----------------|
| Library not found | Add `!pip install openpyxl` at code start |
| '[ERROR] unknown error' | Retry once, then try simpler code structure |
| File not created | Check path is `/workspace/` not `./` or `/tmp/` |
| Permission denied | Ensure no file lock from previous execution |

**If 2 consecutive failures:** Skip to Phase 2 and return later with reduced scope.

## Phase 2: Diagram Generation (Iterations 13-22)

### Step 2.1: Choose Diagram Type

| Type | Library | Best For |
|------|---------|----------|
| Flowchart | `graphviz` or `matplotlib` | Process flows, decision trees |
| Layout/Diagram | `matplotlib` | Physical layouts, workcell designs |
| Network/Topology | `networkx` + `matplotlib` | System architecture |
| Simple shapes | `PIL/Pillow` | Basic boxes, arrows, labels |

### Step 2.2: Write Generation Code

**Using matplotlib for layout diagrams:**

```python
# Install if needed
# !pip install matplotlib

import matplotlib.pyplot as plt
import matplotlib.patches as patches

fig, ax = plt.subplots(figsize=(12, 8))
ax.set_xlim(0, 100)
ax.set_ylim(0, 80)
ax.set_aspect('equal')

# Add components (example: workcell layout)
components = [
    {"label": "Robot", "x": 30, "y": 40, "w": 20, "h": 15, "color": "lightblue"},
    {"label": "Conveyor", "x": 60, "y": 35, "w": 30, "h": 10, "color": "lightgreen"},
    {"label": "Safety Zone", "x": 10, "y": 10, "w": 80, "h": 60, "color": "none", "border": "red"},
]

for comp in components:
    rect = patches.Rectangle(
        (comp["x"], comp["y"]), 
        comp["w"], 
        comp["h"],
        linewidth=2, 
        edgecolor="black" if comp.get("border") else "black",
        facecolor=comp["color"] if comp["color"] != "none" else "white",
        linestyle="--" if comp.get("border") else "-"
    )
    ax.add_patch(rect)
    ax.text(
        comp["x"] + comp["w"]/2, 
        comp["y"] + comp["h"]/2, 
        comp["label"],
        ha="center", 
        va="center",
        fontsize=10,
        fontweight="bold"
    )

# Remove axis, add title
ax.axis("off")
plt.title("Workcell Layout Diagram", fontsize=14, fontweight="bold", pad=20)
plt.tight_layout()

# Save with standard path
output_path = "/workspace/cnc_workcell_layout.png"
plt.savefig(output_path, dpi=150, bbox_inches="tight")
print(f"ARTIFACT_PATH:{output_path}")
print("DIAGRAM_GENERATED")
```

### Step 2.3: Execute and Validate

```
execute_code_sandbox
  code: <your diagram code>
  language: python
```

**Validation checklist:**
- [ ] ARTIFACT_PATH output present
- [ ] File exists: `run_shell command: ls -lh /workspace/*.png`
- [ ] File size > 5KB (diagrams should be substantial)
- [ ] No error messages in output

### Step 2.4: Error Recovery

| Error | Recovery Action |
|-------|-----------------|
| Backend unknown error | Retry with simplified diagram (fewer elements) |
| Font rendering issues | Use default matplotlib fonts, avoid custom fonts |
| Memory issues | Reduce figure size or DPI |
| Import errors | Add `!pip install matplotlib pillow` at start |

**If 2 consecutive failures:** Create a simpler placeholder diagram and move to Phase 3.

## Phase 3: PDF Report Generation (Iterations 23-32)

### Step 3.1: Gather Content from Previous Phases

Before generating PDF, collect:
- Data from spreadsheet (reference key findings)
- Diagram file path for embedding or referencing
- Any additional text content or analysis

### Step 3.2: Write Generation Code

**Using reportlab for professional reports:**

```python
# Install if needed
# !pip install reportlab

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch

def create_pdf_report(output_path, spreadsheet_path, diagram_path):
    doc = SimpleDocTemplate(output_path, pagesize=letter)
    styles = getSampleStyleSheet()
    story = []
    
    # Title
    title_style = ParagraphStyle('CustomTitle', parent=styles['Heading1'],
                                  fontSize=18, spaceAfter=30, alignment=1)
    story.append(Paragraph("Project Deliverables Report", title_style))
    story.append(Spacer(1, 0.3*inch))
    
    # Executive Summary
    story.append(Paragraph("Executive Summary", styles['Heading2']))
    story.append(Paragraph("This report summarizes the project deliverables including hardware selection, layout design, and implementation recommendations.", styles['Normal']))
    story.append(Spacer(1, 0.2*inch))
    
    # Spreadsheet Reference Section
    story.append(Paragraph("Hardware Selection Summary", styles['Heading2']))
    story.append(Paragraph(f"See: {spreadsheet_path}", styles['Normal']))
    
    # Sample data table
    data = [
        ['Component', 'Selected Option', 'Status'],
        ['Robot Arm', 'Model X-200', 'Approved'],
        ['Controller', 'Unity Pro', 'Approved'],
        ['Safety System', 'Light Curtain', 'Pending']
    ]
    
    table = Table(data, colWidths=[2*inch, 2*inch, 1.5*inch])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('GRID', (0, 0), (-1, -1), 1, colors.black)
    ]))
    story.append(table)
    story.append(Spacer(1, 0.3*inch))
    
    # Diagram Reference Section
    story.append(Paragraph("Layout Diagram", styles['Heading2']))
    story.append(Paragraph(f"See: {diagram_path}", styles['Normal']))
    story.append(Spacer(1, 0.2*inch))
    
    # Try to embed diagram if it exists
    try:
        img = Image(diagram_path, width=6*inch, height=4*inch)
        story.append(img)
        story.append(Spacer(1, 0.2*inch))
    except:
        story.append(Paragraph("Diagram referenced but not embedded.", styles['Normal']))
    
    # Recommendations
    story.append(Paragraph("Recommendations", styles['Heading2']))
    recommendations = [
        "Proceed with approved hardware components",
        "Complete safety system evaluation before deployment",
        "Schedule installation during planned maintenance window"
    ]
    for i, rec in enumerate(recommendations, 1):
        story.append(Paragraph(f"{i}. {rec}", styles['Normal']))
    
    # Build PDF
    doc.build(story)
    print(f"ARTIFACT_PATH:{output_path}")
    print("PDF_REPORT_GENERATED")

create_pdf_report(
    '/workspace/project_report.pdf',
    '/workspace/hardware_selection_table.xlsx',
    '/workspace/cnc_workcell_layout.png'
)
```

### Step 3.3: Execute and Validate

```
execute_code_sandbox
  code: <your PDF code>
  language: python
```

**Validation checklist:**
- [ ] ARTIFACT_PATH output present
- [ ] File exists: `run_shell command: ls -lh /workspace/*.pdf`
- [ ] File size > 10KB (reports should have substantial content)
- [ ] All previous deliverables referenced correctly

### Step 3.4: Error Recovery

| Error | Recovery Action |
|-------|-----------------|
| Font errors | Use standard fonts (Helvetica, Arial, Courier) |
| Image embed fails | Reference diagram path in text instead |
| Unknown error | Simplify report structure, remove images |
| Path issues | Hardcode absolute `/workspace/` paths |

**If failures persist:** Generate a text-only PDF with basic structure.

## Phase 4: Final Validation (Iterations 33-36)

### Step 4.1: Verify All Deliverables

```
run_shell
  command: ls -lh /workspace/*.xlsx /workspace/*.png /workspace/*.pdf
```

### Step 4.2: Check File Integrity

```
run_shell
  command: file /workspace/*.xlsx /workspace/*.png /workspace/*.pdf
```

### Step 4.3: Confirm Download Paths

Ensure each deliverable has `ARTIFACT_PATH:` prefix in execution output for proper download handling.

## Cross-Phase Best Practices

### Path Standardization
- **Always use**: `/workspace/filename.ext`
- **Never use**: `./filename.ext`, `/tmp/filename.ext`, or relative paths
- **Consistent naming**: `projectname_deliverabletype.ext`

### Iteration Management
- Track iterations used per phase
- If Phase 1 uses >12 iterations, reduce Phase 2 and 3 budgets
- Reserve minimum 4 iterations for error recovery
- **Hard stop at iteration 28**: Begin PDF generation regardless of previous phase completion status

### Error Handling Strategy
1. **First failure**: Retry same code once
2. **Second failure**: Simplify the approach (reduce complexity)
3. **Third failure**: Create minimal viable deliverable and move on
4. **Document failures**: Note what failed for post-task review

### Tool Failure Workarounds

| Tool | Primary Use | Fallback |
|------|-------------|----------|
| execute_code_sandbox | Run Python code | run_shell with python -c |
| read_file | Verify file content | run_shell cat/head |
| run_shell | Check file existence | execute_code_sandbox with os.path |

## Quick Start Template

```python
# Unified deliverable generator - minimal viable version
# Run in execute_code_sandbox

# ===== SPREADSHEET =====
from openpyxl import Workbook
wb = Workbook()
ws = wb.active
ws.append(["Item", "Value", "Status"])
ws.append(["Component A", 100, "OK"])
wb.save("/workspace/data.xlsx")
print("ARTIFACT_PATH:/workspace/data.xlsx")

# ===== DIAGRAM =====
import matplotlib.pyplot as plt
fig, ax = plt.subplots(figsize=(8, 6))
ax.text(0.5, 0.5, "Diagram Placeholder", ha='center', va='center', fontsize=16)
ax.axis('off')
plt.savefig("/workspace/diagram.png", dpi=100)
print("ARTIFACT_PATH:/workspace/diagram.png")

# ===== PDF =====
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph
from reportlab.lib.styles import getSampleStyleSheet
doc = SimpleDocTemplate("/workspace/report.pdf", pagesize=letter)
styles = getSampleStyleSheet()
story = [Paragraph("Report", styles['Heading1']), 
         Paragraph("Generated successfully.", styles['Normal'])]
doc.build(story)
print("ARTIFACT_PATH:/workspace/report.pdf")
```

## Related Tools

- `execute_code_sandbox` - Run Python code for all deliverable generation
- `read_file` - Verify file content (type: xlsx, png, pdf)
- `run_shell` - Check file existence, size, and integrity
- `create_file` - For simple text-based deliverables if needed

## Common Pitfalls and Solutions

| Pitfall | Solution |
|---------|----------|
| Exhausting budget on Phase 1 | Set hard iteration limit per phase (max 12) |
| Workspace path confusion | Always use absolute `/workspace/` paths |
| Repeated unknown errors | Simplify code, reduce library dependencies |
| Missing ARTIFACT_PATH | Always print prefix for each deliverable |
| PDF created but not downloadable | Verify ARTIFACT_PATH is on its own line |
| Diagram too complex | Start with basic shapes, add detail only if time permits |

## Success Criteria

A successful execution should produce:
- [ ] 3 deliverable files in `/workspace/`
- [ ] Each file > minimum size threshold
- [ ] Each execution outputs `ARTIFACT_PATH:` prefix
- [ ] Completed before iteration budget exhaustion
- [ ] No unrecovered tool errors
