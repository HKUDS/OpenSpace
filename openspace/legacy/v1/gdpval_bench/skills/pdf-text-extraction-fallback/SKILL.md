---
name: pdf-text-extraction-fallback
description: Extract text from PDFs using pdftotext when read_file returns binary data
---

# PDF Text Extraction Fallback

## When to Use This Skill

Use this skill when `read_file` with `filetype="pdf"` returns binary/image data instead of readable text content. This is a common issue with PDF files that contain embedded images or complex formatting.

## Steps

### 1. Validate Parameters First

Before attempting extraction, ensure you're using the correct parameter name:
- **Use `filetype`** (not `file_type`) for the `read_file` function
- Incorrect parameter names can cause silent failures

```python
# Correct
read_file(filetype="pdf", file_path="document.pdf")

# Incorrect - may fail silently
read_file(file_type="pdf", file_path="document.pdf")
```

### 2. Detect Binary Data Issue

After calling `read_file`, check if the result contains:
- Garbled/binary characters
- Image data representations (e.g., `b'...'` byte strings with non-text content)
- Unreadable or corrupted-looking content

If yes, proceed with the pdftotext workaround.

### 3. Extract Text via pdftotext

Use `run_shell` to call pdftotext, which extracts text directly from PDF files:

```python
# Extract text to stdout
result = run_shell(command="pdftotext /path/to/document.pdf -")
text_content = result.stdout
```

The `-` flag tells pdftotext to output to stdout instead of creating a file.

### 4. Handle Output and Errors

```python
result = run_shell(command="pdftotext /path/to/document.pdf -")

if result.stderr:
    # Check for errors like "pdftotext not found"
    # May need to install poppler-utils
    pass

text_content = result.stdout
# text_content now contains the extracted text
```

## Example Workflow

```python
# Step 1: Try normal read with correct parameters
content = read_file(filetype="pdf", file_path="reference.pdf")

# Step 2: Check if content is readable
if not content or looks_like_binary(content):
    # Step 3: Fall back to pdftotext
    result = run_shell(command="pdftotext reference.pdf -")
    text_content = result.stdout
    
    # Step 4: Verify extraction succeeded
    if result.stderr:
        # Handle error (e.g., install pdftotext)
        pass
```

## Installation Notes

pdftotext is part of the poppler-utils package:

- **Debian/Ubuntu**: `apt-get install poppler-utils`
- **macOS**: `brew install poppler`
- **Many Linux environments**: Pre-installed

## Alternative: Output to File

If stdout approach has issues, output to a temporary file:

```python
run_shell(command="pdftotext /path/to/document.pdf /tmp/output.txt")
text_content = read_file(filetype="txt", file_path="/tmp/output.txt")
```

## Best Practices

1. Always validate the `filetype` parameter spelling before troubleshooting
2. Check both stdout and stderr from pdftotext
3. For multi-page PDFs, pdftotext preserves page breaks with form feeds
4. This method works better for text-based PDFs than image-scanned PDFs