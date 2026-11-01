---
name: pdf-checklist-workflow
description: Generate structured PDF documents with tables, sections, and scoring using Python libraries in execute_code_sandbox
---

# PDF Checklist/Report Generation Workflow

This skill provides a reusable pattern for creating structured PDF documents such as checklists, reports, or assessments using Python in a sandboxed environment.

## Overview

Use this workflow when you need to:
 - Generate PDF checklists, reports, or assessments
 - Create structured documents with tables, sections, and headers
 - Include scoring criteria or evaluation frameworks
 - Produce downloadable artifacts for users
 
 **Primary method**: Use `execute_code_sandbox` for Python PDF generation
 **Fallback method**: If `execute_code_sandbox` fails, create and run Python scripts via shell commands

## Step-by-Step Instructions

### Step 1: Choose a PDF Library

Select a Python library based on your needs:

| Library | Best For | Complexity |
|---------|----------|------------|
| `reportlab` | Professional reports, complex layouts | Medium |
| `fpdf` / `fpdf2` | Simple documents, quick generation | Low |
| `matplotlib` | Charts, graphs, visual elements | Medium |

### Step 2: Write PDF Generation Code in execute_code_sandbox

Use the `execute_code_sandbox` tool with Python code that:
1. Imports the chosen PDF library
2. Defines document structure (title, sections, tables)
3. Adds content (text, tables, scores, criteria)
4. Saves to a file path like `/workspace/output.pdf`
5. Outputs the path using `ARTIFACT_PATH:` prefix for download
 
 **Determine workspace directory dynamically**:
 Before generating PDFs, identify the correct output path:
 ```bash
 # Option 1: Use current directory
 WORKDIR=$(pwd)
 
 # Option 2: Check common workspace locations
 if [ -d "/workspace" ]; then WORKDIR="/workspace"
 elif [ -d "$HOME/workspace" ]; then WORKDIR="$HOME/workspace"
 else WORKDIR=$(pwd); fi
 ```
 
 In Python code, use environment variables or dynamic path detection:
 ```python
 import os
 workspace = os.environ.get('WORKSPACE', os.getcwd())
 output_path = os.path.join(workspace, 'output.pdf')
 ```

**Example using fpdf2:**

```python
from fpdf import FPDF

class PDFChecklist(FPDF):
    def header(self):
        self.set_font('Arial', 'B', 16)
        self.cell(0, 10, 'Assessment Checklist', 0, 1, 'C')
        self.ln(10)
    
    def section_title(self, title):
        self.set_font('Arial', 'B', 12)
        self.cell(0, 10, title, 0, 1, 'L')
        self.ln(5)
    
    def add_checklist_item(self, item, criteria, score):
        self.set_font('Arial', '', 10)
        self.cell(100, 8, item, 1)
        self.cell(60, 8, criteria, 1)
        self.cell(30, 8, str(score), 1)
        self.ln()

# Create PDF
pdf = PDFChecklist()
pdf.add_page()
pdf.set_auto_page_break(auto=True, margin=15)

# Add sections
pdf.section_title('Evaluation Criteria')
pdf.add_checklist_item('Requirement 1', 'Must meet standard', 5)
pdf.add_checklist_item('Requirement 2', 'Should be complete', 4)

# Save
output_path = '/workspace/checklist.pdf'
pdf.output(output_path)
print(f'ARTIFACT_PATH:{output_path}')
```

**Example using reportlab (more professional):**

```python
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch

def create_pdf_report(output_path):
    doc = SimpleDocTemplate(output_path, pagesize=letter)
    styles = getSampleStyleSheet()
    story = []
    
    # Title
    title_style = ParagraphStyle('CustomTitle', parent=styles['Heading1'],
                                  fontSize=18, spaceAfter=30, alignment=1)
    story.append(Paragraph("Assessment Report", title_style))
    story.append(Spacer(1, 0.3*inch))
    
    # Section header
    story.append(Paragraph("Evaluation Summary", styles['Heading2']))
    story.append(Spacer(1, 0.2*inch))
    
    # Data table
    data = [
        ['Criteria', 'Description', 'Score'],
        ['Completeness', 'All sections filled', '8/10'],
        ['Accuracy', 'Information verified', '9/10'],
        ['Clarity', 'Easy to understand', '7/10']
    ]
    
    table = Table(data, colWidths=[2*inch, 2.5*inch, 1*inch])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
        ('GRID', (0, 0), (-1, -1), 1, colors.black)
    ]))
    story.append(table)
    
    doc.build(story)
    print(f'ARTIFACT_PATH:{output_path}')

create_pdf_report('/workspace/report.pdf')
```

### Step 3: Execute the Code

