---
name: pdf-extraction-fallbacks-7d54a9
description: Multi-fallback PDF extraction with sequential approaches and early failure detection
---

# PDF Extraction Fallbacks

This skill provides a robust workflow for extracting text from PDFs when source documents may fail to download or extract due to JavaScript protection, CORS restrictions, or encoding issues.

## When to Use

- Downloading regulatory documents from government/agency websites
- Extracting text from PDFs that may be JavaScript-protected
- Handling PDFs with potential CORS or encoding issues
- When you need reliable text extraction with guaranteed fallbacks

## Core Workflow

### Step 1: Download with Validation

```bash
# Download PDF and immediately validate
curl -L -o document.pdf "URL_HERE"

# Check file size (reject if < 1KB - likely error page)
FILE_SIZE=$(stat -f%z document.pdf 2>/dev/null || stat -c%s document.pdf 2>/dev/null)
if [ "$FILE_SIZE" -lt 1024 ]; then
    echo "FAIL: File too small ($FILE_SIZE bytes) - likely error page"
    # Log the actual content to diagnose
    head -c 500 document.pdf
    exit 1
fi

# Check for HTML/error content instead of PDF
if head -c 500 document.pdf | grep -qi "<!DOCTYPE html\|<html\|error\|access denied"; then
    echo "FAIL: Downloaded HTML/error page instead of PDF"
    exit 1
fi
```

### Step 2: Sequential Extraction Fallbacks

Try extraction methods in order of reliability:

**Fallback 1: pdftotext (poppler-utils)**
```bash
if command -v pdftotext &> /dev/null; then
    pdftotext -layout document.pdf output.txt 2>/dev/null
    if [ -s output.txt ] && [ $(wc -c < output.txt) -gt 100 ]; then
        echo "SUCCESS: pdftotext extraction"
        exit 0
    fi
fi
```

**Fallback 2: PyMuPDF (fitz)**
```python
import fitz  # PyMuPDF

def extract_with_pymupdf(pdf_path):
    try:
        doc = fitz.open(pdf_path)
        text = ""
        for page in doc:
            text += page.get_text()
        doc.close()
        if len(text.strip()) > 100:
            return text
        return None
    except Exception as e:
        print(f"PyMuPDF failed: {e}")
        return None
```

**Fallback 3: pdfplumber**
```python
import pdfplumber

def extract_with_pdfplumber(pdf_path):
    try:
        text = ""
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
        if len(text.strip()) > 100:
            return text
        return None
    except Exception as e:
        print(f"pdfplumber failed: {e}")
        return None
```

### Step 3: Content Sanity Check

After any extraction, validate the output:

```python
def validate_extraction(text, min_chars=100, min_words=20):
    """Check if extracted text is meaningful content."""
    if not text:
        return False, "Empty extraction"
    
    text = text.strip()
    if len(text) < min_chars:
        return False, f"Too short: {len(text)} chars"
    
    words = text.split()
    if len(words) < min_words:
        return False, f"Too few words: {len(words)}"
    
    # Check for error patterns
    error_patterns = [
        "access denied", "permission denied", "javascript required",
        "failed to load", "cannot display", "corrupted"
    ]
    text_lower = text.lower()
    for pattern in error_patterns:
        if pattern in text_lower[:500]:  # Check beginning
            return False, f"Error pattern detected: {pattern}"
    
    return True, "Valid extraction"
```

## Complete Python Implementation

```python
import requests
import subprocess
import os
from pathlib import Path

def robust_pdf_extraction(url, output_path="extracted.txt", temp_pdf="temp.pdf"):
    """
    Multi-fallback PDF extraction with validation at each step.
    Returns (success, text_or_error)
    """
    
    # Step 1: Download with validation
    try:
        response = requests.get(url, timeout=30, headers={
            'User-Agent': 'Mozilla/5.0 (compatible; DocumentExtractor/1.0)'
        })
        response.raise_for_status()
    except Exception as e:
        return False, f"Download failed: {e}"
    
    # Check response size
    if len(response.content) < 1024:
        return False, f"Downloaded content too small: {len(response.content)} bytes"
    
    # Check for HTML error pages
    if response.content[:500].lower().find(b'<html') != -1:
        return False, "Downloaded HTML page instead of PDF"
    
    # Save PDF
    Path(temp_pdf).write_bytes(response.content)
    
    # Step 2: Try extraction methods in order
    extraction_methods = [
        ("pdftotext", extract_pdftotext),
        ("PyMuPDF", extract_pymupdf),
        ("pdfplumber", extract_pdfplumber),
    ]
    
    for method_name, extract_func in extraction_methods:
        try:
            text = extract_func(temp_pdf)
            valid, msg = validate_extraction(text)
            if valid:
                Path(output_path).write_text(text)
                return True, text
            print(f"{method_name}: {msg}")
        except Exception as e:
            print(f"{method_name} exception: {e}")
    
    # Cleanup
    os.remove(temp_pdf)
    return False, "All extraction methods failed"


def extract_pdftotext(pdf_path):
    result = subprocess.run(
        ["pdftotext", "-layout", pdf_path, "-"],
        capture_output=True, text=True, timeout=60
    )
    return result.stdout if result.returncode == 0 else None


def extract_pymupdf(pdf_path):
    import fitz
    doc = fitz.open(pdf_path)
    text = "".join(page.get_text() for page in doc)
    doc.close()
    return text


def extract_pdfplumber(pdf_path):
    import pdfplumber
    text = ""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"
    return text
```

## Failure Detection Checklist

| Check | Threshold | Action |
|-------|-----------|--------|
| File size | < 1KB | Reject - likely error page |
| Content type | HTML detected | Reject - not a PDF |
| Extracted text | < 100 chars | Try next fallback |
| Word count | < 20 words | Try next fallback |
| Error patterns | Found in first 500 chars | Reject extraction |

## Best Practices

1. **Always validate immediately after download** - Don't wait until extraction to discover the PDF is invalid
2. **Log each fallback attempt** - Helps diagnose which sites need special handling
3. **Set reasonable timeouts** - PDF processing can hang on corrupted files
4. **Clean up temp files** - Especially important in automated workflows
5. **Preserve original PDFs** - Keep copies for debugging extraction failures
6. **Check for JavaScript protection** - Some sites require headless browser rendering first

## Common Failure Modes

| Symptom | Likely Cause | Solution |
|---------|--------------|----------|
| 92-byte "PDF" | JavaScript error page | Use headless browser (Playwright/Selenium) |
| HTML content | Redirect to login/error | Check authentication requirements |
| Empty extraction | Scan-only PDF | Use OCR (pytesseract) as additional fallback |
| Garbled text | Encoding issues | Try different PDF libraries |

## Integration Example

```python
# For regulatory document retrieval workflows
def retrieve_regulatory_doc(doc_url, output_dir="docs"):
    success, result = robust_pdf_extraction(
        doc_url,
        output_path=f"{output_dir}/content.txt",
        temp_pdf=f"{output_dir}/temp.pdf"
    )
    
    if success:
        print(f"✓ Extracted {len(result)} characters")
        return result
    else:
        print(f"✗ Failed: {result}")
        # Log URL for manual review
        with open("failed_urls.log", "a") as f:
            f.write(f"{doc_url}: {result}\n")
        return None
```