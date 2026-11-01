---
name: integrated-report-bundle
description: Generate spreadsheet, diagram, and PDF report as a cohesive deliverable set with iteration budgeting per component
---

# Integrated Report Bundle Workflow

This skill provides a reusable pattern for creating a complete report package consisting of three interconnected deliverables: a spreadsheet (data table), a diagram (visual layout), and a PDF report (summary document). Designed for resilience with explicit iteration budgeting and workspace management.

## Overview

Use this workflow when you need to:
- Generate multiple interconnected deliverables in a single task
- Create data spreadsheets with structured tables
- Produce visual diagrams (PNG/SVG) for layouts or workflows
- Generate summary PDF reports with scoring or assessments
- Manage iteration budgets across multiple components
- Handle sandbox execution failures gracefully

## Critical: Iteration Budget Allocation

**Total budget must be divided across deliverables:**

| Deliverable | Recommended Budget | Purpose |
|-------------|-------------------|---------|
| Spreadsheet | 8-10 iterations | Data gathering, table creation, validation |
| Diagram | 8-10 iterations | Visual generation, layout refinement |
| PDF Report | 8-10 iterations | Report assembly, formatting, final output |
| Buffer | 2-4 iterations | Error recovery, verification, fixes |

**Never spend entire budget on one deliverable.** Track iterations explicitly.

## Step-by-Step Instructions

### Phase 0: Workspace Preparation (1 iteration)

Before generating any deliverables, establish clean workspace state:

```bash
# Check current workspace state
ls -la /workspace/

# Create dedicated output directory
mkdir -p /workspace/deliverables

# Verify write permissions
touch /workspace/deliverables/test.tmp && rm /workspace/deliverables/test.tmp
```

**Why:** Prevents workspace path confusion that caused previous failures.

### Phase 1: Spreadsheet Creation (8-10 iterations max)

#### Step 1.1: Choose Approach

| Mode | Tool | Use When |
|------|------|----------|
| A | `execute_code_sandbox` with `openpyxl`/`pandas` | Programmatic table generation |
| B | `shell_agent` with Python script | Complex data processing |
| C | `create_file` (CSV/XLSX) | Simple tabular data |

#### Step 1.2: Generate Spreadsheet Code

**Example using openpyxl:**

```python
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment

wb = Workbook()
ws = wb.active
ws.title = "Hardware Selection"

# Headers with styling
headers = ["Component", "Model", "Qty", "Unit Price", "Total"]
for col, header in enumerate(headers, 1):
    cell = ws.cell(row=1, column=col, value=header)
    cell.font = Font(bold=True)
    cell.fill = PatternFill(start_color="4472C4", fill_type="solid")
    cell.font = Font(color="FFFFFF", bold=True)

# Data rows
data = [
    ["CNC Controller", "Fanuc 0i-MF", 1, 2500, 2500],
    ["Servo Motor", "SGM7G-09A", 4, 450, 1800],
    ["HMI Panel", "GT2110-WTBD", 1, 380, 380]
]

for row_idx, row_data in enumerate(data, 2):
    for col_idx, value in enumerate(row_data, 1):
        ws.cell(row=row_idx, column=col_idx, value=value)

# Total row
ws.cell(row=len(data)+2, column=4, value="TOTAL:")
ws.cell(row=len(data)+2, column=5, value=f"=E2:E{len(data)+1}")

# Save
output_path = '/workspace/deliverables/hardware_selection_table.xlsx'
wb.save(output_path)
print(f'ARTIFACT_PATH:{output_path}')
print('Spreadsheet created successfully')
```

#### Step 1.3: Execute and Verify

```
execute_code_sandbox
  code: <spreadsheet generation code>
  language: python
```

**Verification (use 1 iteration):**
```bash
run_shell
  command: ls -lh /workspace/deliverables/*.xlsx && file /workspace/deliverables/*.xlsx
```

