---
name: docx-shell-workaround
description: Handle docx files using shell-based XML extraction and python-docx via run_shell when standard tools fail
---

# DOCX Shell Workaround

## When to Use

Use this skill when:
- `read_file` cannot extract content from .docx files
- `execute_code_sandbox` encounters failures when creating or modifying docx files
- You need a reliable fallback for docx file manipulation

## Reading DOCX Files

### Step 1: Extract document.xml using unzip

Use `run_shell` to unzip the .docx file (which is a ZIP archive) and extract the main document XML:

```bash
unzip -p input.docx word/document.xml > document.xml
```

### Step 2: Parse XML with ElementTree via run_shell

Extract text content by parsing the XML. Use `run_shell` to execute Python code:

```bash
python3 << 'EOF'
import xml.etree.ElementTree as ET

tree = ET.parse('document.xml')
root = tree.getroot()
namespace = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}

text_content = []
for elem in root.iter():
    if elem.tag.endswith('t'):
        if elem.text:
            text_content.append(elem.text)

text = ''.join(text_content)
print(text)
EOF
```

### Step 3: Optional - More robust XML parsing

For better text extraction that handles paragraphs:

```bash
python3 << 'EOF'
import xml.etree.ElementTree as ET

tree = ET.parse('document.xml')
root = tree.getroot()
ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}

paragraphs = []
for p in root.findall('.//w:p', ns):
    para_text = []
    for t in p.findall('.//w:t', ns):
        if t.text:
            para_text.append(t.text)
    if para_text:
        paragraphs.append(''.join(para_text))

for para in paragraphs:
    print(para)
EOF
```

## Creating DOCX Files

### Use python-docx via run_shell (NOT execute_code_sandbox)

When `execute_code_sandbox` fails for docx operations, use `run_shell` instead:

```bash
python3 << 'EOF'
from docx import Document
from docx.shared import Inches, Pt
from docx.enum.text import WD_ALIGN_PARAGRAPH

doc = Document()

# Add heading
doc.add_heading('Document Title', 0)

# Add paragraph
doc.add_paragraph('Your content here.')

# Add section with heading
doc.add_heading('Section Title', level=1)
doc.add_paragraph('Section content with multiple paragraphs.')

# Add table if needed
table = doc.add_table(rows=3, cols=3)
table.style = 'Table Grid'

# Save document
doc.save('output.docx')
print("Document created successfully")
EOF
```

### Example: Creating a structured business document

```bash
python3 << 'EOF'
from docx import Document
from docx.shared import Pt

doc = Document()

# Title
title = doc.add_heading('Business Strategy Memo', 0)
title.alignment = 1  # Center

# Executive Summary
doc.add_heading('Executive Summary', level=1)
doc.add_paragraph('Brief overview of key points and recommendations.')

# Market Overview
doc.add_heading('Market Overview', level=1)
doc.add_paragraph('Analysis of current market conditions and trends.')

# Recommendations
doc.add_heading('Recommendations', level=1)
doc.add_paragraph('Actionable recommendations based on analysis.')

doc.save('Strategy_Memo.docx')
EOF
```

## Full Workflow Example

Complete example showing extraction and creation:

```bash
# Step 1: Extract content from existing docx
unzip -p existing.docx word/document.xml > doc.xml

# Step 2: Parse and transform content
python3 << 'EOF'
import xml.etree.ElementTree as ET

tree = ET.parse('doc.xml')
root = tree.getroot()
ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}

content = []
for p in root.findall('.//w:p', ns):
    para_text = []
    for t in p.findall('.//w:t', ns):
        if t.text:
            para_text.append(t.text)
    if para_text:
        content.append(''.join(para_text))

# Write extracted content to file for reference
with open('extracted.txt', 'w') as f:
    for line in content:
        f.write(line + '\n')
EOF

# Step 3: Create new docx with modified content
python3 << 'EOF'
from docx import Document

doc = Document()
doc.add_heading('Updated Document', 0)

with open('extracted.txt', 'r') as f:
    for line in f:
        if line.strip():
            doc.add_paragraph(line.strip())

doc.save('updated.docx')
EOF
```

## Key Points

1. **Always use `run_shell`** - Not `execute_code_sandbox` for docx operations
2. **DOCX is a ZIP archive** - Contains XML files including word/document.xml
3. **Use `unzip -p`** - The `-p` flag outputs to stdout, useful for piping
4. **Handle XML namespaces** - Word XML uses the `w:` namespace prefix
5. **Install python-docx if needed** - Run `pip install python-docx` in the shell if not available

## Troubleshooting

If python-docx is not installed:
```bash
pip install python-docx
```

If unzip is not available:
```bash
# Use Python's zipfile module instead
python3 -c "import zipfile; z=zipfile.ZipFile('file.docx'); print(z.read('word/document.xml').decode('utf-8', errors='ignore'))"
```

If XML parsing fails:
- Check the namespace URI matches your document version
- Use `.endswith('t')` instead of full namespace matching for flexibility
- Handle encoding issues with `errors='ignore'` parameter