Call the tool:
```
execute_code_sandbox
  code: <your PDF generation code from Step 2>
  language: python
```
 
 ### Step 3b: Fallback - Shell-Based Execution
 
 If `execute_code_sandbox` fails repeatedly (e.g., "unknown error", timeouts), use this fallback:
 
 **1. Create the Python script via heredoc:**
 ```bash
 cat > /tmp/generate_pdf.py << 'EOF'
 # Your PDF generation code here
 from fpdf import FPDF
 import os
 
 workspace = os.environ.get('WORKSPACE', os.getcwd())
 output_path = os.path.join(workspace, 'checklist.pdf')
 
 pdf = FPDF()
 pdf.add_page()
 pdf.set_font('Arial', 'B', 16)
 pdf.cell(0, 10, 'Checklist', 0, 1, 'C')
 pdf.output(output_path)
 print(f'ARTIFACT_PATH:{output_path}')
 EOF
 ```
 
 **2. Install required library if needed:**
 ```bash
 pip install fpdf2 --quiet 2>/dev/null || pip install fpdf2
 ```
 
 **3. Execute the script:**
 ```bash
 python /tmp/generate_pdf.py
 ```
 
 **4. Verify the output file was created:**
 ```bash
 ls -lh $(pwd)/*.pdf 2>/dev/null || ls -lh /workspace/*.pdf 2>/dev/null
 ```
 
 **Important for fallback**: When using shell execution, ensure the script outputs the ARTIFACT_PATH so the file can be downloaded. Adjust the path based on where the file was actually created.

### Step 4: Verify Output

After execution, verify the PDF was created:

**Option A - Use read_file:**
```
read_file
  filetype: pdf
  file_path: /workspace/output.pdf
```

**Option B - Use shell inspection:**
```
run_shell
  command: ls -lh /workspace/*.pdf
```

Check that:
- File exists and has reasonable size (>1KB)
- No error messages in execution output
- ARTIFACT_PATH was correctly output for download
 
 **If using fallback execution**, check multiple possible locations:
 ```bash
 run_shell
   command: find . -name "*.pdf" -type f -exec ls -lh {} \; 2>/dev/null
 ```

### Step 5: Handle Common Issues

| Issue | Solution |
|-------|----------|
| Library not installed | Add `!pip install fpdf2` or `!pip install reportlab` at code start |
| Font errors | Use standard fonts (Arial, Helvetica, Courier) |
| File not found | Ensure path is `/workspace/filename.pdf` |
| Empty PDF | Check that `pdf.output()` or `doc.build()` is called |
| Encoding issues | Use ASCII text or handle unicode properly |
| `execute_code_sandbox` fails | Switch to shell-based fallback (Step 3b): create script with heredoc, run with `python` |
| Wrong workspace path | Use dynamic detection: `os.getcwd()` or `$(pwd)` instead of hardcoded `/workspace/` |
| Permission errors | Write to current directory or `/tmp/` then move file |

## Best Practices

1. **Keep it simple first** - Start with basic text, add tables/graphics later
2. **Use ARTIFACT_PATH prefix** - Ensures the file is downloadable
3. **Test incrementally** - Generate a minimal PDF before adding complexity
4. **Include error handling** - Wrap file operations in try/except blocks
5. **Set appropriate margins** - Prevent content from being cut off
 6. **Have a fallback ready** - If `execute_code_sandbox` fails after 2-3 attempts, switch to shell-based execution immediately
 7. **Detect workspace dynamically** - Don't assume `/workspace/`; use `os.getcwd()` or environment variables

## Template for Quick Start

```python
# Install library if needed
# !pip install fpdf2

from fpdf import FPDF
 import os

pdf = FPDF()
pdf.add_page()
pdf.set_font('Arial', 'B', 16)
pdf.cell(0, 10, 'Your Title Here', 0, 1, 'C')
pdf.ln(10)
pdf.set_font('Arial', '', 12)
pdf.multi_cell(0, 8, 'Your content here...')
 workspace = os.environ.get('WORKSPACE', os.getcwd())
 output_path = os.path.join(workspace, 'output.pdf')
 pdf.output(output_path)
 print(f'ARTIFACT_PATH:{output_path}')
```
 
 **Shell fallback template:**
 ```bash
 # Create script
 cat > /tmp/pdf_gen.py << 'PYEOF'
 from fpdf import FPDF
 import os
 pdf = FPDF()
 pdf.add_page()
 pdf.set_font('Arial', 'B', 16)
 pdf.cell(0, 10, 'Title', 0, 1, 'C')
 workspace = os.environ.get('WORKSPACE', os.getcwd())
 pdf.output(os.path.join(workspace, 'output.pdf'))
 print(f'ARTIFACT_PATH:{workspace}/output.pdf')
 PYEOF
 # Run it
 pip install fpdf2 -q 2>/dev/null; python /tmp/pdf_gen.py
 ```

## Related Tools

- `execute_code_sandbox` - Run Python code for PDF generation
- `read_file` - Verify PDF content (type: pdf)
- `run_shell` - Check file existence and size
- `create_file` - Alternative for simple text files if PDF not required
