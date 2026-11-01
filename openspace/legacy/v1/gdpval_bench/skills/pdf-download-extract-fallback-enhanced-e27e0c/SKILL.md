---
name: pdf-extract-shell-first
description: PDF text extraction with tool cascade prioritizing shell pdftotext before Python fallback
---

# PDF Extract with Shell-First Tool Cascade

This skill provides an optimized workflow for extracting text content from PDF documents (local files or downloaded URLs) using a prioritized tool cascade that favors shell-based extraction before falling back to Python libraries.

## Why Shell-First?

Analysis of execution patterns shows:
- `read_file` on PDFs sometimes returns binary/image data instead of text
- `run_shell` with `pdftotext` has higher success rate and fewer sandbox errors
- `execute_code_sandbox` can fail with "unknown error" in constrained environments
- Shell tools are more reliable for PDF text extraction when available

## Entry Point: Determine Your Starting Point

**Before beginning, identify your scenario:**

| Scenario | Start Here | Skip |
|----------|-----------|------|
| PDF already on local disk | Step 1 (Try read_file) | Shell download steps |
| PDF at a web URL | Shell download, then Step 1 | None |
| Need maximum reliability | Full cascade (all 3 tools) | None |

## Complete Workflow

### Step 0: Download PDF (URL Only)

If your PDF is at a web URL, download it first using browser user-agent:

```bash
curl -L -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36" -o target.pdf "URL_HERE"
```

Key flags:
- `-L`: Follow redirects
- `-A`: Set user-agent header to mimic a real browser
- `-o`: Specify output filename

**If you already have the PDF locally**, skip to Step 1.

### Step 1: Try read_file (Primary Attempt)

First, attempt to extract text using the `read_file` tool:

```
read_file(filetype="pdf", file_path="target.pdf")
```

**Evaluate the response:**

| Response Type | Interpretation | Next Action |
|---------------|----------------|-------------|
| Clean readable text | Success | Proceed to content analysis |
| Binary data / PNG image / garbled | `read_file` returned raw data | Go to Step 2 immediately |
| Error / timeout | Tool failure | Go to Step 2 immediately |

**Critical:** If `read_file` returns binary image data or garbled content, **do not retry `read_file`**. Immediately proceed to Step 2.

### Step 2: Use run_shell with pdftotext (Preferred Fallback)

When `read_file` fails or returns binary data, use `run_shell` with `pdftotext`:

```bash
run_shell(command="pdftotext target.pdf output.txt")
```

Then read the extracted text:

```
read_file(filetype="txt", file_path="output.txt")
```

**If pdftotext is not found**, install it first:

```bash
run_shell(command="apt-get update && apt-get install -y poppler-utils")
# Or for macOS:
run_shell(command="brew install poppler")
```

Then retry:

```bash
run_shell(command="pdftotext target.pdf output.txt")
```

**Verify extraction quality:**
- Check that `output.txt` exists and has content
- Sample the text to ensure it's readable (not garbled)
- If extraction looks corrupted, proceed to Step 3

### Step 3: Use execute_code_sandbox with PyMuPDF (Last Resort)

If `pdftotext` is unavailable or produces poor results, use Python's PyMuPDF via `execute_code_sandbox`:

```python
import fitz  # PyMuPDF

doc = fitz.open("target.pdf")
text = ""
for page in doc:
    text += page.get_text()
doc.close()

with open("output.txt", "w") as f:
    f.write(text)
print(f"Extracted {len(text)} characters from {len(doc)} pages")
```

Execute via:

```
execute_code_sandbox(code="<python code above>")
```

Then read the result:

```
read_file(filetype="txt", file_path="output.txt")
```

**Note:** `execute_code_sandbox` may fail with "unknown error" in some environments. If this occurs, document the failure and proceed to Step 4.

### Step 4: Graceful Degradation to Domain Knowledge

If all extraction methods fail:

1. Document the specific failure mode for each tool attempted
2. Extract any partial content that was successfully retrieved
3. Supplement missing content from established domain knowledge
4. Clearly mark which portions are from source vs. generated from knowledge
5. Provide citations for any claimed requirements or specifications

Example degradation documentation:

```
EXTRACTION FAILURE REPORT:
- Source: [URL or file path]
- read_file: Returned binary/image data (no text extraction)
- run_shell/pdftotext: [Tool not available / produced garbled output / succeeded]
- execute_code_sandbox/PyMuPDF: [Failed with unknown error / succeeded]

NOTE: Content below combines partial extraction with established domain 
knowledge for [topic]. Verify against official sources when available.
```

## Tool Selection Decision Tree

