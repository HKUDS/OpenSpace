---
name: pdf-download-extract-fallback
description: Multi-step PDF download and text extraction with progressive fallback strategies
---

# PDF Download and Extract with Fallback

This skill provides a robust workflow for acquiring PDF documents from web sources and extracting their text content, with multiple fallback mechanisms to handle various failure modes.

## Overview

When working with PDFs from web sources, encounters with JavaScript redirects, corrupted files, missing tools, or inaccessible content are common. This workflow ensures maximum success rate through progressive fallback strategies.

## Step-by-Step Instructions

### Step 1: Download PDF with Browser User-Agent

Many PDF hosting sites use JavaScript-based redirects or block automated requests. Use curl with a realistic browser user-agent:

```bash
curl -L -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36" -o output.pdf "URL_HERE"
```

Key flags:
- `-L`: Follow redirects
- `-A`: Set user-agent header to mimic a real browser
- `-o`: Specify output filename

### Step 2: Verify File Type Before Parsing

Always validate the downloaded file is actually a PDF before attempting extraction:

```bash
file output.pdf
```

Expected output should contain "PDF document". If not:
- The URL may have redirected to an HTML error page
- The file may be corrupted
- Access may be blocked

### Step 3: Primary Extraction with pdftotext

First attempt extraction using the standard `pdftotext` utility (part of poppler-utils):

```bash
pdftotext output.pdf output.txt
```

If `pdftotext` is not available, install it:
```bash
# Debian/Ubuntu
apt-get update && apt-get install -y poppler-utils

# macOS
brew install poppler

# RHEL/CentOS
yum install -y poppler-utils
```

### Step 4: Fallback to PyMuPDF (fitz)

If `pdftotext` fails or produces poor results, use Python's PyMuPDF library:

```python
import fitz  # PyMuPDF

doc = fitz.open("output.pdf")
text = ""
for page in doc:
    text += page.get_text()
doc.close()

with open("output.txt", "w") as f:
    f.write(text)
```

Install if needed:
```bash
pip install pymupdf
```

### Step 5: Graceful Degradation to Domain Knowledge

If the PDF cannot be accessed or extracted after all attempts:

1. Document the failure mode (network issue, corrupted file, access denied, etc.)
2. Extract any partial content that was successfully retrieved
3. Supplement missing content from established domain knowledge
4. Clearly mark which portions are from source vs. generated from knowledge
5. Provide citations for any claimed requirements or specifications

Example degradation note:
```
NOTE: Source document [URL] was inaccessible due to [reason]. 
Content below combines partial extraction with established domain knowledge 
for [topic]. Verify against official sources when available.
```

## Complete Workflow Script

```bash
#!/bin/bash
# pdf-extract-workflow.sh
# pdf-extract-workflow.sh - Handles both URL downloads and local files

INPUT="$1"
OUTPUT_PDF="downloaded.pdf"
OUTPUT_TXT="extracted.txt"

if [[ "$INPUT" =~ ^https?:// ]]; then
    # Mode A: URL download
    PDF_URL="$INPUT"
    echo "Downloading PDF from URL..."
    curl -L -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36" -o "$OUTPUT_PDF" "$PDF_URL"
else
    # Mode B: Local file
    if [ ! -f "$INPUT" ]; then
        echo "ERROR: Local file not found: $INPUT"
        exit 1
    fi
    OUTPUT_PDF="$INPUT"
    echo "Using local file: $INPUT"
fi

# Step 2: Verify file type
echo "Verifying file type..."
if ! file "$OUTPUT_PDF" | grep -q "PDF document"; then
    echo "WARNING: Downloaded file is not a valid PDF"
    echo "Attempting fallback extraction anyway..."
fi

# Step 3: Try pdftotext
echo "Attempting pdftotext extraction..."
if command -v pdftotext &> /dev/null; then
    if pdftotext "$OUTPUT_PDF" "$OUTPUT_TXT" 2>/dev/null; then
        echo "Extraction successful with pdftotext"
        exit 0
    fi
fi

# Step 4: Fallback to PyMuPDF
echo "Falling back to PyMuPDF..."
python3 << 'PYTHON_SCRIPT'
import fitz
import sys

try:
    doc = fitz.open("downloaded.pdf")
    text = ""
    for page in doc:
        text += page.get_text()
    doc.close()
    with open("extracted.txt", "w") as f:
        f.write(text)
    print("Extraction successful with PyMuPDF")
    sys.exit(0)
except Exception as e:
    print(f"PyMuPDF failed: {e}")
    sys.exit(1)
PYTHON_SCRIPT

# Step 5: Handle complete failure
# Step 5: Handle complete failure (domain knowledge fallback)
if [ $? -ne 0 ]; then
    echo "ERROR: All extraction methods failed."
    echo "ACTION: Generate content from domain knowledge and clearly mark source limitations."
    echo "Document the failure and proceed with knowledge-based content generation."
fi
```

