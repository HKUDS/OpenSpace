---
name: resilient-context-extraction
description: Ensures agents extract data from context files with validation and fallback strategies before resorting to assumptions or external searches.
category: workflow
---

# Resilient Context Extraction

## Objective
Prevent data hallucination and inefficiency by mandating that agents inspect, validate, and fully extract data from provided reference files before attempting web searches or generating synthetic data. When extraction is incomplete, use fallback strategies before making assumptions.

## Critical Rule
**If a reference file is provided in the task context, it is the source of truth.** Do not fabricate data or search the web for information that may exist within the provided attachments. If extraction appears incomplete, attempt alternative methods before proceeding.

## Workflow Steps

### 1. Scan Context for Attachments
At the start of every task, explicitly list all files provided in the context window or attachment panel.
- Check for spreadsheets (`.xlsx`, `.csv`), documents (`.pdf`, `.docx`, `.pptx`), or data dumps (`.json`, `.txt`, `.xml`, `.yaml`).
- Note the filename, file size (if available), and inferred content type.
- **Record this list** for later verification.

### 2. Evaluate Relevance
Determine if any provided file contains the data required to complete the task.
- **Match Keywords:** Do filenames or expected column headers match task requirements?
- **Check Scope:** Does the data cover the necessary timeframe or region?
- **Prioritize:** Rank files by likelihood of containing required data.

### 3. Extract Data with Validation
If relevant files are found:
- Read the file content using appropriate tools (e.g., `read_file`, `pandas`, `pdf_reader`).
- **Validate Extraction Completeness** (NEW CRITICAL STEP):
  - Check if output appears truncated (e.g., sudden cutoff mid-sentence, character limits hit).
  - Compare expected data points vs. extracted data points (e.g., "Task mentions pricing tiers; did extraction include pricing numbers?").
  - Look for structural indicators of incompleteness (e.g., unclosed tables, missing document endings).
- **If extraction is incomplete or suspicious**, proceed to Step 4 (Fallback Strategies) before using the data.

### 4. Apply Fallback Extraction Strategies
When initial extraction is incomplete or critical data is missing, attempt these strategies **in order** before making assumptions:

#### 4a. Alternative Library/Tool Approach
- For `.docx`: Try `python-docx` directly via `execute_code_sandbox` if `read_file` was truncated.
- For `.xlsx`/`.csv`: Try `pandas` with explicit sheet selection or `openpyxl` directly.
- For `.pdf`: Try `pdfplumber`, `PyPDF2`, or shell-based `pdftotext` if available.
- For any format: Try reading as raw bytes/text via shell commands (`cat`, `xxd`, `strings`).

#### 4b. Shell-Based Extraction
- Use `shell_agent` or `run_shell` for format-specific tools:
  - `unzip -p file.docx word/document.xml | xmllint --format -` (extract raw docx XML)
  - `in2csv file.xlsx` (convert Excel to CSV via shell)
  - `pdftotext file.pdf -` (extract PDF text via command line)
- This bypasses potential sandbox or library limitations in `read_file`.

#### 4c. Targeted Re-Reading
- If specific sections are missing (e.g., "pricing table on page 3"), attempt to re-read with focus on that region.
- Use tools that support page/section ranges if available.

#### 4d. Document What's Missing
- **Before any assumptions or external searches**, explicitly document:
  - What data was expected but not found.
  - What extraction methods were attempted.
  - Why each method failed or what portions remain unavailable.
- Example: "Attempted `read_file` on `Pricing_email.docx` (truncated at 980 chars). Attempted `python-docx` extraction via sandbox—pricing table in section 2 not present in file. Pricing numbers unavailable from provided context."

### 5. Cite Source Explicitly
When presenting data in the final output:
- Explicitly state which file the data came from.
- Example: "According to `Massabama_active_listings.xlsx`..."
- If fallback methods were used, note this: "Extracted via shell-based `pdftotext` from `report.pdf`..."
- This verifies to the user that real data was used, not hallucinated.

### 6. Fallback to Search (Last Resort Only)
If all extraction strategies fail to provide the specific data needed:
- State clearly what was missing from the files.
- Document all extraction methods attempted and why they failed.
- **Then** proceed with web search or estimation.
- Mark any non-file data clearly as "External Search" or "Estimated (file data unavailable)".
- **Never** present estimated data as if it came from the file.

## Checklist

- [ ] Did I list all attached files with their types?
- [ ] Did I open and read the relevant files?
- [ ] Did I validate extraction completeness (check for truncation, missing sections)?
- [ ] If extraction was incomplete, did I try at least one fallback strategy?
- [ ] Did I explicitly document what data is unavailable before making assumptions?
- [ ] Did I cite the file name (and extraction method if non-standard) in my output?
- [ ] Did I avoid fabricating numbers that should have come from the file?
- [ ] Is any external/estimated data clearly marked as such?