**If execute_code_sandbox fails with '[ERROR] unknown error':**
1. Retry once with `shell_agent` instead
2. If still failing, fall back to `create_file` with CSV format
3. Document the failure and proceed to next deliverable

**Iteration checkpoint:** After 8 iterations on spreadsheet, move to diagram regardless of completion status.

### Phase 2: Diagram Generation (8-10 iterations max)

#### Step 2.1: Choose Diagram Type

| Type | Tool | Best For |
|------|------|----------|
| Flowchart | `graphviz` + `pydot` | Process flows, decision trees |
| Layout | `matplotlib` + `patches` | Physical layouts, workcells |
| Architecture | `diagrams` library | System architecture |
| Simple shapes | `PIL/Pillow` | Basic diagrams |

#### Step 2.2: Generate Diagram Code

**Example using matplotlib for layout diagram:**

```python
import matplotlib.pyplot as plt
import matplotlib.patches as patches

fig, ax = plt.subplots(figsize=(12, 8))
ax.set_xlim(0, 100)
ax.set_ylim(0, 80)
ax.set_aspect('equal')
ax.axis('off')

# Title
ax.text(50, 75, 'CNC Workcell Layout', fontsize=16, ha='center', 
        fontweight='bold', bbox=dict(boxstyle='round', facecolor='wheat'))

# CNC Machine
cnc = patches.Rectangle((10, 30), 25, 20, linewidth=2, 
                        edgecolor='darkblue', facecolor='lightblue',
                        label='CNC Machine')
ax.add_patch(cnc)
ax.text(22.5, 40, 'CNC\nMachine', ha='center', va='center', fontsize=10)

# Robot Arm
robot = patches.Circle((50, 40), 8, linewidth=2,
                       edgecolor='darkgreen', facecolor='lightgreen',
                       label='Robot')
ax.add_patch(robot)
ax.text(50, 40, 'Robot', ha='center', va='center', fontsize=10)

# Material Storage
storage = patches.Rectangle((70, 30), 20, 20, linewidth=2,
                           edgecolor='darkred', facecolor='lightcoral',
                           label='Storage')
ax.add_patch(storage)
ax.text(80, 40, 'Material\nStorage', ha='center', va='center', fontsize=9)

# Arrows showing flow
ax.annotate('', xy=(35, 40), xytext=(42, 40),
           arrowprops=dict(arrowstyle='->', color='gray', lw=2))
ax.annotate('', xy=(58, 40), xytext=(65, 40),
           arrowprops=dict(arrowstyle='->', color='gray', lw=2))

# Legend
ax.legend(loc='upper right', bbox_to_anchor=(0.98, 0.98))

# Save
output_path = '/workspace/deliverables/cnc_workcell_layout.png'
plt.savefig(output_path, dpi=150, bbox_inches='tight')
print(f'ARTIFACT_PATH:{output_path}')
print('Diagram created successfully')
```

#### Step 2.3: Execute and Verify

```
execute_code_sandbox
  code: <diagram generation code>
  language: python
```

**Verification:**
```bash
run_shell
  command: ls -lh /workspace/deliverables/*.png && file /workspace/deliverables/*.png
```

**If execute_code_sandbox fails:**
1. First retry: Simplify the diagram (remove complex elements)
2. Second retry: Use `shell_agent` with explicit matplotlib installation
3. Third retry: Create ASCII/text-based diagram as fallback
4. Document and proceed (diagram is often optional compared to PDF)

**Iteration checkpoint:** After 8 iterations on diagram, move to PDF regardless of completion status.

### Phase 3: PDF Report Generation (8-10 iterations max)

#### Step 3.1: Choose PDF Library

| Library | Best For | Installation |
|---------|----------|--------------|
| `fpdf2` | Simple reports, quick setup | `!pip install fpdf2` |
| `reportlab` | Professional reports, tables | `!pip install reportlab` |
| `matplotlib` | Chart-heavy reports | Built-in |

#### Step 3.2: Generate PDF Code

