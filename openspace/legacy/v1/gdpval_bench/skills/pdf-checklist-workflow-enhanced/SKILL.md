---
name: unified-deliverables-flow
description: Generate spreadsheets, diagrams, and PDF reports in a phased workflow with explicit iteration budgets and error recovery
---

# Unified Multi-Deliverable Generation Workflow

This skill provides a structured, phased approach for creating multiple deliverable types (spreadsheets, diagrams, PDF reports) in a single cohesive workflow with explicit iteration budgeting to prevent premature exhaustion.

## Overview

Use this workflow when you need to:
- Generate **multiple deliverable types** (Excel, images, PDFs) in one task
- Ensure **balanced iteration allocation** across all deliverables
- Handle **tool failures gracefully** with retries and fallbacks
- Produce **downloadable artifacts** with verified paths

## Critical: Iteration Budget Allocation

**Allocate iterations BEFORE starting work:**

| Phase | Deliverable | Budget | Checkpoint |
|-------|-------------|--------|------------|
| Phase 1 | Spreadsheet | 8 iterations | File exists + readable |
| Phase 2 | Diagram | 8 iterations | File exists + >1KB |
| Phase 3 | PDF Report | 8 iterations | File exists + ARTIFACT_PATH output |
| Buffer | Error recovery | 6 iterations | Remaining for retries |
| **Total** | **All deliverables** | **30 iterations** | **All verified** |

**Rules:**
- Complete each phase before moving to the next
- If a phase exceeds budget, use fallback strategy (see below)
- Never spend >10 iterations on a single deliverable without checkpoint
- Verify each deliverable before proceeding

## Step-by-Step Instructions

### Phase 0: Pre-Work Setup (1 iteration max)

1. **Define all deliverables explicitly:**
   ```
   Deliverable 1: hardware_selection_table.xlsx (Excel with comparison data)
   Deliverable 2: cnc_workcell_layout.png (PNG diagram of layout)
   Deliverable 3: final_report.pdf (PDF summary report)
   ```

2. **Verify workspace is accessible:**
   ```
   run_shell
     command: ls -la /workspace/ && mkdir -p /workspace/artifacts
   ```

3. **Set path variables for consistency:**
   - All files go to `/workspace/` or `/workspace/artifacts/`
   - Use absolute paths in all code
   - Never use relative paths like `./output.xlsx`

### Phase 1: Spreadsheet Generation (Budget: 8 iterations)

**Step 1.1: Choose approach**

| Method | Tool | Best For |
|--------|------|----------|
| Direct Python | `execute_code_sandbox` | Quick generation, simple tables |
| Shell agent | `shell_agent` | Complex logic, error recovery |

**Step 1.2: Create spreadsheet code**

```python
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, Border, Side

def create_hardware_table():
    wb = Workbook()
    ws = wb.active
    ws.title = "Hardware Selection"
    
    # Headers with styling
    headers = ["Component", "Option A", "Option B", "Option C", "Recommendation"]
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal='center')
    
    # Data rows
    data = [
        ["Controller", "PLC-X100 ($500)", "PLC-Y200 ($650)", "PLC-Z300 ($800)", "PLC-Y200"],
        ["Motor", "Servo-500W ($300)", "Servo-750W ($400)", "Stepper-1kW ($250)", "Servo-750W"],
        ["Sensor", "Proximity-S1 ($50)", "Vision-V2 ($200)", "Laser-L3 ($150)", "Vision-V2"]
    ]
    
    for row_idx, row_data in enumerate(data, 2):
        for col_idx, value in enumerate(row_data, 1):
            ws.cell(row=row_idx, column=col_idx, value=value)
    
    # Auto-adjust column widths
    for col in ws.columns:
        max_length = max(len(str(cell.value)) for cell in col if cell.value)
        ws.column_dimensions[col[0].column_letter].width = min(max_length + 2, 25)
    
    output_path = '/workspace/hardware_selection_table.xlsx'
    wb.save(output_path)
    print(f'SUCCESS: Spreadsheet created at {output_path}')
    print(f'ARTIFACT_PATH:{output_path}')
    return output_path

create_hardware_table()
```

**Step 1.3: Execute with verification**

```
execute_code_sandbox
  code: <spreadsheet code from Step 1.2>
  language: python
```

**Step 1.4: Verify (REQUIRED before proceeding)**

```
run_shell
  command: ls -lh /workspace/*.xlsx && python3 -c "import openpyxl; openpyxl.load_workbook('/workspace/hardware_selection_table.xlsx')"
```

**Checkpoint criteria:**
- ✓ File exists
- ✓ Size > 1KB
- ✓ No errors opening file
- ✗ If failed, retry once with shell_agent, then proceed to Phase 2 with note

### Phase 2: Diagram Generation (Budget: 8 iterations)

**Step 2.1: Choose diagram type and tool**

| Diagram Type | Tool | Library |
|--------------|------|---------|
| Flowchart/Blocks | `execute_code_sandbox` | matplotlib, graphviz |
| Layout/Floorplan | `shell_agent` | matplotlib, PIL |
| Architecture | `execute_code_sandbox` | matplotlib, diagram |

