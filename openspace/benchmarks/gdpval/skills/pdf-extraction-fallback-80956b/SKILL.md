---
name: pdf-extraction-fallback-80956b
description: Multi-fallback PDF download and text extraction with early failure detection
---

# PDF Extraction Fallback Workflow

This skill provides a robust, multi-layered approach to downloading and extracting text from PDF documents when sources may be protected, corrupted, or inaccessible via standard methods.

## When to Use

- Downloading regulatory documents, handbooks, or official PDFs from government/enterprise websites
- Sources that may have JavaScript protection, CORS restrictions, or dynamic content
- When initial PDF downloads produce suspiciously small files or error content
- Any scenario requiring reliable text extraction from potentially problematic PDF sources

## Core Workflow

### Step 1: Download with Validation

```bash
# Download PDF with size check
curl -L -o output.pdf "https://example.com/document.pdf"

# Early failure detection: check file size
file_size=$(stat -c%s output.pdf 2>/dev/null || stat -f%z output.pdf)

if [ "$file_size" -lt 1000 ]; then
    echo "WARNING: File size ($file_size bytes) suggests failed download or error page"
    # Check for HTML/JavaScript error content
    if head -c 500 output.pdf | grep -qi "<html\|<script\|error\|access denied"; then
        echo "FAILURE: File contains error message, not PDF content"
        rm output.pdf
        # Proceed to alternative download method
    fi
fi
```

### Step 2: Sequential Extraction Fallbacks

Try extraction methods in order, moving to next on failure:

#### Fallback 1: pdftotext (command-line)

```bash
if command -v pdftotext &> /dev/null; then
    pdftotext output.pdf output.txt
    if [ -s output.txt ] && [ $(wc -c < output.txt) -gt 100 ]; then
        echo "SUCCESS: pdftotext extraction"
        exit 0
    fi
fi
```

#### Fallback 2: PyMuPDF (fitz)

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

#### Fallback 3: pdfplumber

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

### Step 3: Content Sanity Validation

After any extraction method succeeds:

```python
def validate_extracted_text(text, min_length=100):
    """Validate extracted content is meaningful"""
    if not text or len(text.strip()) < min_length:
        return False
    
    # Check for common error patterns
    error_patterns = [
        "access denied", "permission denied", "error", 
        "javascript", "<html", "<script", "404", "403"
    ]
    text_lower = text.lower()[:500]  # Check first 500 chars
    for pattern in error_patterns:
        if pattern in text_lower:
            return False
    
    return True
```

## Complete Workflow Script

```python
#!/usr/bin/env python3
"""
Robust PDF extraction with multiple fallbacks
"""
import subprocess
import os
import sys

def download_pdf(url, output_path):
    """Download PDF with validation"""
    subprocess.run(["curl", "-L", "-o", output_path, url], check=True)
    
    # Validate download
    if not os.path.exists(output_path):
        return False
    
    file_size = os.path.getsize(output_path)
    if file_size < 1000:
        with open(output_path, 'r', errors='ignore') as f:
            content = f.read(500).lower()
            if any(x in content for x in ['<html', '<script', 'error', 'denied']):
                os.remove(output_path)
                return False
    return True

def extract_text(pdf_path):
    """Try multiple extraction methods"""
    
    # Method 1: pdftotext
    try:
        result = subprocess.run(
            ["pdftotext", pdf_path, "-"],
            capture_output=True, text=True, timeout=60
        )
        if result.stdout and len(result.stdout.strip()) > 100:
            return result.stdout
    except:
        pass
    
    # Method 2: PyMuPDF
    try:
        import fitz
        doc = fitz.open(pdf_path)
        text = "".join(page.get_text() for page in doc)
        doc.close()
        if len(text.strip()) > 100:
            return text
    except:
        pass
    
    # Method 3: pdfplumber
    try:
        import pdfplumber
        text = ""
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
        if len(text.strip()) > 100:
            return text
    except:
        pass
    
    return None

def main():
    url = sys.argv[1]
    pdf_path = "document.pdf"
    
    if not download_pdf(url, pdf_path):
        print("ERROR: Download failed or invalid content")
        sys.exit(1)
    
    text = extract_text(pdf_path)
    if text:
        with open("extracted.txt", "w") as f:
            f.write(text)
        print(f"SUCCESS: Extracted {len(text)} characters")
    else:
        print("ERROR: All extraction methods failed")
        sys.exit(1)

if __name__ == "__main__":
    main()
```

## Failure Documentation

For each failed attempt, log:

| Attempt | Method | Failure Reason | File Size | Content Preview |
|---------|--------|----------------|-----------|-----------------|
| 1 | Direct download | JavaScript error page | 92 bytes | `<!DOCTYPE html>...` |
| 2 | pdftotext | File not valid PDF | - | - |
| 3 | PyMuPDF | Encrypted/protected | - | - |
| 4 | pdfplumber | Success | 2.4 MB | "VA Handbook Chapter 1..." |

## Best Practices

1. **Always validate downloads immediately** - Don't assume a successful HTTP 200 means valid content
2. **Check file size thresholds** - Files <1KB are almost always error pages
3. **Scan for error patterns** - HTML tags, JavaScript, error messages indicate failed downloads
4. **Try multiple extractors** - Different PDFs work better with different libraries
5. **Set minimum content thresholds** - Extracted text <100 chars usually indicates failure
6. **Clean up failed artifacts** - Remove invalid files before retrying
7. **Document each failure** - Helps diagnose patterns in source protection mechanisms

## Dependencies

Install required tools:

```bash
# Command-line tool
apt-get install poppler-utils  # provides pdftotext

# Python libraries
pip install PyMuPDF pdfplumber
```

## Notes for Regulatory Documents

Government and regulatory websites often:
- Use JavaScript-based PDF viewers instead of direct links
- Implement session-based access requiring authentication
- Serve error pages with 200 status codes
- Have CORS restrictions on direct downloads

When encountering these, consider:
- Using browser automation (Selenium/Playwright) as an additional fallback
- Checking for alternative document repositories
- Looking for cached versions via search engines