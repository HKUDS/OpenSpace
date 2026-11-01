---
name: local-pdf-extraction
description: Extract text from local PDFs using pdftotext or PyMuPDF via run_shell
---

# Local PDF Extraction Workflow

Use this skill when you need to extract text from PDF files that exist locally on the filesystem, and `read_file` returns binary data instead of readable text.

## When to Use

- PDF files exist in the local workspace or known directories
- `read_file` on PDFs returns binary/garbled data instead of text
- You need to process PDF content for analysis, summarization, or data extraction

## Step-by-Step Instructions

### Step 1: Locate PDF Files

First, list directory contents to find all PDF files:

```bash
ls -la *.pdf
# or for recursive search
find . -name "*.pdf" -type f
```

### Step 2: Extract PDFs to Text

Choose one of these methods based on available tools:

#### Method A: Using pdftotext (poppler-utils)

```bash
# Extract single PDF
pdftotext input.pdf output.txt

# Batch extract all PDFs in directory
for pdf in *.pdf; do
    pdftotext "$pdf" "${pdf%.pdf}.txt"
done
```

#### Method B: Using PyMuPDF (fitz) via Python

```bash
python3 << 'EOF'
import fitz  # PyMuPDF
import glob
import os

for pdf_path in glob.glob("*.pdf"):
    doc = fitz.open(pdf_path)
    text = ""
    for page in doc:
        text += page.get_text()
    
    txt_path = pdf_path.replace(".pdf", ".txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"Extracted: {pdf_path} -> {txt_path}")
EOF
```

### Step 3: Read Extracted Text Files

Once extracted, use `read_file` to read the `.txt` files:

```python
# Now you can read the text files normally
content = read_file(filetype="txt", file_path="document.txt")
```

### Step 4: Process Content

Proceed with your analysis, summarization, or data extraction on the text content.

## Complete Workflow Example

```bash
# Step 1: Find PDFs
ls -la *.pdf

# Step 2: Extract all PDFs to text
for pdf in *.pdf; do
    pdftotext "$pdf" "${pdf%.pdf}.txt"
done

# Step 3: Verify extraction
ls -la *.txt
```

Or as a Python script via `run_shell`:

```bash
python3 << 'SCRIPT'
import fitz, glob
for pdf in glob.glob("*.pdf"):
    doc = fitz.open(pdf)
    text = "".join(page.get_text() for page in doc)
    with open(pdf.replace(".pdf", ".txt"), "w") as f:
        f.write(text)
    print(f"Done: {pdf}")
SCRIPT
```

## Troubleshooting

- **pdftotext not found**: Install with `apt-get install poppler-utils` or use PyMuPDF method
- **Empty text output**: PDF may be image-based; consider OCR tools like `pdftoppm` + `tesseract`
- **Encoding issues**: Ensure output files use UTF-8 encoding

## Key Takeaways

1. Never use `read_file` directly on PDFs for text extraction
2. Always list directory first to confirm PDF locations
3. Use `run_shell` with pdftotext or PyMuPDF for reliable extraction
4. Batch process when multiple PDFs exist
5. Read the resulting `.txt` files for further processing