**Example using fpdf2 (simplest, most reliable):**

```python
from fpdf import FPDF

class ReportPDF(FPDF):
    def header(self):
        self.set_font('Arial', 'B', 14)
        self.cell(0, 10, 'Project Assessment Report', 0, 1, 'C')
        self.line(10, 20, 200, 20)
        self.ln(10)
    
    def footer(self):
        self.set_y(-15)
        self.set_font('Arial', 'I', 8)
        self.cell(0, 10, f'Page {self.page_no()}', 0, 0, 'C')
    
    def section(self, title):
        self.set_font('Arial', 'B', 12)
        self.set_fill_color(200, 220, 255)
        self.cell(0, 8, title, 0, 1, 'L', 1)
        self.ln(3)
    
    def add_table_row(self, col1, col2, col3):
        self.set_font('Arial', '', 10)
        self.cell(60, 8, col1, 1)
        self.cell(80, 8, col2, 1)
        self.cell(40, 8, col3, 1)
        self.ln()

pdf = ReportPDF()
pdf.add_page()
pdf.set_auto_page_break(auto=True, margin=15)

# Executive Summary
pdf.section('Executive Summary')
pdf.set_font('Arial', '', 11)
pdf.multi_cell(0, 7, 'This report provides a comprehensive assessment of the CNC workcell implementation project, including hardware selection, layout design, and cost analysis.')
pdf.ln(5)

# Hardware Selection Summary
pdf.section('Hardware Selection')
pdf.add_table_row('Component', 'Model', 'Cost')
pdf.add_table_row('CNC Controller', 'Fanuc 0i-MF', '$2,500')
pdf.add_table_row('Servo Motors', 'SGM7G-09A (x4)', '$1,800')
pdf.add_table_row('HMI Panel', 'GT2110-WTBD', '$380')
pdf.ln(5)

# Assessment Scores
pdf.section('Assessment Scores')
pdf.add_table_row('Criteria', 'Description', 'Score')
pdf.add_table_row('Completeness', 'All requirements addressed', '8/10')
pdf.add_table_row('Feasibility', 'Technical implementation possible', '9/10')
pdf.add_table_row('Cost Efficiency', 'Within budget constraints', '7/10')
pdf.ln(5)

# Conclusion
pdf.section('Conclusion')
pdf.multi_cell(0, 7, 'The proposed CNC workcell meets all critical requirements with a recommended implementation approach. Total estimated cost: $4,680.')

# Save
output_path = '/workspace/deliverables/assessment_report.pdf'
pdf.output(output_path)
print(f'ARTIFACT_PATH:{output_path}')
print('PDF report created successfully')
```

#### Step 3.3: Execute and Verify

```
execute_code_sandbox
  code: <PDF generation code>
  language: python
```

**Verification (critical - use read_file):**
```
read_file
  filetype: pdf
  file_path: /workspace/deliverables/assessment_report.pdf
```

**Alternative verification:**
```bash
run_shell
  command: ls -lh /workspace/deliverables/*.pdf && pdftk /workspace/deliverables/*.pdf dump_data 2>/dev/null | head -20
```

### Phase 4: Final Bundle Verification (2-4 iterations)

Verify all deliverables exist and are valid:

```bash
run_shell
  command: |
    echo "=== Deliverable Bundle Verification ==="
    echo ""
    echo "Spreadsheet:"
    ls -lh /workspace/deliverables/*.xlsx 2>/dev/null || echo "  NOT FOUND"
    echo ""
    echo "Diagram:"
    ls -lh /workspace/deliverables/*.png 2>/dev/null || echo "  NOT FOUND"
    echo ""
    echo "PDF Report:"
    ls -lh /workspace/deliverables/*.pdf 2>/dev/null || echo "  NOT FOUND"
    echo ""
    echo "=== Bundle Complete ==="
```

**Checklist:**
- [ ] At least 2 of 3 deliverables created
- [ ] All files > 1KB (not empty)
- [ ] ARTIFACT_PATH outputs visible in execution logs
- [ ] No unresolved execute_code_sandbox errors