**Step 2.2: Create diagram code (matplotlib example)**

```python
import matplotlib.pyplot as plt
import matplotlib.patches as patches

def create_layout_diagram():
    fig, ax = plt.subplots(figsize=(12, 8))
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 80)
    ax.set_aspect('equal')
    ax.set_title('CNC Workcell Layout', fontsize=16, pad=20)
    
    # Define workcell components
    components = [
        {'label': 'CNC Machine', 'xy': (30, 40), 'w': 25, 'h': 20, 'color': '#3498db'},
        {'label': 'Robot Arm', 'xy': (60, 40), 'w': 15, 'h': 15, 'color': '#e74c3c'},
        {'label': 'Material Rack', 'xy': (10, 20), 'w': 15, 'h': 40, 'color': '#2ecc71'},
        {'label': 'Control Panel', 'xy': (75, 60), 'w': 12, 'h': 10, 'color': '#f39c12'},
        {'label': 'Safety Zone', 'xy': (25, 35), 'w': 50, 'h': 30, 'color': 'none', 'edge': '#95a5a6', 'dashed': True}
    ]
    
    for comp in components:
        if comp.get('edge'):
            rect = patches.Rectangle(
                (comp['xy'][0], comp['xy'][1]), 
                comp['w'], comp['h'],
                linewidth=2, 
                edgecolor=comp['edge'],
                facecolor=comp['color'],
                linestyle='--' if comp.get('dashed') else '-'
            )
        else:
            rect = patches.Rectangle(
                (comp['xy'][0], comp['xy'][1]), 
                comp['w'], comp['h'],
                linewidth=2, 
                edgecolor='black',
                facecolor=comp['color']
            )
        ax.add_patch(rect)
        ax.text(
            comp['xy'][0] + comp['w']/2, 
            comp['xy'][1] + comp['h']/2, 
            comp['label'],
            ha='center', va='center', fontsize=10, fontweight='bold'
        )
    
    plt.grid(True, alpha=0.3)
    plt.xlabel('Distance (m)')
    plt.ylabel('Distance (m)')
    
    output_path = '/workspace/cnc_workcell_layout.png'
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f'SUCCESS: Diagram created at {output_path}')
    print(f'ARTIFACT_PATH:{output_path}')
    return output_path

create_layout_diagram()
```

**Step 2.3: Execute and verify**

```
execute_code_sandbox
  code: <diagram code from Step 2.2>
  language: python
```

**Verification:**
```
run_shell
  command: ls -lh /workspace/*.png && file /workspace/*.png
```

**Checkpoint criteria:**
- ✓ File exists
- ✓ Size > 1KB
- ✓ File type confirmed as PNG/image
- ✗ If failed after 2 retries, use shell_agent fallback, then proceed

### Phase 3: PDF Report Generation (Budget: 8 iterations)

**Step 3.1: Prepare content aggregation**

Gather data from previous phases:
- Spreadsheet: key findings, recommendations
- Diagram: visual summary
- Additional: scoring, conclusions

**Step 3.2: Create PDF generation code**

```python
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_CENTER, TA_LEFT

def create_final_report(output_path):
    doc = SimpleDocTemplate(output_path, pagesize=letter,
                           rightMargin=0.75*inch, leftMargin=0.75*inch,
                           topMargin=0.75*inch, bottomMargin=0.75*inch)
    styles = getSampleStyleSheet()
    story = []
    
    # Custom styles
    title_style = ParagraphStyle('CustomTitle', parent=styles['Heading1'],
                                  fontSize=20, spaceAfter=30, alignment=TA_CENTER,
                                  fontName='Helvetica-Bold')
    heading_style = ParagraphStyle('CustomHeading', parent=styles['Heading2'],
                                    fontSize=14, spaceAfter=12, spaceBefore=20,
                                    fontName='Helvetica-Bold')
    
    # Title
    story.append(Paragraph("CNC Workcell Implementation Report", title_style))
    story.append(Spacer(1, 0.5*inch))
    
    # Executive Summary
    story.append(Paragraph("Executive Summary", heading_style))
    summary_text = """
    This report presents the hardware selection analysis and workcell layout 
    for the proposed CNC implementation. After evaluating multiple options across 
    controllers, motors, and sensors, recommended configurations balance cost, 
    performance, and reliability.
    """
    story.append(Paragraph(summary_text, styles['Normal']))
    story.append(Spacer(1, 0.3*inch))
    
    # Hardware Selection Table
    story.append(Paragraph("Hardware Selection Summary", heading_style))
    data = [
        ['Component', 'Recommended Option', 'Cost', 'Rationale'],
        ['Controller', 'PLC-Y200', '$650', 'Best cost/performance balance'],
        ['Motor', 'Servo-750W', '$400', 'Adequate power with precision'],
        ['Sensor', 'Vision-V2', '$200', 'Enables quality inspection']
    ]
    
    table = Table(data, colWidths=[1.5*inch, 1.7*inch, 1*inch, 2.3*inch])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors '#2c3e50'),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 11),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE')
    ]))
    story.append(table)
    story.append(Spacer(1, 0.4*inch))
    
    # Layout Diagram Reference
    story.append(Paragraph("Workcell Layout", heading_style))
    story.append(Paragraph("See attached diagram: cnc_workcell_layout.png", styles['Normal']))
    story.append(Spacer(1, 0.2*inch))
    
    # Recommendations
    story.append(Paragraph("Implementation Recommendations", heading_style))
    rec_text = """
    1. Begin with controller installation and configuration
    2. Integrate motor drives and verify motion control
    3. Install and calibrate vision system
    4. Conduct safety zone validation
    5. Perform full system integration testing
    """
    story.append(Paragraph(rec_text, styles['Normal']))
    story.append(Spacer(1, 0.5*inch))
    
    # Footer
    story.append(Spacer(1, 0.5*inch))
    footer_style = ParagraphStyle('Footer', parent=styles['Normal'],
                                   fontSize=9, alignment=TA_CENTER, textColor=colors.grey)
    story.append(Paragraph("Generated by Unified Deliverables Workflow", footer_style))
    
    doc.build(story)
    print(f'SUCCESS: PDF report created at {output_path}')
    print(f'ARTIFACT_PATH:{output_path}')
    return output_path

create_final_report('/workspace/final_report.pdf')
```