## Best Practices

1. **Always verify before parsing**: Never assume a downloaded file is valid
2. **Pre-check tools before extraction**: Verify pdftotext or PyMuPDF availability before starting
3. **Local files skip download**: When PDF is already on disk, begin at file validation step
2. **Preserve original PDF**: Keep the downloaded file for debugging if needed
3. **Log each step**: Document which method succeeded for future reference
4. **Check extraction quality**: Verify extracted text is readable and complete
5. **Cite source limitations**: When using fallback knowledge, clearly indicate source gaps

## Common Failure Modes

| Symptom | Cause | Solution |
|---------|-------|----------|
| HTML content in file | URL redirected to error page or wrong file type | Check HTTP status, verify file with `file` command |
| Empty extraction | Password-protected or scanned PDF | Try OCR tools or request accessible version |
| Garbled text | Encoding issues | Try PyMuPDF with different extraction mode |
| Curl blocked | Anti-bot measures | Add more headers, use delay between requests |
| **pdftotext not found** | Tool not installed | Run `apt-get install poppler-utils` or use PyMuPDF fallback |
| **PyMuPDF import failed** | Package not installed | Run `pip install pymupdf` |
| **File not found (local)** | Incorrect path or file not accessible | Verify file path, check permissions, confirm file was uploaded |

## When to Use This Skill

- **Mode A (URL download)**: Downloading documents from web sources
- **Mode B (Local file)**: Processing PDFs already on disk or uploaded as reference files
- Extracting content from technical manuals or handbooks
- Processing PDFs in automated pipelines where reliability matters
- Any situation where PDF access may be unreliable or restricted

## Local File Processing Workflow

When you already have the PDF file locally (not from a URL):

### Step L1: Verify File Exists

```bash
if [ ! -f "your_file.pdf" ]; then
    echo "ERROR: File not found"
    echo "ACTION: Verify the file path and that the file was successfully uploaded"
    exit 1
fi
```

### Step L2: Validate File Type

```bash
file your_file.pdf
```

Expected output should contain "PDF document". If not, the file may be corrupted or mislabeled.

### Step L3: Proceed to Extraction

After validation, skip directly to **Step 3: Primary Extraction with pdftotext** in the main workflow.

## Tool Availability Pre-Check

Before attempting any PDF extraction, verify your environment has the necessary tools:

```bash
# Check pdftotext availability
command -v pdftotext && echo "pdftotext: AVAILABLE" || echo "pdftotext: NOT FOUND - install poppler-utils"

# Check PyMuPDF availability  
python3 -c "import fitz; print('PyMuPDF: AVAILABLE')" 2>/dev/null || echo "PyMuPDF: NOT FOUND - run: pip install pymupdf"
```

**Installation commands if tools are missing:**

```bash
# Install pdftotext (poppler-utils)
apt-get update && apt-get install -y poppler-utils  # Debian/Ubuntu
yum install -y poppler-utils                        # RHEL/CentOS
brew install poppler                                # macOS

# Install PyMuPDF
pip install pymupdf
```

---
name: pdf-download-extract-fallback
description: Multi-step PDF download and text extraction with progressive fallback strategies
---
This skill provides a robust workflow for acquiring PDF documents from web sources **or processing locally-available files** and extracting their text content, with multiple fallback mechanisms to handle various failure modes.
## Entry Point: Determine Your Starting Point

**Before beginning, identify your scenario:**

| Scenario | Start Here | Skip |
|----------|-----------|------|
| PDF already on local disk | Step 2 (Verify File Type) | Step 1 (Download) |
| PDF at a web URL | Step 1 (Download) | None |

## Overview

When working with PDFs from web sources **or local files**, encounters with corrupted files, missing tools, or inaccessible content are common. This workflow ensures maximum success rate through progressive fallback strategies.
## Mode A: Web URL Download

### Step 1: Download PDF with Browser User-Agent

Many PDF hosting sites use JavaScript-based redirects or block automated requests. Use curl with a realistic browser user-agent:

```bash
curl -L -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36" -o output.pdf "URL_HERE"
```

Key flags:
- `-L`: Follow redirects
- `-A`: Set user-agent header to mimic a real browser
- `-o`: Specify output filename

## Mode B: Local File Processing

If you already have the PDF file locally, **skip Step 1** and begin here:

Always validate the file is actually a PDF before attempting extraction:
## Complete Workflow Script (Handles Both URL and Local File)
