---
name: pdf-read-file-fallback
description: Extract text from PDFs using pdftotext when read_file returns binary data
---

# PDF Text Extraction Fallback

## When to Use

Use this pattern when `read_file` with `filetype="pdf"` returns binary image data instead of extractable text content. This commonly occurs with PDFs that contain scanned images or complex formatting.

## Steps

### 1. Attempt Primary Extraction

First, try using `read_file`:

```python
result = read_file(file_path="document.pdf", filetype="pdf")
```

**Important**: Use `filetype` (not `file_type`) - incorrect parameter naming will cause execution failures.

### 2. Detect Binary/Image Data

Check if the result contains unusable content:

```python
# Indicators of binary/image data:
# - Contains null bytes: '\x00'
# - Very short or empty
# - Contains image markers (PNG/JPEG headers)
# - Unreadable character sequences

if not result or len(result) < 50 or '\x00' in str(result):
    # Proceed to fallback
```

### 3. Use pdftotext via run_shell

Extract text using the `pdftotext` command-line tool:

```python
shell_result = run_shell(command="pdftotext -layout document.pdf -")
text_content = shell_result.stdout
```

The `-` flag outputs to stdout for easy capture. The `-layout` flag preserves original formatting.

### 4. Handle pdftotext Unavailable

If `pdftotext` is not installed, try Python-based extraction:

```python
result = execute_code_sandbox(code="""
import pdfplumber
text = ''
with pdfplumber.open('document.pdf') as pdf:
    for page in pdf.pages:
        extracted = page.extract_text()
        if extracted:
            text += extracted + '\\n'
print(text)
""")
```

## Complete Example

```python
file_path = "report.pdf"

# Primary attempt
result = read_file(file_path=file_path, filetype="pdf")

# Validate and fallback if needed
if not result or len(str(result)) < 100 or '\x00' in str(result):
    # Fallback to pdftotext
    shell_result = run_shell(command=f"pdftotext -layout {file_path} -")
    text_content = shell_result.stdout
    
    # If pdftotext fails, try Python extraction
    if not text_content or len(text_content) < 50:
        code_result = execute_code_sandbox(code=f"""
import pdfplumber
text = ''
with pdfplumber.open('{file_path}') as pdf:
    for page in pdf.pages:
        extracted = page.extract_text()
        if extracted:
            text += extracted + '\\n'
print(text)
""")
        text_content = code_result
```

## Notes

- `pdftotext` is part of the `poppler-utils` package on most Linux systems
- For scanned/image-only PDFs, consider OCR tools (tesseract) instead
- Always validate parameter names against tool documentation (`filetype` vs `file_type`)