---
name: robust-pdf-extraction
description: Multi-method PDF extraction with sequential fallback and OCR for scanned documents
---

# Robust PDF Extraction Workflow

This skill provides a systematic approach to extracting text from PDF files, handling both text-based and scanned/image-based documents through progressive fallback methods.

## When to Use

- Processing PDFs of unknown or mixed types (text vs. scanned images)
- Critical document processing where extraction failure is not acceptable
- Batch processing multiple PDFs with varying formats

## Workflow Steps

### Step 1: Verify File Accessibility

Before attempting extraction, confirm the PDF exists and is readable:

```bash
# Check file exists and get basic info
ls -la /path/to/document.pdf

# Or search for files if location uncertain
find /path -name "*.pdf" -type f 2>/dev/null
```

### Step 2: Attempt Primary Extraction (pdfplumber)

Start with pdfplumber for best text structure preservation:

```python
import pdfplumber

def extract_with_pdfplumber(pdf_path):
    text = ""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"
    return text.strip()
```

### Step 3: Fallback to Secondary Method (pypdfium2)

If pdfplumber returns empty or incomplete text:

```python
import pdfium2

def extract_with_pypdfium2(pdf_path):
    pdf = pdfium2.PdfDocument(pdf_path)
    text = ""
    for page in pdf:
        text_page = page.get_textpage()
        page_text = text_page.get_text_bounded()
        if page_text:
            text += page_text + "\n"
    return text.strip()
```

### Step 4: Fallback to Tertiary Method (pdftotext)

If pypdfium2 also fails, use command-line pdftotext:

```bash
pdftotext /path/to/document.pdf - 2>/dev/null
```

Or in Python:

```python
import subprocess

def extract_with_pdftotext(pdf_path):
    result = subprocess.run(
        ['pdftotext', pdf_path, '-'],
        capture_output=True,
        text=True
    )
    return result.stdout.strip()
```

### Step 5: Detect Scanned/Image-Based PDFs

After each extraction attempt, verify text was actually extracted:

```python
def is_meaningful_text(text, min_chars=50):
    """Check if extracted text is meaningful (not empty or just whitespace)"""
    if not text:
        return False
    # Remove whitespace and check length
    cleaned = ''.join(text.split())
    return len(cleaned) >= min_chars
```

### Step 6: OCR Fallback for Scanned Documents

If all text extraction methods return empty/insufficient text, the PDF is likely scanned. Use OCR:

```python
import pdf2image
import pytesseract
from PIL import Image

def extract_with_ocr(pdf_path, dpi=300):
    """Extract text from scanned PDFs using OCR"""
    text = ""
    images = pdf2image.convert_from_path(pdf_path, dpi=dpi)
    for image in images:
        page_text = pytesseract.image_to_string(image)
        text += page_text + "\n"
    return text.strip()
```

## Complete Workflow Function

```python
def robust_pdf_extract(pdf_path):
    """
    Extract text from PDF using progressive fallback methods.
    Returns (text, method_used) tuple.
    """
    methods = [
        ("pdfplumber", extract_with_pdfplumber),
        ("pypdfium2", extract_with_pypdfium2),
        ("pdftotext", extract_with_pdftotext),
    ]
    
    for method_name, extract_func in methods:
        try:
            text = extract_func(pdf_path)
            if is_meaningful_text(text):
                return text, method_name
        except Exception as e:
            print(f"{method_name} failed: {e}")
            continue
    
    # All text methods failed - try OCR
    try:
        text = extract_with_ocr(pdf_path)
        if is_meaningful_text(text):
            return text, "ocr"
    except Exception as e:
        print(f"OCR failed: {e}")
    
    return "", "failed"
```

## Dependencies

Install required packages:

```bash
pip install pdfplumber pypdfium2 pdf2image pytesseract pillow
# Also need system packages:
# apt-get install poppler-utils tesseract-ocr  # Debian/Ubuntu
# brew install poppler tesseract  # macOS
```

## Best Practices

1. **Log which method succeeded** - helps identify document types for future optimization
2. **Set reasonable character thresholds** - adjust `min_chars` based on expected document content
3. **Handle exceptions gracefully** - each method may fail for different reasons
4. **Consider DPI for OCR** - higher DPI (300+) improves accuracy but increases processing time
5. **Cache results** - if processing same PDFs repeatedly, store successful extraction method per file

## Troubleshooting

| Symptom | Likely Cause | Solution |
|---------|--------------|----------|
| All methods return empty | Scanned PDF | OCR fallback should handle this |
| pdfplumber fails with permission error | File locked or permissions issue | Check file permissions with `ls -la` |
| OCR returns gibberish | Low quality scan or wrong language | Increase DPI, specify language in pytesseract |
| pdftotext not found | Missing poppler-utils | Install system package |