```
                    PDF to Extract
                          │
                          ▼
                  ┌───────────────┐
                  │  read_file    │
                  │  (primary)    │
                  └───────┬───────┘
                          │
            ┌─────────────┼─────────────┐
            │             │             │
     Returns text   Returns binary   Error/timeout
        (✓)         / image data         │
            │             │             │
            ▼             ▼             ▼
       SUCCESS    ┌───────────────┐
                  │ run_shell     │
                  │ pdftotext     │
                  └───────┬───────┘
                          │
                  ┌───────┼───────┐
                  │       │       │
             Succeeds  Not      Garbled
                (✓)   avail.    output
                  │       │       │
                  ▼       ▼       ▼
             SUCCESS ┌───────────────┐
                     │ execute_code  │
                     │ _sandbox      │
                     │ PyMuPDF       │
                     └───────┬───────┘
                             │
                     ┌───────┼───────┐
                     │       │       │
                Succeeds   Fails   Error
                   (✓)      │       │
                     │      ▼       │
                     ▼   Domain     │
                 SUCCESS  Knowledge │
                             │      │
                             └──────┘
                              FAILURE
                              DOCUMENTED
```

## Complete Automated Script

```bash
#!/bin/bash
# pdf-extract-cascade.sh
# Implements the full tool cascade for PDF extraction

INPUT="$1"
OUTPUT_PDF="target.pdf"
OUTPUT_TXT="output.txt"

# Step 0: Handle URL vs local file
if [[ "$INPUT" =~ ^https?:// ]]; then
    echo "Downloading PDF from URL..."
    curl -L -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36" -o "$OUTPUT_PDF" "$INPUT"
else
    if [ ! -f "$INPUT" ]; then
        echo "ERROR: Local file not found: $INPUT"
        exit 1
    fi
    OUTPUT_PDF="$INPUT"
fi

# Step 1: Verify file type
echo "Verifying file type..."
if ! file "$OUTPUT_PDF" | grep -q "PDF document"; then
    echo "WARNING: File is not a valid PDF"
    file "$OUTPUT_PDF"
fi

# Step 2: Try pdftotext (shell-first approach)
echo "Attempting pdftotext extraction..."
if command -v pdftotext &> /dev/null; then
    if pdftotext "$OUTPUT_PDF" "$OUTPUT_TXT" 2>/dev/null; then
        if [ -s "$OUTPUT_TXT" ]; then
            echo "SUCCESS: Extraction completed with pdftotext"
            wc -l "$OUTPUT_TXT"
            exit 0
        fi
    fi
fi

# Step 3: Fallback to PyMuPDF
echo "Falling back to PyMuPDF..."
python3 << 'PYTHON_SCRIPT'
import fitz
import sys

try:
    doc = fitz.open("target.pdf")
    text = ""
    for page in doc:
        text += page.get_text()
    doc.close()
    with open("output.txt", "w") as f:
        f.write(text)
    print(f"SUCCESS: Extracted {len(text)} characters from {len(doc)} pages")
    sys.exit(0)
except Exception as e:
    print(f"PyMuPDF failed: {e}")
    sys.exit(1)
PYTHON_SCRIPT

# Step 4: Handle complete failure
if [ $? -ne 0 ]; then
    echo "FAILURE: All extraction methods failed"
    echo "ACTION: Generate content from domain knowledge"
    echo "Document each tool's failure mode for future reference"
    exit 1
fi
```

## Best Practices

1. **Never retry read_file on binary response**: If `read_file` returns image/binary data, immediately switch to `run_shell`
2. **Prefer run_shell over execute_code_sandbox**: Shell tools have higher reliability and fewer sandbox-related errors
3. **Verify before trusting**: Always check extracted text is readable, not just that the command succeeded
4. **Document failures**: Record which tools failed and how, to inform future extraction attempts
5. **Preserve originals**: Keep the source PDF for debugging and re-extraction if needed
6. **Check tool availability early**: Test for `pdftotext` before starting complex workflows

## Common Failure Modes and Responses

| Symptom | Likely Cause | Recommended Action |
|---------|--------------|-------------------|
| read_file returns PNG/binary | PDF rendered as image, not parsed | Immediately use run_shell with pdftotext |
| pdftotext: command not found | poppler-utils not installed | Run `apt-get install poppler-utils` first |
| pdftotext produces empty file | Password-protected or scanned PDF | Try PyMuPDF, or use OCR tools |
| execute_code_sandbox "unknown error" | Sandbox execution issue | Document failure, use domain knowledge fallback |
| Garbled text output | Encoding issues | Try PyMuPDF with `page.get_text("text")` |
| All tools fail | Severely corrupted or encrypted PDF | Document limitation, use knowledge-based content |

## When to Use This Skill

- Extracting text from local PDF files where `read_file` may return binary data
- Processing PDFs in automated pipelines requiring high reliability
- Situations where `execute_code_sandbox` has shown instability
- Working with PDFs from sources that may deliver rendered images instead of parseable text
- Any workflow where shell tool availability can be assumed or easily installed

## Migration Notes from pdf-download-extract-fallback

This skill (`pdf-extract-shell-first`) differs from the parent in these key ways:

1. **Explicit tool sequencing**: Clearly prioritizes `read_file` → `run_shell` → `execute_code_sandbox`
2. **No retry on binary read_file**: Instructs immediate fallback when binary data detected
3. **Shell-first philosophy**: Emphasizes `pdftotext` via `run_shell` as preferred over Python
4. **Reduced download focus**: Assumes PDF is available or downloads in pre-step; focuses on extraction cascade
5. **Decision tree visualization**: Provides clear flowchart for tool selection
