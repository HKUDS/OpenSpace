---
name: pdf-extract-ordered-fallback
description: PDF extraction with ordered tool chain: read_file, then run_shell/pdftotext, then execute_code_sandbox/PyMuPDF
---

# PDF Download and Extract with Ordered Fallback

This skill provides a robust workflow for acquiring PDF documents from web sources and extracting their text content, with a clearly ordered sequence of tool invocations to maximize success rate.

## Overview

When working with PDFs from web sources, encounters with JavaScript redirects, corrupted files, missing tools, or inaccessible content are common. This workflow ensures maximum success rate through a严格 ordered fallback sequence that prioritizes shell-based tools over Python sandbox execution.

## Ordered Tool Chain Summary

| Step | Tool | Method | Priority |
|------|------|--------|----------|
| 0 | read_file | Direct PDF text extraction | First attempt |
| 1 | run_shell | pdftotext command | Primary fallback (if Step 0 returns binary/fails) |
| 2 | execute_code_sandbox | PyMuPDF Python library | Secondary fallback (if Step 1 fails) |
| 3 | Domain knowledge | Manual content generation | Last resort |

**Key principle**: Always try shell tools (`run_shell`) before Python sandbox (`execute_code_sandbox`) when both are viable options. Shell execution is more reliable in constrained environments.

## Step-by-Step Instructions

### Step 0: Initial Extraction Attempt with read_file Tool

First, attempt to extract PDF text using the `read_file` tool. This is the simplest approach and handles many PDFs correctly:

```
read_file filetype="pdf" file_path="path/to/document.pdf"
```

Expected outcomes:
- **Success**: Returns extracted text content - proceed to use this directly
- **Binary/Image data returned**: The tool failed to extract text; file content is raw binary or image data
  - **Immediate action**: Proceed to Step 1 (run_shell with pdftotext)
- **Error returned**: Tool failed entirely; proceed to Step 1 (run_shell with pdftotext)

**Critical**: If `read_file` returns binary data (PNG/JPEG headers, raw PDF bytes), do NOT attempt to parse it manually. Immediately switch to shell-based `pdftotext`.

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

### Step 3: Primary Shell Extraction with pdftotext via run_shell

**This step takes priority over Python-based extraction.** If Step 0 failed or if you're working with a newly downloaded PDF, use `run_shell` with `pdftotext` before attempting any Python libraries:

```bash
pdftotext downloaded.pdf extracted.txt
```

Execute via `run_shell`:
```
run_shell command="pdftotext downloaded.pdf extracted.txt"
```

If `pdftotext` is not available, install it first:
```bash
# Debian/Ubuntu
apt-get update && apt-get install -y poppler-utils

# macOS  
brew install poppler

# RHEL/CentOS
yum install -y poppler-utils
```

**Why shell-first?** Shell-based `pdftotext` is more reliable, faster, and avoids sandbox execution issues that can affect Python code execution in constrained environments.

### Step 4: Secondary Python Fallback with PyMuPDF via execute_code_sandbox

Only if `run_shell` with `pdftotext` fails or is unavailable, fall back to Python's PyMuPDF library via `execute_code_sandbox`:

```python
import fitz  # PyMuPDF

doc = fitz.open("downloaded.pdf")
text = ""
for page in doc:
    text += page.get_text()
doc.close()

with open("extracted.txt", "w") as f:
    f.write(text)
```

Execute within `execute_code_sandbox`:
```
execute_code_sandbox code="<Python code above>"
```

Install if needed:
```bash
pip install pymupdf
```

**Note**: Some environments may experience `execute_code_sandbox` failures (unknown errors). This is why shell-based extraction (Step 3) must be attempted first.

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

PDF_URL="$1"
OUTPUT_PDF="downloaded.pdf"
OUTPUT_TXT="extracted.txt"

# Step 0/1: Download with browser user-agent
echo "Downloading PDF..."
curl -L -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36" -o "$OUTPUT_PDF" "$PDF_URL"

# Step 1: Verify file type
echo "Verifying file type..."
if ! file "$OUTPUT_PDF" | grep -q "PDF document"; then
    echo "WARNING: Downloaded file is not a valid PDF"
    echo "Attempting fallback extraction anyway..."
