---
name: pdf-extraction-fallbacks
description: Multi-fallback PDF/text extraction with early failure detection and sequential tool fallbacks
---

# PDF Extraction with Multi-Fallback Strategy

## Purpose

When extracting text from PDFs (especially regulatory documents, handbooks, or protected content), single-method approaches often fail due to JavaScript protection, CORS restrictions, encoding issues, or corrupted downloads. This skill provides a robust multi-fallback workflow that detects failures early and tries sequential extraction methods.

## Core Pattern

1. **Download with validation** - Check file size and content sanity immediately
2. **Sequential extraction attempts** - Try multiple tools in order of reliability
3. **Early failure detection** - Don't proceed with obviously corrupt files
4. **Document fallback path** - Log which method succeeded for future reference

## Step-by-Step Instructions

### Step 1: Download and Validate

Before attempting extraction, validate the downloaded file:

```bash
# Download the PDF
curl -L -o document.pdf "$URL"

# Check file size (reject if < 1KB - likely error page)
FILE_SIZE=$(stat -f%z document.pdf 2>/dev/null || stat -c%s document.pdf 2>/dev/null)
if [ "$FILE_SIZE" -lt 1024 ]; then
    echo "ERROR: File too small ($FILE_SIZE bytes) - likely not a valid PDF"
    # Check if it's an HTML error page
    head -c 200 document.pdf | grep -i "<html\|<!doctype\|error\|access denied" && \
        echo "Detected HTML error page instead of PDF"
    exit 1
fi

# Check PDF magic bytes
HEAD_BYTES=$(head -c 4 document.pdf)
if [ "$HEAD_BYTES" != "%PDF" ]; then
    echo "ERROR: File does not start with PDF magic bytes"
    head -c 100 document.pdf
    exit 1
fi
```

### Step 2: Primary Extraction (pdftotext)

```bash
# Try pdftotext first (fastest, most reliable for simple PDFs)
if command -v pdftotext &> /dev/null; then
    pdftotext -layout document.pdf output.txt 2>/dev/null
    if [ -s output.txt ]; then
        WORD_COUNT=$(wc -w < output.txt)
        if [ "$WORD_COUNT" -gt 50 ]; then
            echo "SUCCESS: pdftotext extracted $WORD_COUNT words"
            exit 0
        fi
    fi
fi
```

### Step 3: Fallback 1 (PyMuPDF/fitz)

```python
# Try PyMuPDF - handles more complex PDFs
import fitz  # pymupdf

try:
    doc = fitz.open("document.pdf")
    text = ""
    for page in doc:
        text += page.get_text()
    
    if len(text.strip()) > 500:  # Sanity check
        with open("output.txt", "w") as f:
            f.write(text)
        print(f"SUCCESS: PyMuPDF extracted {len(text)} characters")
    else:
        print("WARNING: PyMuPDF extraction too short, trying next method")
except Exception as e:
    print(f"PyMuPDF failed: {e}")
```

### Step 4: Fallback 2 (pdfplumber)

```python
# Try pdfplumber - better for tables and structured content
import pdfplumber

try:
    text = ""
    with pdfplumber.open("document.pdf") as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"
    
    if len(text.strip()) > 500:
        with open("output.txt", "w") as f:
            f.write(text)
        print(f"SUCCESS: pdfplumber extracted {len(text)} characters")
    else:
        print("WARNING: pdfplumber extraction too short")
except Exception as e:
    print(f"pdfplumber failed: {e}")
```

### Step 5: Handle JavaScript-Protected Pages

If all methods fail, the PDF may be JavaScript-protected:

```python
# Check for JavaScript in PDF
import fitz

doc = fitz.open("document.pdf")
has_js = False
for page in doc:
    if page.get_java_script():
        has_js = True
        break

if has_js:
    print("WARNING: PDF contains JavaScript - may be protected")
    # Try rendering pages as images and OCR (requires additional tools)
    # Or try alternative download source
```