**Step 3.3: Execute with explicit artifact path**

```
execute_code_sandbox
  code: <PDF code from Step 3.2>
  language: python
```

**Step 3.4: Final verification**

```
run_shell
  command: ls -lh /workspace/*.pdf && echo "---" && ls -lh /workspace/*.xlsx /workspace/*.png
```

**Checkpoint criteria:**
- ✓ PDF file exists
- ✓ Size > 10KB (reports should be substantial)
- ✓ ARTIFACT_PATH was output
- ✓ All three deliverables verified

## Error Recovery & Fallback Strategies

### When execute_code_sandbox fails repeatedly:

**Strategy 1: Use shell_agent (more resilient)**
```
shell_agent
  task: Create an Excel file at /workspace/hardware_selection_table.xlsx with hardware comparison data including controllers, motors, and sensors with costs and recommendations
```

**Strategy 2: Simplify the code**
- Remove complex styling
- Use basic libraries only (csv instead of openpyxl if needed)
- Reduce dependencies

**Strategy 3: Change output format**
- Excel → CSV if openpyxl fails
- PNG → SVG if PIL/matplotlib fails
- PDF → Markdown + convert later

### When iteration budget is running low:

| Iterations Remaining | Action |
|---------------------|--------|
| < 10 | Skip non-critical formatting, use minimal viable output |
| < 5 | Use simplest possible implementation, skip verification |
| < 3 | Output text summary with file paths, request manual generation |

## Best Practices

1. **Phase sequentially** - Complete and verify each phase before moving on
2. **Use absolute paths** - Always `/workspace/filename.ext`, never relative
3. **Output ARTIFACT_PATH** - Every successful generation must print this
4. **Verify before proceeding** - Run shell check after each deliverable
5. **Track iteration count** - Count tool calls, stop at 25 to leave buffer
6. **Simplify on failure** - If complex code fails, strip to minimum viable
7. **Document decisions** - Note why fallback was used for future reference

## Quick Reference: Tool Selection

| Task | Primary Tool | Fallback Tool |
|------|--------------|---------------|
| Spreadsheet | execute_code_sandbox (openpyxl) | shell_agent (pandas) |
| Diagram | execute_code_sandbox (matplotlib) | shell_agent (graphviz) |
| PDF Report | execute_code_sandbox (reportlab) | shell_agent (fpdf) |
| Verification | run_shell (ls, file) | read_file (for content check) |

## Complete Workflow Checklist

- [ ] Phase 0: Defined all 3 deliverables with paths
- [ ] Phase 0: Verified workspace accessibility
- [ ] Phase 1: Spreadsheet created and verified (< 8 iterations)
- [ ] Phase 2: Diagram created and verified (< 8 iterations)
- [ ] Phase 3: PDF created and verified (< 8 iterations)
- [ ] All ARTIFACT_PATH outputs captured
- [ ] Final verification: all files exist with appropriate sizes
- [ ] Total iterations used ≤ 30

## Troubleshooting

| Problem | Immediate Action |
|---------|-----------------|
| execute_code_sandbox returns "[ERROR] unknown error" | Retry once, then switch to shell_agent |
| File not found after execution | Check actual path with `run_shell: ls /workspace/` |
| ARTIFACT_PATH not output | Re-run with explicit print statement |
| Iteration budget nearly exhausted | Skip remaining deliverables, document what was created |
| Library import fails | Add `!pip install <library>` at code start |

## Related Tools

- `execute_code_sandbox` - Primary tool for code execution (Python)
- `shell_agent` - Fallback for complex tasks or when sandbox fails
- `run_shell` - Verification and workspace inspection
- `read_file` - Content verification (type: xlsx, png, pdf)
- `create_file` - Alternative for text-based outputs
