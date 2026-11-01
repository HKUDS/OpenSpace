---
name: pptx-debug-workflow
description: Systematic debugging workflow for python-pptx presentation generation
---

# PPTX Debugging Workflow

A systematic approach to creating and debugging PowerPoint presentations using the `python-pptx` library. This workflow ensures reliable presentation generation through iterative verification and error resolution.

## Step 1: Verify Installation

Before writing any code, verify that `python-pptx` is installed:

```bash
pip show python-pptx
```

If not installed:
```bash
pip install python-pptx
```

## Step 2: Test Minimal PPTX Creation

Create and run a minimal test script to verify the library works:

```python
from pptx import Presentation

prs = Presentation()
slide = prs.slides.add_slide(prs.slide_layouts[0])
slide.shapes.title.text = "Test Slide"
prs.save("test.pptx")
print("Test presentation created successfully")
```

Run this first to confirm basic functionality before building complex presentations.

## Step 3: Write Full Script to File

**Always write your presentation script to a `.py` file** instead of using heredoc or inline execution. This enables:
- Better error messages with line numbers
- Easier iteration and debugging
- Preserved state between error fixes

Example structure:
```python
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
from pptx.chart.data import CategoryChartData

def create_presentation():
    prs = Presentation()
    # Build slides here
    prs.save("output.pptx")
    print("Presentation saved successfully")

if __name__ == "__main__":
    create_presentation()
```

## Step 4: Execute and Capture Full Traceback

Run the script with full error output:

```bash
python your_script.py 2>&1 | tee debug_output.log
```

This captures the complete Python traceback including:
- Error type and message
- Line numbers where errors occurred
- Stack trace for debugging

## Step 5: Fix API Errors Iteratively

Common `python-pptx` API patterns to remember:

### Chart Legend Positioning
Use `XL_LEGEND_POSITION` (NOT `XL_CHART_TYPE`) for legend positioning:
```python
from pptx.enum.chart import XL_LEGEND_POSITION

chart.chart.has_legend = True
chart.chart.legend.position = XL_LEGEND_POSITION.RIGHT
```

### Table Cell Access
Access table cells through row iteration pattern:
```python
table = slide.shapes.add_table(rows=5, cols=4, left=Inches(1), top=Inches(2), width=Inches(6), height=Inches(3)).table

# Correct pattern - iterate through rows, then cells
for row_idx, row in enumerate(table.rows):
    for cell_idx, cell in enumerate(row.cells):
        cell.text = f"Row {row_idx}, Cell {cell_idx}"

# Or direct access
table.rows[0].cells[0].text = "Header"
```

### Chart Data Format
For pie charts, use category-based data:
```python
chart_data = CategoryChartData()
chart_data.categories = ['Category A', 'Category B', 'Category C']
chart_data.add_series('Series 1', (30, 45, 25))

x, y, cx, cy = Inches(2), Inches(2), Inches(6), Inches(4.5)
slide.shapes.add_chart(XL_CHART_TYPE.PIE, x, y, cx, cy, chart_data)
```

## Step 6: Validate Output

After successful execution:
1. Verify the `.pptx` file was created
2. Open in PowerPoint or compatible viewer
3. Check that all slides, tables, and charts render correctly

## Common Troubleshooting

| Issue | Solution |
|-------|----------|
| Module not found | `pip install python-pptx` |
| AttributeError on chart | Check `XL_LEGEND_POSITION` vs `XL_CHART_TYPE` |
| Table cell access fails | Use `table.rows[row].cells[cell]` pattern |
| Chart doesn't display | Verify chart_data format matches chart type |
| Import errors | Check enum imports from `pptx.enum.*` |

## Quick Reference Script Template

```python
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
from pptx.chart.data import CategoryChartData

def main():
    prs = Presentation()
    
    # Add title slide
    slide = prs.slides.add_slide(prs.slide_layouts[0])
    slide.shapes.title.text = "Title"
    
    # Add content slide with table
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    table = slide.shapes.add_table(3, 3, Inches(1), Inches(2), Inches(8), Inches(2)).table
    for row in table.rows:
        for cell in row.cells:
            cell.text = "Data"
    
    prs.save("presentation.pptx")
    print("Done!")

if __name__ == "__main__":
    main()
```