---
name: pdf-text-extraction
description: Extract text from PDFs using shell tools when read_file fails
---

# PDF Text Extraction (Fallback Method)

## When to Use This Skill

Use this skill when `read_file` with `filetype='pdf'`:
- Returns binary image data instead of text
- Produces errors or incomplete content
- Fails to extract structured data reliably

The built-in PDF handler is unreliable for structured text extraction. Shell-based tools provide more robust alternatives.

## Available Methods

### Method 1: pdftotext (Recommended)

```bash
# Extract text from PDF to stdout
pdftotext /path/to/file.pdf -

# Extract text to a file
pdftotext /path/to/file.pdf output.txt

# Preserve layout (maintains spacing/structure)
pdftotext -layout /path/to/file.pdf output.txt
```

**Usage in agent:**
```bash
run_shell command="pdftotext -layout /path/to/document.pdf -"
```

### Method 2: pdfinfo (Metadata)

```bash
# Get PDF metadata (pages, author, creation date, etc.)
pdfinfo /path/to/file.pdf
```

**Usage in agent:**
```bash
run_shell command="pdfinfo /path/to/document.pdf"
```

### Method 3: Python with PyMuPDF (fitz)

```python
import fitz  # PyMuPDF

doc = fitz.open("/path/to/file.pdf")
text = ""
for page in doc:
    text += page.get_text()
doc.close()
print(text)
```

**Usage in agent:**
```bash
run_shell command="python3 -c \"import fitz; doc=fitz.open('file.pdf'); print(''.join(p.get_text() for p in doc))\""
```

### Method 4: Python with pdfplumber (Tables)

```python
import pdfplumber

with pdfplumber.open("/path/to/file.pdf") as pdf:
    for page in pdf.pages:
        text = page.extract_text()
        tables = page.extract_tables()  # For tabular data
```

**Usage in agent:**
```bash
run_shell command="python3 -c \"import pdfplumber; pdf=pdfplumber.open('file.pdf'); print(''.join(p.extract_text() or '' for p in pdf.pages))\""
```

## Workflow

1. **Try pdftotext first** - Fastest and most reliable for plain text
   ```bash
   run_shell command="pdftotext -layout /path/to/file.pdf -"
   ```

2. **If pdftotext unavailable, check for Python libraries**
   ```bash
   run_shell command="python3 -c \"import fitz; print('PyMuPDF available')\""
   ```

3. **For table/structured data, use pdfplumber**
   ```bash
   run_shell command="python3 -c \"import pdfplumber; ...\""
   ```

4. **Verify extraction succeeded** - Check output contains readable text, not binary data

## Installation Notes

If tools are not available:
```bash
# Ubuntu/Debian
apt-get install poppler-utils  # pdftotext, pdfinfo

# Install Python libraries
pip install pymupdf pdfplumber
```

## Anti-Patterns to Avoid

- Do NOT rely solely on `read_file` with `filetype='pdf'` for critical text extraction
- Do NOT assume PDF text is in any particular order - verify extracted content
- Do NOT use image-based extraction unless the PDF is scanned (use OCR instead)

## Example: Complete Extraction Pattern

```bash
# Step 1: Try pdftotext
RESULT=$(pdftotext -layout /path/to/form.pdf - 2>/dev/null)

# Step 2: Verify we got text, not error
if [ -z "$RESULT" ]; then
    # Fallback to Python
    RESULT=$(python3 -c "import fitz; doc=fitz.open('/path/to/form.pdf'); print(''.join(p.get_text() for p in doc))")
fi

# Step 3: Use extracted text
echo "$RESULT"
```