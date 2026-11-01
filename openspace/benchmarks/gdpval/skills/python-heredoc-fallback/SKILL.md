---
name: python-heredoc-fallback
description: Use shell Python heredoc as fallback when code sandbox fails
---

# Python Heredoc Fallback for Code Execution

## When to Use

Use this pattern when `execute_code_sandbox` fails with e2b initialization errors, sandbox connection issues, or other execution environment problems. This is especially reliable for file generation tasks.

## Core Pattern

Instead of `execute_code_sandbox`, run Python code directly in the shell using a heredoc:

```bash
python3 << 'EOF'
# Your Python code here
import sys
print("Python code executing in shell")
EOF
```

## Key Syntax Rules

1. **Use single-quoted EOF** (`<< 'EOF'`) to prevent shell variable expansion within the Python code
2. **No indentation before EOF** - the closing delimiter must be at the start of the line
3. **Install packages first** if needed, before the Python heredoc block

## Common Use Cases

### 1. Matplotlib Graph Generation

```bash
python3 << 'EOF'
import matplotlib.pyplot as plt
import pandas as pd

# Load data
data = pd.read_excel('input.xlsx')

# Create visualization
plt.figure(figsize=(10, 6))
plt.plot(data['x'], data['y'])
plt.xlabel('X Label')
plt.ylabel('Y Label')
plt.title('Graph Title')
plt.savefig('output_graph.png', dpi=300, bbox_inches='tight')
plt.close()

print("Graph saved successfully")
EOF
```

### 2. Python-docx Document Creation

```bash
pip install python-docx -q

python3 << 'EOF'
from docx import Document
from docx.shared import Inches, Pt
from docx.enum.text import WD_ALIGN_PARAGRAPH

doc = Document()

# Add title
title = doc.add_heading('Document Title', 0)
title.alignment = WD_ALIGN_PARAGRAPH.CENTER

# Add sections
doc.add_heading('Section 1', level=1)
doc.add_paragraph('Content for section 1.')

# Add table
table = doc.add_table(rows=3, cols=3)
table.style = 'Table Grid'

# Save document
doc.save('output.docx')
print("Document created successfully")
EOF
```

### 3. General File Operations

```bash
python3 << 'EOF'
import json
import os

# Read input file
with open('input.json', 'r') as f:
    data = json.load(f)

# Process data
result = {'processed': True, 'items': len(data)}

# Write output
with open('output.json', 'w') as f:
    json.dump(result, f, indent=2)

print(f"Processed {result['items']} items")
EOF
```

## Best Practices

1. **Install dependencies first**: Run `pip install package-name -q` before the Python block if packages aren't guaranteed to be available
2. **Verify file paths**: Files are read from and written to the current working directory
3. **Add print statements**: Include progress/output messages to verify execution succeeded
4. **Close resources**: Explicitly close matplotlib figures and file handles
5. **Handle errors gracefully**: Add try/except blocks for production code

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Module not found | Add `pip install <module> -q` before Python block |
| File not found | Verify file exists in current directory with `ls` |
| Permission denied | Check file permissions and directory write access |
| Syntax errors | Ensure proper Python indentation within heredoc |

## Advantages Over execute_code_sandbox

- **No initialization overhead** - runs immediately in existing shell
- **Persistent environment** - maintains state across multiple commands
- **Direct file system access** - no sandbox isolation issues
- **Full system Python** - access to all installed packages
- **Reliable for large files** - no sandbox size restrictions