fi

# Step 2: Try pdftotext via shell (PRIMARY EXTRACTION)
echo "Attempting pdftotext extraction..."
if command -v pdftotext &> /dev/null; then
    if pdftotext "$OUTPUT_PDF" "$OUTPUT_TXT" 2>/dev/null; then
        echo "Extraction successful with pdftotext"
        exit 0
    fi
fi

# Step 3: Fallback to PyMuPDF via Python sandbox (SECONDARY)
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

# Step 4: Handle complete failure
if [ $? -ne 0 ]; then
    echo "All extraction methods failed. Generate content from domain knowledge."
    echo "Document the failure and proceed with knowledge-based content generation."
fi
```

## Agent-Specific Tool Invocation Pattern

For AI agents with access to specialized tools, follow this exact sequence:

```
# ITERATION 1: Try read_file first
read_file filetype="pdf" file_path="document.pdf"

# If read_file returns binary data or fails:
# ITERATION 2: Use run_shell with pdftotext
run_shell command="pdftotext document.pdf extracted.txt"

# If run_shell fails:
# ITERATION 3: Use execute_code_sandbox with PyMuPDF
execute_code_sandbox code="import fitz; doc = fitz.open('document.pdf'); ..."

# If all automated extraction fails:
# ITERATION 4+: Document failure mode and generate from domain knowledge
```

**Critical anti-pattern to avoid**: Do NOT attempt `execute_code_sandbox` before `run_shell` for PDF extraction. Shell tools are more reliable and should be prioritized.

## Failure Recovery from Observed Patterns

Based on execution analysis, here are common failure cascades and recovery strategies:

### Pattern: read_file Returns Binary Data
**Symptom**: `read_file` returns PNG/JPEG image data or raw PDF bytes instead of text
**Cause**: Tool cannot extract text from scanned PDFs or certain PDF structures  
**Recovery**: Immediately switch to `run_shell` with `pdftotext` - do not attempt to parse binary data

### Pattern: execute_code_sandbox Returns Unknown Error
**Symptom**: Multiple `execute_code_sandbox` calls fail with "unknown error"
**Cause**: Sandbox execution environment issues or resource constraints
**Recovery**: This is why `run_shell` must be attempted first - shell execution bypasses sandbox limitations

### Pattern: read_webpage Fails on All URLs
**Symptom**: All `read_webpage` calls to domain URLs return errors
**Cause**: Anti-bot measures, network issues, or site blocking
**Recovery**: Focus on PDF extraction from locally downloaded files; supplement missing context from domain knowledge with clear citations

## Best Practices

1. **Always verify before parsing**: Never assume a downloaded file is valid
2. **Preserve original PDF**: Keep the downloaded file for debugging if needed
3. **Log each step**: Document which method succeeded for future reference
4. **Check extraction quality**: Verify extracted text is readable and complete
5. **Cite source limitations**: When using fallback knowledge, clearly indicate source gaps
6. **Follow tool ordering**: read_file → run_shell → execute_code_sandbox → domain knowledge
7. **Shell before Python**: Prioritize `run_shell` over `execute_code_sandbox` when both are viable
8. **Detect binary early**: If `read_file` returns non-text data, immediately switch to shell tools

## Common Failure Modes

| Symptom | Cause | Solution |
|---------|-------|----------|
| HTML content in PDF | URL redirected to error page | Check HTTP status, try alternate URL |
| Empty extraction | Password-protected or scanned PDF | Try OCR tools or request accessible version |
| Garbled text | Encoding issues | Try PyMuPDF with different extraction mode |
| read_file returns binary | Scanned PDF or tool limitation | Immediately use run_shell with pdftotext |
| execute_code_sandbox unknown error | Sandbox execution failure | This is why run_shell should be tried first |
| Curl blocked | Anti-bot measures | Add more headers, use delay between requests |

## When to Use This Skill

- Downloading regulatory documents from government websites
- Extracting content from technical manuals or handbooks
- Processing PDFs in automated pipelines where reliability matters
- Situations where tool execution constraints may limit Python sandbox availability
- Any situation where PDF access may be unreliable or restricted
