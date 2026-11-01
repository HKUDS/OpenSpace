---
name: pdftotext-fallback
description: Recover PDF text extraction when read_file returns binary data by using pdftotext via shell
---

# pdftotext Fallback for PDF Text Extraction

## When to Use

Apply this pattern when:
- `read_file` on a PDF returns binary/image data instead of readable text
- Python-based PDF extraction (PyMuPDF, pdfplumber, etc.) in `execute_code_sandbox` fails or returns garbled content
- You need reliable text extraction from PDF files as a recovery strategy

## Why This Works

The `pdftotext` utility (from poppler-utils) is a mature, command-line tool that handles many PDF edge cases that confuse Python libraries or the `read_file` tool. It's pre-installed on most Linux systems and provides consistent, reliable text extraction.

## Steps

### Step 1: Detect the Failure

Recognize extraction failure when:
- `read_file` returns binary content, image data, or garbled text
- Error messages indicate encoding issues or unsupported formats
- Python PDF libraries in sandbox fail with import errors or extraction failures

### Step 2: Use pdftotext via run_shell

**Extract to stdout** (recommended for quick extraction):

```bash
pdftotext /path/to/file.pdf -
```

The `-` argument outputs directly to stdout for easy capture in your tool response.

**Example:**
```bash
run_shell("pdftotext document.pdf -")
```

### Step 3: Handle Complex PDFs

**Preserve layout** (maintains original formatting):
```bash
pdftotext -layout /path/to/file.pdf -
```

**Extract to a file** (for large PDFs):
```bash
pdftotext /path/to/file.pdf /path/to/output.txt
```
Then read the output file with `read_file`.

**Handle encoded text:**
```bash
pdftotext -enc UTF-8 /path/to/file.pdf -
```

### Step 4: Verify and Continue

- Check the extracted text for completeness
- If text is still garbled, the PDF may be image-based (scanned) - consider OCR tools
- Proceed with your task using the extracted text

## Code Examples

**Basic extraction:**
```bash
# Simple text extraction
text = run_shell("pdftotext document.pdf -")
```

**With error handling:**
```bash
# Try extraction, check for success
result = run_shell("pdftotext document.pdf - && echo 'SUCCESS' || echo 'FAILED'")
```

**Multi-page PDF with layout:**
```bash
# Preserve tables and formatting
text = run_shell("pdftotext -layout document.pdf -")
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| `pdftotext: command not found` | Install poppler-utils: `apt-get install poppler-utils` |
| Garbled/special characters | Try `-enc UTF-8` or `-enc ASCII7` |
| Missing formatting | Use `-layout` flag |
| Scanned PDF (no text) | Requires OCR (e.g., tesseract), not text extraction |
| Large PDF timeout | Extract to file instead of stdout |

## Limitations

- Does not work on image-only (scanned) PDFs without embedded text
- May lose complex formatting, tables, or embedded objects
- Requires poppler-utils to be installed (common on Linux, may need installation on other systems)

## Related Tools

- `pdfinfo`: Get PDF metadata (pages, size, etc.)
- `pdftoppm`: Convert PDF pages to images
- `tesseract`: OCR for scanned PDFs