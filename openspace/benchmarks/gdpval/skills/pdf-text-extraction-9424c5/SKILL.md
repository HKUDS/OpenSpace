---
name: pdf-text-extraction-9424c5
description: Extract text from PDF files using pdftotext when read_file returns binary data
---

# PDF Text Extraction via pdftotext

## Problem

When using `read_file` on PDF documents, the function may return binary image data or garbled content instead of readable text. This occurs because PDFs can contain scanned images or complex binary structures that `read_file` cannot properly parse as text.

## Solution

Use the `pdftotext` command-line utility via `run_shell` to extract clean text content from PDF files.

## Steps

### 1. Verify PDF file exists

```python
import os

pdf_path = "path/to/document.pdf"
if not os.path.exists(pdf_path):
    raise FileNotFoundError(f"PDF not found: {pdf_path}")
```

### 2. Extract text using pdftotext

```python
from tools import run_shell

# Extract text to stdout
result = run_shell(command=f"pdftotext '{pdf_path}' -", timeout=60)
pdf_text = result.stdout

# Alternative: extract to a temporary file
temp_txt = "/tmp/extracted.txt"
run_shell(command=f"pdftotext '{pdf_path}' '{temp_txt}'", timeout=60)
with open(temp_txt, 'r') as f:
    pdf_text = f.read()
```

### 3. Handle parameter naming carefully

When calling `read_file`, be aware of the parameter name:
- Use `filetype="pdf"` (not `file_type`)
- Some tool implementations may use different parameter names

```python
# Correct parameter usage
content = read_file(file_path="doc.pdf", filetype="pdf")

# If this returns binary/garbled data, fall back to pdftotext
```

## Common pdftotext Options

| Option | Description |
|--------|-------------|
| `-` | Output to stdout |
| `-layout` | Maintain original layout |
| `-f <n>` | Start from page n |
| `-l <n>` | End at page n |
| `-q` | Quiet mode |

Example with options:
```python
result = run_shell(command=f"pdftotext -layout -q '{pdf_path}' -", timeout=60)
```

## Error Handling

```python
from tools import run_shell

def extract_pdf_text(pdf_path):
    """Extract text from PDF using pdftotext with error handling."""
    import os
    
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"PDF not found: {pdf_path}")
    
    result = run_shell(command=f"pdftotext '{pdf_path}' -", timeout=60)
    
    if result.returncode != 0:
        raise RuntimeError(f"pdftotext failed: {result.stderr}")
    
    return result.stdout.strip()
```

## When to Use This Pattern

- `read_file` returns binary data, garbled text, or image content for a PDF
- You need searchable/processable text from PDF documents
- The PDF contains text (not just scanned images - for those, consider OCR tools)

## Prerequisites

- `pdftotext` must be installed (part of `poppler-utils` on Debian/Ubuntu, `poppler` on macOS via Homebrew)
- Verify availability: `run_shell(command="which pdftotext")`