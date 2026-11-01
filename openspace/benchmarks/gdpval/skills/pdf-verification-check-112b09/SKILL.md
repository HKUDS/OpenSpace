---
name: pdf-verification-check-112b09
description: Verify generated PDF files using PyPDF2 to check page counts and integrity
---

# PDF Verification Check

After generating PDF files, always programmatically verify their structure and integrity before declaring task completion. This catches formatting errors, incorrect page counts, and corrupted files early.

## When to Use

Use this skill whenever your task involves:
- Generating PDF documents
- Creating PDFs with specific page count requirements
- Producing multi-part PDF outputs (e.g., listings + maps)

## Verification Steps

### 1. Install PyPDF2 (if not available)

```bash
pip install PyPDF2
```

### 2. Verify PDF Page Count and Integrity

Create a verification script or run inline Python:

```python
from PyPDF2 import PdfReader
import os

def verify_pdf(pdf_path, expected_pages=None):
    """Verify PDF file exists, is readable, and has expected page count."""
    try:
        # Check file exists
        if not os.path.exists(pdf_path):
            return False, f"File not found: {pdf_path}"
        
        # Check file size (non-empty)
        file_size = os.path.getsize(pdf_path)
        if file_size == 0:
            return False, f"Empty file: {pdf_path}"
        
        # Open and verify PDF structure
        reader = PdfReader(pdf_path)
        actual_pages = len(reader.pages)
        
        # Check page count if expected
        if expected_pages is not None and actual_pages != expected_pages:
            return False, f"Expected {expected_pages} pages, got {actual_pages}"
        
        return True, f"Valid PDF with {actual_pages} page(s)"
        
    except Exception as e:
        return False, f"PDF verification failed: {str(e)}"

# Example usage
pdf_files = [
    ("listings.pdf", 2),
    ("map.pdf", 1),
]

all_valid = True
for pdf_path, expected in pdf_files:
    valid, message = verify_pdf(pdf_path, expected)
    print(f"{pdf_path}: {'✓' if valid else '✗'} {message}")
    if not valid:
        all_valid = False

if not all_valid:
    raise Exception("PDF verification failed - review and regenerate")
```

### 3. Use run_shell for Verification

Execute the verification using `run_shell`:

```bash
python -c "
from PyPDF2 import PdfReader
import sys

pdf_path = 'output.pdf'
expected = 2

try:
    reader = PdfReader(pdf_path)
    actual = len(reader.pages)
    if actual != expected:
        print(f'ERROR: Expected {expected} pages, got {actual}')
        sys.exit(1)
    print(f'OK: {pdf_path} has {actual} page(s)')
except Exception as e:
    print(f'ERROR: {e}')
    sys.exit(1)
"
```

### 4. Verify Before Completion

**Critical:** Only mark your task as complete after ALL PDF verification checks pass. If verification fails:

1. Examine the error message
2. Regenerate the PDF with corrected formatting
3. Re-run verification
4. Repeat until all checks pass

## Common Issues Caught

- Wrong page counts (e.g., content spilling to extra pages)
- Empty or corrupted PDF files
- PDFs that fail to open properly
- Missing expected sections due to formatting errors

## Example Workflow

```python
# 1. Generate PDF
generate_pdf("listings.pdf", content)

# 2. Verify before completion
valid, msg = verify_pdf("listings.pdf", expected_pages=2)
if not valid:
    # Fix and regenerate
    regenerate_pdf("listings.pdf", corrected_content)
    valid, msg = verify_pdf("listings.pdf", expected_pages=2)

# 3. Only complete if verification passes
assert valid, f"Cannot complete: {msg}"
```