## Error Handling Matrix

| Error Type | Detection | Recovery Action | Max Retries |
|------------|-----------|-----------------|-------------|
| `[ERROR] unknown error` (execute_code_sandbox) | Error in tool output | Switch to `shell_agent` or simplify code | 2 |
| File not found | Verification step fails | Check path, re-run generation | 1 |
| Library not installed | ImportError in output | Add `!pip install` at code start | 1 |
| Empty file (0 bytes) | `ls -lh` shows 0 size | Regenerate with error handling | 1 |
| Iteration budget exceeded | Iteration count reached | Document partial completion, proceed | N/A |

## Workspace Path Convention

**Always use:** `/workspace/deliverables/filename.extension`

**Never use:**
- `/workspace/filename.extension` (root clutter)
- `./filename.extension` (ambiguous)
- `filename.extension` (missing path)

## Template: Quick Start All Three Deliverables

**Use this template structure to generate all three deliverables efficiently:**

```python
# DELIVERABLE 1: Spreadsheet
import openpyxl
wb = openpyxl.Workbook()
ws = wb.active
ws['A1'] = 'Header'
wb.save('/workspace/deliverables/data.xlsx')
print('ARTIFACT_PATH:/workspace/deliverables/data.xlsx')

# DELIVERABLE 2: Diagram  
import matplotlib.pyplot as plt
fig, ax = plt.subplots()
ax.text(0.5, 0.5, 'Diagram')
plt.savefig('/workspace/deliverables/diagram.png')
print('ARTIFACT_PATH:/workspace/deliverables/diagram.png')

# DELIVERABLE 3: PDF
from fpdf import FPDF
pdf = FPDF()
pdf.add_page()
pdf.set_font('Arial', '', 12)
pdf.cell(0, 10, 'Report')
pdf.output('/workspace/deliverables/report.pdf')
print('ARTIFACT_PATH:/workspace/deliverables/report.pdf')
```

## Iteration Tracking Template

Track your iteration usage explicitly:

```
Iteration Log:
- Iteration 1-2: Workspace preparation
- Iteration 3-10: Spreadsheet creation (8 used)
- Iteration 11-18: Diagram generation (8 used)
- Iteration 19-26: PDF report (8 used)
- Iteration 27-30: Bundle verification (4 buffer)
Total: 30 iterations
```

## Best Practices

1. **Budget discipline** - Never exceed 10 iterations on any single deliverable
2. **Early verification** - Verify each deliverable before moving to next phase
3. **Graceful degradation** - If one deliverable fails, complete the others
4. **Consistent paths** - Always use `/workspace/deliverables/` prefix
5. **ARTIFACT_PATH output** - Essential for download capability
6. **Error logging** - Document failures and recovery actions taken
7. **Fallback strategies** - Have alternative approaches ready for each deliverable type

## Related Tools

- `execute_code_sandbox` - Primary tool for all code generation
- `shell_agent` - Fallback when execute_code_sandbox fails repeatedly
- `read_file` - Verify PDF content (filetype: pdf, xlsx, png)
- `run_shell` - Check file existence, sizes, and workspace state
- `create_file` - Fallback for simple data files
- `list_dir` - Verify deliverables directory contents

## Anti-Patterns to Avoid

1. **Don't** spend all iterations on one deliverable
2. **Don't** ignore execute_code_sandbox errors - address immediately
3. **Don't** use inconsistent paths across deliverables
4. **Don't** skip verification steps
5. **Don't** assume a deliverable succeeded without checking

## Success Criteria

A successful execution produces:
- Minimum 2 of 3 deliverables (spreadsheet, diagram, PDF)
- All deliverables in `/workspace/deliverables/`
- Valid, non-empty files (>1KB each)
- Clear ARTIFACT_PATH outputs in execution logs
- Iteration budget not exceeded

*** End Files
