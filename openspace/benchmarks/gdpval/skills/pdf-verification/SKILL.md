---
name: pdf-verification
description: Verify PDF page counts and file integrity programmatically using PyPDF2 after generation
---

# PDF Verification Skill

## Purpose

After generating PDF files, always verify page counts and file integrity programmatically before declaring task completion. This catches formatting issues, empty pages, and corrupted files early.

## When to Use

- After any PDF generation task
- When specific page counts are required
- Before marking a PDF-related task as complete

## Verification Steps

### 1. Install PyPDF2 (if needed)

```bash
pip install PyPDF2
```

### 2. Verify Page Count and Integrity

Use `run_shell` to execute a Python verification script:

```bash
python -c "
from PyPDF2 import PdfReader
import sys

def verify_pdf(filepath, expected_pages=None):
    try:
        reader = PdfReader(filepath)
        actual_pages = len(reader.pages)
        print(f'✓ {filepath}: {actual_pages} pages')
        
        if expected_pages and actual_pages != expected_pages:
            print(f'✗ Page count mismatch: expected {expected_pages}, got {actual_pages}')
            return False
        
        # Check file is not empty/corrupted
        if actual_pages == 0:
            print(f'✗ Empty PDF: {filepath}')
            return False
        
        return True
    except Exception as e:
        print(f'✗ Error reading {filepath}: {e}')
        return False

# Verify each PDF
results = []
results.append(verify_pdf('output.pdf', expected_pages=2))
print(f'All checks passed: {all(results)}')
sys.exit(0 if all(results) else 1)
"
```

### 3. Verify Multiple PDFs

For tasks with multiple PDFs, verify each one:

```bash
python -c "
from PyPDF2 import PdfReader

pdfs = {
    'listings.pdf': 2,
    'map.pdf': 1,
    'summary.pdf': 1
}

all_passed = True
for filepath, expected in pdfs.items():
    try:
        reader = PdfReader(filepath)
        actual = len(reader.pages)
        status = '✓' if actual == expected else '✗'
        print(f'{status} {filepath}: {actual}/{expected} pages')
        if actual != expected:
            all_passed = False
    except Exception as e:
        print(f'✗ {filepath}: {e}')
        all_passed = False

print(f'Verification: {\"PASSED\" if all_passed else \"FAILED\"}')
"
```

### 4. Check File Size (Optional)

Add file size validation to catch empty or near-empty files:

```bash
python -c "
import os
from PyPDF2 import PdfReader

filepath = 'output.pdf'
min_size = 1000  # minimum bytes

file_size = os.path.getsize(filepath)
if file_size < min_size:
    print(f'✗ File too small: {file_size} bytes')
else:
    reader = PdfReader(filepath)
    print(f'✓ {filepath}: {len(reader.pages)} pages, {file_size} bytes')
"
```

## Best Practices

1. **Verify immediately after generation** - Don't wait until the end of the task
2. **Check all required PDFs** - Verify each file meets specifications
3. **Fail fast** - If verification fails, regenerate before proceeding
4. **Log results** - Print verification results for debugging
5. **Set reasonable minimums** - Use file size checks to catch empty outputs

## Common Issues Caught

- Wrong page counts (extra blank pages, missing content)
- Corrupted or unreadable PDF files
- Empty PDFs (0 pages)
- Files that appear generated but contain no actual content

## Example Task Completion Check

```bash
# Final verification before marking task complete
python -c "
from PyPDF2 import PdfReader
import sys

required = {'listings.pdf': 2, 'map.pdf': 1}
passed = True

for f, pages in required.items():
    try:
        actual = len(PdfReader(f).pages)
        if actual != pages:
            print(f'FAIL: {f} has {actual} pages, expected {pages}')
            passed = False
    except Exception as e:
        print(f'FAIL: Cannot read {f}: {e}')
        passed = False

if passed:
    print('SUCCESS: All PDFs verified')
    sys.exit(0)
else:
    sys.exit(1)
"
```