## Example Usage

**Task:** Create a report on active property listings.
**Context:** `Massabama_active_listings.xlsx` is attached.

**Incorrect Approach:**
- Ignore the Excel file.
- Search web for "Massabama property listings".
- Fabricate numbers based on search snippets.
- **Result:** Inaccurate data, ignored source of truth.

**Correct Approach:**
- Identify `Massabama_active_listings.xlsx` in context.
- Load the Excel file via `read_file` or `pandas`.
- Validate: Check row count matches expected scope, verify all columns present.
- Count rows and sum prices directly from the sheet.
- Output: "Based on the 50 entries in `Massabama_active_listings.xlsx`, the total value is..."
- **Result:** Accurate data, verified source.

---

**Task:** Extract pricing tiers from `Pricing_email.docx`.
**Context:** `Pricing_email.docx` attached, but `read_file` output cuts off mid-sentence.

**Incorrect Approach:**
- Accept truncated extraction as complete.
- Assume pricing tiers based on incomplete info.
- Proceed with fabricated numbers.

**Correct Approach:**
- Identify truncation: `read_file` output ends at 980 chars, mid-sentence.
- Attempt fallback: Use `execute_code_sandbox` with `python-docx` to read full document.
- If fallback also incomplete: Document "Pricing table in section 2 not extractable; attempted `read_file` (truncated) and `python-docx` (table structure not preserved)."
- Only then: Either request clarification from user, or mark pricing as "Estimated—file data unavailable" if task requires proceeding.
- **Result:** User informed of limitation; no false claim of file-sourced data.

## Warnings

- **Hallucination Risk:** Ignoring provided files OR accepting incomplete extractions without validation are primary causes of data fabrication.
- **Efficiency:** Reading a local file is faster and more reliable than web scraping—but only if extraction is complete.
- **User Expectation:** Users attach files expecting them to be used. Ignoring them or failing to extract fully is a failure of instruction following.
- **Truncation Blind Spot:** Many tools have character/content limits. Always verify output completeness against expected data scope.

## Tool-Specific Notes

| Tool | Known Limitations | Fallback Strategy |
|------|-------------------|-------------------|
| `read_file` | May truncate at ~1000-5000 chars depending on format | Use `execute_code_sandbox` with format-specific library |
| `execute_code_sandbox` | May have missing dependencies or sandbox errors | Use `shell_agent` or `run_shell` for CLI tools |
| `shell_agent` | Slower, but more flexible with system tools | Use for `pdftotext`, `unzip` XML extraction, `in2csv`, etc. |

## Recovery Protocol

If all extraction attempts fail:

1. **Stop** and assess: Is this data critical to task completion?
2. **Document** all failed attempts with specific error messages or observations.
3. **Communicate** to user: "Critical data from [filename] could not be extracted despite [N] attempts using [methods]. Please clarify or provide alternative source."
4. **Proceed cautiously**: If task must continue, clearly demarcate any estimated/synthesized data.

*** End Files
*** Add File: examples/extraction_fallback.sh
#!/bin/bash
# Fallback extraction strategies for common file formats
# Use when read_file produces incomplete results

extract_docx_raw() {
    local file="$1"
    # Extract raw XML from docx (docx is a zip archive)
    unzip -p "$file" word/document.xml 2>/dev/null | xmllint --format - 2>/dev/null
}

extract_xlsx_to_csv() {
    local file="$1"
    # Convert Excel to CSV using in2csv (from csvkit)
    if command -v in2csv &>/dev/null; then
        in2csv "$file" 2>/dev/null
    else
        echo "in2csv not available; try python pandas approach"
        return 1
    fi
}

extract_pdf_text() {
    local file="$1"
    # Extract text from PDF using pdftotext
    if command -v pdftotext &>/dev/null; then
        pdftotext "$file" - 2>/dev/null
    else
        echo "pdftotext not available; try PyPDF2 via Python sandbox"
        return 1
    fi
}

extract_raw_strings() {
    local file="$1"
    # Extract printable strings from any binary file
    if command -v strings &>/dev/null; then
        strings "$file" 2>/dev/null | head -500
    else
        echo "strings not available"
        return 1
    fi
}

# Usage: ./extraction_fallback.sh <command> <file>
# Commands: docx_raw, xlsx_csv, pdf_text, raw_strings

case "$1" in
    docx_raw)  extract_docx_raw "$2" ;;
    xlsx_csv)  extract_xlsx_to_csv "$2" ;;
    pdf_text)  extract_pdf_text "$2" ;;
    raw_strings)  extract_raw_strings "$2" ;;
    *)
        echo "Usage: $0 <docx_raw|xlsx_csv|pdf_text|raw_strings> <file>"
        exit 1
        ;;
esac
