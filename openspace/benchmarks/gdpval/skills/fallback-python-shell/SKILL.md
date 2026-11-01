---
name: fallback-python-shell
description: Use run_shell with Python heredoc when execute_code_sandbox or read_file fail
---

# Shell-Based Python Fallback

## When to Use

Use this fallback pattern when:
- `execute_code_sandbox` returns 'unknown error'
- `read_file` returns 'unknown error' for supported formats
- You need to process documents, analyze data, or read files programmatically

## Core Technique

Run Python code through `run_shell` using a heredoc. This bypasses sandbox execution issues while maintaining Python's full capabilities for file I/O and data processing.

### Basic Pattern

```bash
python3 << 'EOF'
# Your Python code here
import json
import os

# Example: Read and process a file
with open('/path/to/file.txt', 'r') as f:
    content = f.read()
    print(content)
EOF
```

### Key Syntax Points

1. Use `python3 << 'EOF'` (quoted EOF prevents variable expansion in the heredoc)
2. Indent Python code starting from column 0 (no extra indentation from shell)
3. End with `EOF` on its own line with no leading/trailing whitespace
4. Print results to stdout to capture them in the tool response

## Common Use Cases

### Reading Files (Fallback for read_file)

```bash
python3 << 'EOF'
import json

# Read text file
with open('document.txt', 'r') as f:
    content = f.read()
    print(content)

# Read JSON file
with open('data.json', 'r') as f:
    data = json.load(f)
    print(json.dumps(data, indent=2))
EOF
```

### Processing Excel/CSV Files

```bash
python3 << 'EOF'
import pandas as pd

# Read Excel file
df = pd.read_excel('data.xlsx', sheet_name='Sheet1')
print(df.to_string())
print(f"Shape: {df.shape}")

# Read CSV
df = pd.read_csv('data.csv')
print(df.head(10))
EOF
```

### Reading PDF Files

```bash
python3 << 'EOF'
import fitz  # PyMuPDF

doc = fitz.open('document.pdf')
for page_num in range(len(doc)):
    page = doc[page_num]
    text = page.get_text()
    print(f"=== Page {page_num + 1} ===")
    print(text)
doc.close()
EOF
```

### Reading Word Documents

```bash
python3 << 'EOF'
from docx import Document

doc = Document('document.docx')
for para in doc.paragraphs:
    print(para.text)
EOF
```

### Data Analysis

```bash
python3 << 'EOF'
import pandas as pd
import numpy as np

df = pd.read_csv('data.csv')

# Basic statistics
print(f"Shape: {df.shape}")
print(f"Columns: {list(df.columns)}")
print(df.describe())

# Filter and aggregate
result = df.groupby('category').agg({'value': 'sum'})
print(result)
EOF
```

## Best Practices

1. **Error Handling**: Wrap file operations in try/except blocks
   ```bash
   python3 << 'EOF'
   try:
       with open('file.txt', 'r') as f:
           content = f.read()
           print(content)
   except FileNotFoundError:
       print("ERROR: File not found")
   except Exception as e:
       print(f"ERROR: {e}")
   EOF
   ```

2. **Large Output**: For large files, process in chunks or print summaries
   ```bash
   python3 << 'EOF'
   with open('large_file.csv', 'r') as f:
       for i, line in enumerate(f):
           if i < 10:
               print(line.strip())
           else:
               print("... truncated ...")
               break
   EOF
   ```

3. **Working Directory**: Remember run_shell executes in the current working directory. Use absolute paths or ensure you're in the right directory.

4. **Multiple Steps**: Chain related operations in a single heredoc rather than multiple calls
   ```bash
   python3 << 'EOF'
   # Do all related work in one call
   with open('input.json') as f:
       data = json.load(f)
   
   processed = [transform(x) for x in data]
   
   with open('output.json', 'w') as f:
       json.dump(processed, f)
   
   print("Processing complete")
   EOF
   ```

## Troubleshooting

- **Module not found**: Some packages may not be available. Stick to standard library or commonly pre-installed packages (pandas, numpy are often available).
- **Permission errors**: Ensure files aren't in protected directories.
- **Character encoding**: Specify encoding explicitly: `open('file.txt', 'r', encoding='utf-8')`
- **Very long code**: Split into multiple heredocs or write to a temporary .py file first.