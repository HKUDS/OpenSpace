---
name: pdf-extract-progressive-tools
description: Progressive tool-chain PDF extraction with explicit read_file, run_shell, and execute_code_sandbox sequencing
---

# PDF Text Extraction with Progressive Tool Fallback

This skill provides a robust workflow for extracting text from PDF documents using a sequenced approach with agent tools, with explicit fallback mechanisms based on observed tool behavior.

## Critical Insight from Execution Data

**read_file often returns binary/image data for PDFs, not extracted text.** When this occurs, immediately escalate to run_shell with pdftotext before attempting Python-based extraction.

## Entry Point: Determine Your Starting Point

**Before beginning, identify your scenario:**

| Scenario | Start Here | Skip |
|----------|-----------|------|
| PDF already on local disk | Step 1 (read_file attempt) | Download steps |
| PDF at a web URL | Download first, then Step 1 | None |
| PDF content already extracted | Step 4 (Quality verification) | Steps 1-3 |

## Overview

PDF extraction failures cascade when tool sequencing is unclear. This workflow ensures maximum success rate through **explicit tool progression**:

1. **read_file** - Quick attempt, but may return binary data
2. **run_shell + pdftotext** - Reliable extraction when read_file fails
3. **execute_code_sandbox + PyMuPDF** - Final fallback for complex PDFs

## Step-by-Step Instructions

### Step 1: Attempt read_file First

Always try the simplest approach first:

```
Tool: read_file
Path: document.pdf
```

**Expected outcome:** Extracted text content

**Critical check:** Examine the returned content:
- ✅ **Text visible:** Proceed to Step 4 (Quality verification)
- ⚠️ **Binary/image data detected:** Immediately proceed to Step 2
- ❌ **File not found:** Verify path or download first

**Binary data indicators:**
- Content starts with `%PDF-` header without text extraction
- Content appears as garbled characters or base64
- Content contains PNG/JPEG markers within PDF wrapper
- File size seems reasonable but no readable text

### Step 2: Escalate to run_shell with pdftotext

**When read_file returns binary data, do NOT attempt execute_code_sandbox yet.** Use run_shell immediately:

```
Tool: run_shell
Command: pdftotext document.pdf document.txt
```

If pdftotext is not available:

```
Tool: run_shell
Command: apt-get update && apt-get install -y poppler-utils && pdftotext document.pdf document.txt
```

**Then read the extracted text:**

```
Tool: read_file
Path: document.txt
```

**Expected outcome:** Clean text extraction

**If this fails:**
- Check if file is password-protected
- Check if file is corrupted (run `file document.pdf`)
- Proceed to Step 3

### Step 3: Final Fallback to execute_code_sandbox with PyMuPDF

Only attempt this if Steps 1-2 fail:

```
Tool: execute_code_sandbox
Language: python
Code: |
  import fitz  # PyMuPDF
  
  try:
      doc = fitz.open("document.pdf")
      text = ""
      for page in doc:
          text += page.get_text()
      doc.close()
      
      with open("document_pymupdf.txt", "w") as f:
          f.write(text)
      
      print("SUCCESS: Extracted {} characters".format(len(text)))
  except Exception as e:
      print(f"FAILED: {e}")
```

**Then read the result:**

```
Tool: read_file
Path: document_pymupdf.txt
```

### Step 4: Quality Verification

Regardless of which method succeeded, verify extraction quality:

1. **Check text length:** Should be proportional to PDF pages (~500-2000 chars per page)
2. **Check readability:** Text should form coherent sentences
3. **Check for truncation:** Look for cut-off words or missing sections
4. **Compare methods:** If multiple methods worked, compare outputs

**If quality is poor:**
- Try alternative extraction tools (pdfplumber, camelot-py for tables)
- Consider OCR for scanned documents
- Document limitations clearly

### Step 5: Graceful Degradation to Domain Knowledge

If all extraction methods fail:

1. Document the specific failure mode for each tool attempted
2. Extract any partial content that was successfully retrieved
3. Supplement missing content from established domain knowledge
4. Clearly mark which portions are from source vs. generated from knowledge
5. Provide citations for any claimed requirements or specifications

**Example degradation note:**

```
NOTE: Source document [path/URL] was inaccessible due to [specific tool failures].
Content below combines partial extraction with established domain knowledge 
for [topic]. All claims verified against [alternative sources] where possible.

Tool Failure Log:
- read_file: Returned binary data (no text extraction)
- run_shell/pdftotext: Command not available in environment
- execute_code_sandbox/PyMuPDF: Sandbox execution failed with [error]
```

## Complete Tool Orchestration Script

