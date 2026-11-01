---
name: reliable-pdf-extraction
description: Use shell commands or Python libraries to extract PDF text when read_file PDF handler fails
---

# Reliable PDF Text Extraction

## Problem

The `read_file` tool with `filetype='pdf'` often returns binary image data, errors, or unusable output when attempting to extract text from PDF documents. This makes it unreliable for structured data extraction tasks.

## Solution

Use `run_shell` with command-line tools (`pdftotext`, `pdfinfo`) or `execute_code_sandbox` with Python libraries (PyMuPDF, pdfplumber) to extract PDF text content reliably.

## Methods

### Method 1: pdftotext (Recommended for simple extraction)

```bash
# Extract all text to stdout
pdftotext input.pdf -

# Or extract to file
pdftotext input.pdf output.txt
cat output.txt
```

### Method 2: pdfinfo (For metadata)

```bash
pdfinfo input.pdf
```

### Method 3: Python with PyMuPDF (fitz)

```python
import fitz  # PyMuPDF

doc = fitz.open("input.pdf")
text = ""
for page in doc:
    text += page.get_text()
print(text)
doc.close()
```

### Method 4: Python with pdfplumber (Better for tables/structured data)

```python
import pdfplumber

with pdfplumber.open("input.pdf") as pdf:
    for page in pdf.pages:
        text = page.extract_text()
        print(text)
        # For tables:
        # tables = page.extract_tables()
```

## Workflow

1. **Attempt** `read_file` with `filetype='pdf'` first (in case it works)

2. **Check output** - If you receive:
   - Binary/garbage data
   - Error messages
   - Empty or truncated content
   - Image data instead of text

3. **Fall back** to one of the extraction methods above:
   - Use `pdftotext` via `run_shell` for quick text extraction
   - Use `pdfplumber` via `execute_code_sandbox` for structured data/tables
   - Use `PyMuPDF` for complex layouts or when you need more control

4. **Process** the extracted text for your task

## Example Usage

```python
# Via run_shell
result = run_shell(command="pdftotext document.pdf -")
pdf_text = result.stdout

# Via execute_code_sandbox
code = """
import pdfplumber
with pdfplumber.open("/path/to/document.pdf") as pdf:
    for page in pdf.pages:
        print(page.extract_text())
"""
result = execute_code_sandbox(code=code)
pdf_text = result.stdout
```

## Tips

- **pdftotext** is fastest and most reliable for plain text extraction
- **pdfplumber** excels at extracting tables and preserving layout
- **PyMuPDF** offers the most control for complex PDF structures
- Always check if the PDF is scanned/image-based (may need OCR tools like `tesseract`)
- Some PDFs have copy protection that may prevent text extraction