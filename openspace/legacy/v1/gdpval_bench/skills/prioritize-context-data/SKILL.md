---
name: prioritize-context-data
description: Ensures agents check and use provided context files for data before attempting external searches.
category: workflow
---

# Prioritize Context Data

## Objective
Prevent data hallucination and inefficiency by mandating that agents inspect and utilize provided reference files (CSV, XLSX, PDF, TXT, etc.) before attempting web searches or generating synthetic data.

## Critical Rule
**If a reference file is provided in the task context, it is the source of truth.** Do not fabricate data or search the web for information that exists within the provided attachments.

## Workflow Steps

### 1. Scan Context for Attachments
At the start of every task, explicitly list all files provided in the context window or attachment panel.
- Check for spreadsheets (`.xlsx`, `.csv`), documents (`.pdf`, `.docx`), or data dumps (`.json`, `.txt`).
- Note the filename and inferred content type.

### 2. Evaluate Relevance
Determine if any provided file contains the data required to complete the task.
- **Match Keywords:** Do filenames or column headers match task requirements?
- **Check Scope:** Does the data cover the necessary timeframe or地域 (region)?

### 3. Extract Data First
If relevant files are found:
- Read the file content using appropriate tools (e.g., `read_file`, `pandas`, `pdf_reader`).
- Extract the specific data points needed.
- **Do not** proceed to web search until you have confirmed the file lacks the necessary information.

### 4. Cite Source Explicitly
When presenting data in the final output:
- Explicitly state which file the data came from.
- Example: "According to `Massabama_active_listings.xlsx`..."
- This verifies to the user that real data was used, not hallucinated.

### 5. Fallback to Search (Only if Necessary)
If the provided files do not contain the specific data needed:
- State clearly what was missing from the files.
- Then proceed with web search or estimation.
- Mark any non-file data as "External Search" or "Estimated".

## Checklist

- [ ] Did I list all attached files?
- [ ] Did I open and read the relevant files?
- [ ] Did I verify the data exists in the files before searching?
- [ ] Did I cite the file name in my output?
- [ ] Did I avoid fabricating numbers that should have come from the file?

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
- Load the Excel file.
- Count rows and sum prices directly from the sheet.
- Output: "Based on the 50 entries in `Massabama_active_listings.xlsx`, the total value is..."
- **Result:** Accurate data, verified source.

## Warnings

- **Hallucination Risk:** Ignoring provided files is a primary cause of data fabrication.
- **Efficiency:** Reading a local file is faster and more reliable than web scraping.
- **User Expectation:** Users attach files expecting them to be used. Ignoring them is a failure of instruction following.