```python
# pdf-extract-orchestrator.py
# Implements the progressive tool fallback pattern

def extract_pdf_text(pdf_path):
    """
    Progressive PDF extraction following tool precedence:
    1. read_file (quick check)
    2. run_shell + pdftotext (primary extraction)
    3. execute_code_sandbox + PyMuPDF (final fallback)
    """
    extraction_log = []
    
    # Step 1: Try read_file
    print("Step 1: Attempting read_file...")
    try:
        content = read_file(pdf_path)
        if is_binary_or_image_data(content):
            extraction_log.append("read_file: Returned binary data")
            # Proceed to Step 2
        else:
            extraction_log.append("read_file: Success")
            return content, extraction_log
    except Exception as e:
        extraction_log.append(f"read_file: Failed - {e}")
    
    # Step 2: Try run_shell with pdftotext
    print("Step 2: Attempting run_shell + pdftotext...")
    try:
        run_shell(f"pdftotext {pdf_path} output.txt")
        content = read_file("output.txt")
        if content and len(content) > 100:
            extraction_log.append("run_shell/pdftotext: Success")
            return content, extraction_log
        else:
            extraction_log.append("run_shell/pdftotext: Empty extraction")
    except Exception as e:
        extraction_log.append(f"run_shell/pdftotext: Failed - {e}")
    
    # Step 3: Try execute_code_sandbox with PyMuPDF
    print("Step 3: Attempting execute_code_sandbox + PyMuPDF...")
    try:
        code = """
import fitz
doc = fitz.open("""" + pdf_path + """")
text = ""
for page in doc:
    text += page.get_text()
doc.close()
print(text[:1000])  # Preview
"""
        result = execute_code_sandbox(language="python", code=code)
        extraction_log.append("execute_code_sandbox/PyMuPDF: Success")
        return result, extraction_log
    except Exception as e:
        extraction_log.append(f"execute_code_sandbox/PyMuPDF: Failed - {e}")
    
    # Step 4: All methods failed
    extraction_log.append("ALL METHODS FAILED - Escalate to domain knowledge")
    return None, extraction_log

def is_binary_or_image_data(content):
    """Detect if content is binary/image data rather than extracted text"""
    if not content:
        return True
    # Check for PDF header without text extraction
    if content.startswith("%PDF-"):
        return True
    # Check for high ratio of non-printable characters
    non_printable = sum(1 for c in content if ord(c) < 32 and c not in '\n\r\t')
    if len(content) > 0 and non_printable / len(content) > 0.1:
        return True
    return False
```

## Tool Precedence Decision Tree

```
                    ┌─────────────────┐
                    │  Start: PDF     │
                    │  Available?     │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │   Step 1:       │
                    │   read_file     │
                    └────────┬────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
        ┌─────▼─────┐  ┌─────▼─────┐  ┌─────▼─────┐
        │  Text     │  │  Binary   │  │  Error/   │
        │  Returned │  │  Data     │  │  Not Found│
        └─────┬─────┘  └─────┬─────┘  └─────┬─────┘
              │              │              │
              │         ┌────▼─────┐  ┌────▼─────┐
              │         │ Step 2:  │  │ Download │
              │         │ run_shell│  │ or Fix   │
              │         │ pdftotext│  │ Path     │
              │         └────┬─────┘  └──────────┘
              │              │
              │         ┌────▼─────┐
              │         │ Success? │
              │         └────┬─────┘
              │              │
        ┌─────▼─────┐  ┌─────▼─────┐
        │  Yes      │  │  No       │
        └─────┬─────┘  └─────┬─────┘
              │              │
              │         ┌────▼─────────┐
              │         │ Step 3:      │
              │         │ execute_     │
              │         │ code_sandbox │
              │         │ PyMuPDF      │
              │         └──────────────┘
              │
        ┌─────▼──────────────────┐
        │  Step 4: Quality Check │
        │  Step 5: Document      │
        │  Limitations           │
        └────────────────────────┘
```

## Best Practices

1. **Check read_file output immediately:** Don't assume it extracted text - verify before proceeding
2. **Escalate quickly on binary data:** Don't waste iterations trying read_file multiple times
3. **Prefer run_shell over execute_code_sandbox:** Shell tools are more reliable for PDF extraction when available
4. **Log each tool attempt:** Document which method succeeded for future reference
5. **Preserve extraction artifacts:** Keep intermediate files for debugging
6. **Verify extraction quality:** Check text length and readability before accepting results
7. **Document tool failures:** When falling back to domain knowledge, specify which tools failed and why

## Common Failure Modes by Tool

| Tool | Symptom | Cause | Solution |
|------|---------|-------|----------|
| read_file | Binary PDF data | Tool doesn't extract PDF text | Escalate to run_shell immediately |
| read_file | PNG/JPEG data | PDF contains embedded images | Use OCR tools or request text version |
| run_shell | pdftotext not found | Tool not installed | Install poppler-utils first |
| run_shell | Empty output | Password-protected PDF | Request accessible version |
| execute_code_sandbox | Unknown error | Sandbox execution issue | Try run_shell alternative or document limitation |
| execute_code_sandbox | Import error | PyMuPDF not installed | Include pip install in script |

## When to Use This Skill

- **PDFs from web downloads:** After downloading, apply this extraction workflow
- **PDFs already local:** Start at Step 1 with existing file path
- **Automated document processing:** Where reliability matters more than speed
- **Regulatory/compliance documents:** Where source verification is critical
- **Situations with tool uncertainty:** When environment capabilities are unknown

## Migration from Parent Skill

This skill enhances `pdf-download-extract-fallback` by:

1. **Explicit tool sequencing:** Parent described shell commands; this specifies agent tool order
2. **Binary detection:** Parent assumed download success; this checks read_file output quality
3. **Faster escalation:** Parent tried pdftotext then PyMuPDF; this escalates immediately on binary data
4. **Agent-focused:** Parent was shell-script focused; this is optimized for agent tool calls
5. **Execution insights:** Incorporates learnings from failed task 0353ee0c showing read_file limitations
