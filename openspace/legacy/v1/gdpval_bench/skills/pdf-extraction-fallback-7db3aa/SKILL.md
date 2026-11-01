---
name: pdf-extraction-fallback-7db3aa
description: Resilient multi-tier PDF extraction with sequential fallback strategies when initial reading fails
---

# PDF Extraction Fallback Strategy

## Purpose

When processing complex documents (tax forms, legal documents, scanned materials), PDF extraction often fails on the first attempt. This skill provides a systematic fallback approach that tries multiple extraction methods in sequence until one succeeds.

## When to Use

- Initial PDF reading tools return errors or empty content
- Document appears to be scanned/image-based rather than text-based
- Previous extraction attempts produced incomplete or garbled output
- Working with forms, tables, or structured documents that need reliable extraction

## Fallback Sequence

### Tier 1: Shell-Based Extraction (pdftotext)

Start with command-line tools that often handle edge cases better:

```bash
# Extract text maintaining layout
pdftotext -layout input.pdf output.txt

# Extract raw text (faster, less formatting)
pdftotext input.pdf output.txt

# Extract specific page range
pdftotext -f 1 -l 3 input.pdf output.txt
```

Check if output contains meaningful content before proceeding.

### Tier 2: Python-Based Parsing

If shell tools fail, use Python libraries with different extraction approaches:

```python
# Using PyPDF2 for basic text extraction
import PyPDF2
with open('document.pdf', 'rb') as f:
    reader = PyPDF2.PdfReader(f)
    text = ''.join(page.extract_text() for page in reader.pages)

# Using pdfplumber for tables and structured content
import pdfplumber
with pdfplumber.open('document.pdf') as pdf:
    for page in pdf.pages:
        text = page.extract_text()
        tables = page.extract_tables()

# Using pypdf for newer PDF features
from pypdf import PdfReader
reader = PdfReader('document.pdf')
text = ''.join(page.extract_text() for page in reader.pages)
```

### Tier 3: OCR Tools (for Scanned Documents)

If the PDF contains images or scanned content, use OCR:

```bash
# Using tesseract via command line
tesseract input.pdf output --psm 6

# Using Python with pytesseract
import pytesseract
from pdf2image import convert_from_path

images = convert_from_path('document.pdf')
text = ''.join(pytesseract.image_to_string(img) for img in images)
```

## Implementation Pattern

```python
def extract_pdf_resilient(pdf_path):
    """Try multiple extraction methods until one succeeds."""
    
    # Tier 1: Shell extraction
    result = run_shell(f'pdftotext -layout "{pdf_path}" -')
    if result.stdout and len(result.stdout.strip()) > 100:
        return result.stdout, 'pdftotext'
    
    # Tier 2: Python libraries
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            text = ''.join(page.extract_text() or '' for page in pdf.pages)
        if text.strip():
            return text, 'pdfplumber'
    except Exception:
        pass
    
    # Tier 3: OCR fallback
    try:
        from pdf2image import convert_from_path
        import pytesseract
        images = convert_from_path(pdf_path)
        text = ''.join(pytesseract.image_to_string(img) for img in images)
        if text.strip():
            return text, 'tesseract-ocr'
    except Exception:
        pass
    
    raise ExtractionError("All extraction methods failed")
```

## Decision Criteria

| Indicator | Action |
|-----------|--------|
| Empty output | Proceed to next tier |
| Garbled/special characters | Try next tier |
| Partial content | Accept if meets minimum threshold |
| Tool not available | Skip to next tier |
| Format-specific errors | Try alternative library |

## Best Practices

1. **Validate each attempt** - Check output length and quality before accepting
2. **Log which method succeeded** - Track which tier worked for future reference
3. **Set minimum content thresholds** - Don't accept trivial results (e.g., <50 chars)
4. **Combine methods if needed** - Some documents need multiple approaches for different sections
5. **Preserve original file** - Never modify the source PDF during extraction attempts

## Error Handling

- Catch exceptions at each tier, don't fail immediately
- Log detailed error messages for debugging
- Continue to next tier even if current tier partially succeeds but produces poor quality
- After all tiers fail, provide clear summary of what was tried

## Output Quality Check

Before declaring extraction complete:

```python
def validate_extraction(text):
    if not text or len(text.strip()) < 50:
        return False
    if text.count('') > len(text) * 0.1:  # Too many replacement chars
        return False
    if len(set(text)) < 10:  # Too little character variety
        return False
    return True
```