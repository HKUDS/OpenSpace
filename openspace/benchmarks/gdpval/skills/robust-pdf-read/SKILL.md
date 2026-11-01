---
name: robust-pdf-read
description: Reliably extract text from PDFs using pdftotext when standard file reading fails.
category: tool_guide
---

# Robust PDF Text Extraction

## Problem
Standard file reading tools (e.g., `read_file`) often fail to extract text from PDF documents. Instead of returning parsed text, they may return:
- Raw binary data
- Base64 encoded images
- Garbled characters or null bytes

This occurs because PDFs are complex binary formats, not plain text files. Attempts to parse them using general-purpose Python libraries (like PyMuPDF) in sandboxed environments may also fail due to missing dependencies or environment restrictions.

## Solution
Use the `pdftotext` command-line utility (part of `poppler-utils`) via `run_shell`. This tool is commonly pre-installed in Linux environments and reliably extracts text content from PDFs.

## Procedure

### 1. Detect Extraction Failure
When attempting to read a PDF:
- Check the content returned by `read_file`.
- If the content contains null bytes (`\x00`), appears as base64, or is clearly binary/garbled, assume standard reading has failed.

### 2. Execute pdftotext
Run the following shell command using `run_shell`:

```bash
pdftotext -layout -nopgbrk <file_path> -
```

- `-layout`: Maintains the physical layout of the text (optional but recommended).
- `-nopgbrk`: Prevents inserting form feed characters between pages.
- `-`: Outputs content to stdout instead of creating a new file.

### 3. Parse Output
Capture the stdout from the shell command. This string is the extracted text.

## Example Usage

**Scenario:** You need to read `document.pdf`.

**Step 1: Attempt standard read**
```python
content = read_file("document.pdf")
if "\x00" in content or not content.strip():
    # Fallback needed
    pass
```

**Step 2: Fallback to shell**
```python
result = run_shell("pdftotext -layout -nopgbrk document.pdf -")
text = result.stdout
```

## Prerequisites
- The environment must have `pdftotext` installed (usually via `poppler-utils`).
- If `pdftotext` is not found, attempt to install it (`apt-get install poppler-utils`) if permissions allow, or notify the user.

## Benefits
- **Reliability:** Bypasses Python library dependency issues in sandboxes.
- **Speed:** Command-line tools are often faster than loading heavy Python libraries.
- **Compatibility:** Works consistently across most Linux-based agent environments.