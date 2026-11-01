---
name: pdf-text-extraction-fallback-85d5ca
description: Fallback workflow for extracting text from PDFs when read_file returns binary data
---

# PDF Text Extraction Fallback

Use this skill when `read_file` returns binary data or garbled content for PDF files instead of readable text. This workflow provides a reliable fallback using command-line PDF tools.

## When to Use

- `read_file` with `filetype: pdf` returns binary data, unreadable characters, or errors
- You need to extract text from a PDF to process its contents
- Standard file reading methods fail to extract usable text

## Step-by-Step Instructions

### Step 1: Detect Binary/Unreadable PDF Output

After attempting to read a PDF with `read_file`, check if the output is:
- Binary data (contains null bytes, non-printable characters)
- Garbled text with many special characters
- Empty or truncated content

```
# Example of problematic output from read_file
%PDF-1.4
1 0 obj
<< /Type /Catalog ...
```

If the output looks like raw PDF structure or binary, proceed to Step 2.

### Step 2: Use shell_agent with PDF Tools

Invoke `shell_agent` to extract text using `pdftotext` (preferred) or `pdfplumber` (Python fallback):

```
Task: Extract all text content from <filename.pdf> using pdftotext or pdfplumber.
Output the extracted text in readable format. If pdftotext is not available, use Python with pdfplumber library.
```

**Example shell_agent invocation:**
```
shell_agent task="Extract text from Move_Out_Inspection_Tracker.pdf using pdftotext. Save output to a .txt file and return the content."
```

### Step 3: Validate Extracted Content

After extraction, validate that the content contains expected text patterns:

```python
# Validation checklist
def validate_pdf_extraction(text, expected_patterns=None):
    checks = [
        bool(text.strip()),  # Not empty
        len(text) > 50,  # Has substantial content
        not text.startswith('%PDF'),  # Not raw PDF structure
    ]
    
    if expected_patterns:
        for pattern in expected_patterns:
            checks.append(pattern.lower() in text.lower())
    
    return all(checks)
```

**Common expected patterns to check:**
- Document-specific keywords (e.g., "inspection", "resident", "date")
- Expected data formats (dates, names, IDs)
- Minimum word count threshold

### Step 4: Handle Extraction Failures

If validation fails:

1. **Try alternative tool:** If `pdftotext` failed, try `pdfplumber`:
   ```
   shell_agent task="Extract text from <file.pdf> using Python pdfplumber library. Handle any encoding issues."
   ```

2. **Try OCR fallback:** For scanned PDFs:
   ```
   shell_agent task="This PDF may be scanned. Use pytesseract or similar OCR tool to extract text from <file.pdf>."
   ```

3. **Report specific error:** Document what patterns were expected but not found.

### Step 5: Proceed with Data Processing

Once validated text is obtained:
- Parse the extracted text for required data
- Store or process the content as needed
- Continue with the original task workflow

## Code Example

```python
# Complete extraction workflow
def extract_pdf_text_fallback(pdf_path, expected_patterns=None):
    """Extract text from PDF with fallback handling."""
    
    # Step 1: Try read_file first
    content = read_file(filetype="pdf", file_path=pdf_path)
    
    # Step 2: Check if binary/unreadable
    if is_binary_or_garbled(content):
        # Step 3: Use shell_agent fallback
        result = shell_agent(
            task=f"Extract all text from {pdf_path} using pdftotext. Return the text content."
        )
        content = result.stdout
        
        # Step 4: Validate
        if not validate_pdf_extraction(content, expected_patterns):
            # Try pdfplumber as secondary fallback
            result = shell_agent(
                task=f"Extract text from {pdf_path} using Python pdfplumber library."
            )
            content = result.stdout
    
    return content

def is_binary_or_garbled(text):
    """Check if text appears to be binary or unreadable."""
    if not text:
        return True
    if text.startswith('%PDF'):
        return True
    # Check for high ratio of non-printable characters
    non_printable = sum(1 for c in text if ord(c) > 127 or ord(c) < 32)
    return non_printable / len(text) > 0.3
```

## Tips

- **pdftotext** is typically faster and pre-installed on many systems
- **pdfplumber** handles complex layouts better but requires Python
- For **scanned PDFs**, you'll need OCR tools (tesseract, pytesseract)
- Always validate extracted content matches your expected data patterns
- Save intermediate extraction results for debugging if needed