### Step 6: Alternative Sources

If the primary URL fails:
- Try alternative domains (e.g., `.gov` mirrors, archive.org)
- Check if the document is available via API
- Look for HTML version of the same content
- Search for the document title + "pdf" to find mirrors

## Decision Tree

```
Download PDF
    │
    ├─→ File < 1KB? → REJECT (likely error page)
    ├─→ No %PDF header? → REJECT (not a PDF)
    │
    └─→ Valid PDF
         │
         ├─→ pdftotext → >50 words? → SUCCESS
         │              └─→ Try next
         │
         ├─→ PyMuPDF → >500 chars? → SUCCESS
         │             └─→ Try next
         │
         ├─→ pdfplumber → >500 chars? → SUCCESS
         │                 └─→ Try next
         │
         └─→ All failed → Check for JS protection, try alternative sources
```

## Common Failure Modes

| Symptom | Likely Cause | Solution |
|---------|-------------|----------|
| File < 100 bytes | JavaScript error page | Check CORS, try different user-agent |
| File ~1-5KB | HTML error/warning page | Parse HTML for actual PDF link |
| pdftotext returns empty | Encrypted/protected PDF | Try PyMuPDF with password handling |
| Garbled text output | Encoding issue | Try pdfplumber, specify encoding |
| Extraction very short | Images-only PDF | Need OCR (tesseract) |

## Example Complete Workflow Script

Save as `extract_pdf_robust.sh`:

```bash
#!/bin/bash
set -e

URL="$1"
OUTPUT="${2:-output.txt}"
TEMP_PDF="temp_download.pdf"

echo "Downloading from: $URL"
curl -L -A "Mozilla/5.0" -o "$TEMP_PDF" "$URL"

# Validate
SIZE=$(stat -c%s "$TEMP_PDF" 2>/dev/null || stat -f%z "$TEMP_PDF")
echo "Downloaded: $SIZE bytes"

if [ "$SIZE" -lt 1024 ]; then
    echo "ERROR: File too small - checking content..."
    head -200 "$TEMP_PDF"
    exit 1
fi

if ! head -c 4 "$TEMP_PDF" | grep -q "%PDF"; then
    echo "ERROR: Not a valid PDF file"
    head -100 "$TEMP_PDF"
    exit 1
fi

# Try extraction methods
python3 << 'PYTHON'
import sys
import fitz
import pdfplumber

pdf_path = "temp_download.pdf"
output_path = "output.txt"

# Method 1: PyMuPDF
try:
    doc = fitz.open(pdf_path)
    text = "".join(page.get_text() for page in doc)
    if len(text.strip()) > 500:
        with open(output_path, "w") as f:
            f.write(text)
        print(f"PyMuPDF: {len(text)} chars")
        sys.exit(0)
except Exception as e:
    print(f"PyMuPDF failed: {e}")

# Method 2: pdfplumber
try:
    text = ""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            txt = page.extract_text()
            if txt:
                text += txt + "\n"
    if len(text.strip()) > 500:
        with open(output_path, "w") as f:
            f.write(text)
        print(f"pdfplumber: {len(text)} chars")
        sys.exit(0)
except Exception as e:
    print(f"pdfplumber failed: {e}")

print("All extraction methods failed")
sys.exit(1)
PYTHON
```

## Best Practices

1. **Always validate** - Never assume a download succeeded
2. **Log the path taken** - Record which method worked for debugging
3. **Set reasonable thresholds** - 50 words / 500 chars minimum for "success"
4. **Keep raw PDF** - Don't delete the original until extraction is confirmed
5. **Retry with variations** - Different user-agents, referer headers, or mirrors

## Dependencies

- `curl` - For downloading
- `poppler-utils` (pdftotext) - Optional, fast extraction
- `PyMuPDF` (`fitz`) - `pip install pymupdf`
- `pdfplumber` - `pip install pdfplumber`