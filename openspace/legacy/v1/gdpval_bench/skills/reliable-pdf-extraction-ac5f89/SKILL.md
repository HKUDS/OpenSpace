---
name: reliable-pdf-extraction-ac5f89
description: Extract PDF text content using shell tools or Python libraries when read_file PDF handler fails
---

# Reliable PDF Text Extraction

## Problem

The `read_file` tool with `filetype='pdf'` can be unreliable for PDF text extraction. It may:
- Return binary image data instead of text
- Fail with errors on certain PDF structures
- Lose formatting or structured content

## Solution

Use `run_shell` with dedicated PDF extraction tools instead of relying on `read_file` for PDFs.

## Methods

### Method 1: pdftotext (Recommended)

```bash
pdftotext input.pdf output.txt
```

Or to extract to stdout:
```bash
pdftotext input.pdf -
```

With layout preservation:
```bash
pdftotext -layout input.pdf output.txt
```

### Method 2: pdfinfo (Metadata)

```bash
pdfinfo input.pdf
```

Useful for checking page count, dimensions, and PDF properties before extraction.

### Method 3: Python with PyMuPDF (fitz)

```python
import fitz  # PyMuPDF

doc = fitz.open("input.pdf")
text = ""
for page in doc:
    text += page.get_text()
doc.close()
```

### Method 4: Python with pdfplumber (Tables)

```python
import pdfplumber

with pdfplumber.open("input.pdf") as pdf:
    for page in pdf.pages:
        text = page.extract_text()
        tables = page.extract_tables()
```

## Workflow

1. **Check PDF exists and is readable:**
   ```bash
   pdfinfo input.pdf 2>/dev/null || echo "PDF not accessible"
   ```

2. **Extract text using pdftotext:**
   ```bash
   pdftotext -layout input.pdf - > extracted_text.txt
   ```

3. **If pdftotext fails, try Python fallback:**
   ```python
   import fitz
   doc = fitz.open("input.pdf")
   for i, page in enumerate(doc):
       print(f"--- Page {i+1} ---")
       print(page.get_text())
   doc.close()
   ```

4. **Verify extraction succeeded:**
   - Check output is non-empty
   - Verify text is readable (not binary/garbled)
   - Confirm expected content is present

## When to Use

| Tool | Best For |
|------|----------|
| `pdftotext` | Fast, simple text extraction |
| `pdftotext -layout` | Preserving spacing/formatting |
| `PyMuPDF` | Complex PDFs, programmatic access |
| `pdfplumber` | Tables and structured data |

## Example Integration

```bash
# In your agent workflow, prefer this pattern:
result = run_shell(command="pdftotext document.pdf -", timeout=30)
if result.stdout and len(result.stdout.strip()) > 0:
    content = result.stdout
else:
    # Fallback to Python
    content = execute_python_to_extract_pdf("document.pdf")
```

## Notes

- Install tools if needed: `apt-get install poppler-utils` (for pdftotext/pdfinfo)
- Python libraries: `pip install pymupdf pdfplumber`
- Some PDFs are image-scanned and require OCR (Tesseract) instead
- Always validate extracted content before proceeding with downstream tasks