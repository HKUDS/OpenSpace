---
name: pdf-extraction-fallback
description: Multi-stage fallback strategy for PDF/document extraction using sequential tool alternatives
---

# PDF Extraction Fallback Strategy

When processing documents (especially PDFs), initial extraction attempts may fail due to formatting, encryption, or tool limitations. This skill provides a systematic fallback approach that tries multiple extraction methods before declaring failure.

## Core Principle

**Never declare completion after a single tool failure.** Instead, iterate through a hierarchy of extraction methods, each with different capabilities and limitations.

## Fallback Hierarchy

Attempt extraction methods in this order:

### Stage 1: Direct PDF Reading
Try native PDF libraries first (fastest, preserves structure):
```python
import PyPDF2
from pypdf import PdfReader

def extract_with_pypdf(pdf_path):
    reader = PdfReader(pdf_path)
    text = ""
    for page in reader.pages:
        text += page.extract_text() or ""
    return text
```

### Stage 2: Shell-based Extraction (pdftotext)
If Stage 1 fails, use system tools:
```bash
# Install if needed: apt-get install poppler-utils
pdftotext -layout input.pdf output.txt
pdftotext -raw input.pdf output.txt  # Alternative layout
```

```python
import subprocess

def extract_with_pdftotext(pdf_path):
    result = subprocess.run(
        ['pdftotext', '-layout', pdf_path, '-'],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        return result.stdout
    raise Exception("pdftotext failed")
```

### Stage 3: Alternative Python Parsers
Try different Python libraries with varying capabilities:
```python
# pdfplumber - better for tables
import pdfplumber
def extract_with_pdfplumber(pdf_path):
    text = ""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text += page.extract_text() or ""
    return text

# pdfminer - handles complex layouts
from pdfminer.high_level import extract_text
def extract_with_pdfminer(pdf_path):
    return extract_text(pdf_path)
```

### Stage 4: OCR Fallback
For scanned images or when text extraction fails:
```bash
# Using tesseract
convert input.pdf output-%d.png  # Convert to images first
tesseract output-0.png result --psm 6
```

```python
# Using pytesseract
from pdf2image import convert_from_path
import pytesseract

def extract_with_ocr(pdf_path):
    images = convert_from_path(pdf_path, dpi=300)
    text = ""
    for image in images:
        text += pytesseract.image_to_string(image)
    return text
```

## Implementation Pattern

```python
def robust_pdf_extraction(pdf_path):
    """Try multiple extraction methods until one succeeds."""
    
    extraction_methods = [
        ("PyPDF2", extract_with_pypdf),
        ("pdftotext", extract_with_pdftotext),
        ("pdfplumber", extract_with_pdfplumber),
        ("pdfminer", extract_with_pdfminer),
        ("OCR", extract_with_ocr),
    ]
    
    errors = []
    for method_name, method_func in extraction_methods:
        try:
            print(f"Trying {method_name}...")
            text = method_func(pdf_path)
            if text and text.strip():
                print(f"Success with {method_name}")
                return text
            else:
                errors.append(f"{method_name}: empty result")
        except Exception as e:
            errors.append(f"{method_name}: {str(e)}")
            print(f"{method_name} failed: {e}")
            continue
    
    # All methods failed
    raise Exception(f"All extraction methods failed:\n" + "\n".join(errors))
```

## Success Criteria

A method is considered successful when:
1. **No exceptions** are raised during execution
2. **Non-empty text** is returned (text.strip() has content)
3. **Content quality** meets task requirements (check for expected keywords/patterns)

## Best Practices

1. **Log each attempt** - Record which methods were tried and why they failed
2. **Validate output** - Check extracted text contains expected content markers
3. **Graceful degradation** - Proceed with partial data if full extraction isn't possible
4. **Cache successful method** - Remember which method worked for similar files
5. **Set timeouts** - Prevent OCR or complex parsing from hanging indefinitely

## Common Failure Modes

| Symptom | Likely Cause | Best Fallback |
|---------|--------------|---------------|
| Empty pages | Image-based PDF | OCR (Stage 4) |
| Garbled text | Encoding issues | pdftotext (Stage 2) |
| Missing tables | Simple parser | pdfplumber (Stage 3) |
| Permission errors | Encrypted PDF | Check password/permissions first |
| Layout lost | Complex formatting | pdftotext -layout or pdfplumber |

## When to Use

- Processing unknown/untrusted PDF sources
- Batch processing diverse document types
- Critical tasks where extraction failure is not acceptable
- Documents with mixed content (text + images + tables)