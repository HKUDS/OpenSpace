---
name: prioritize-reference-files
description: Ensures agents read and use provided reference files before searching or fabricating data
---

# Prioritize Reference Files

## Purpose

This skill ensures that when reference files are provided in task context, you MUST read and extract data from them FIRST before attempting web searches or generating synthetic data. Ignoring available structured data leads to fabricated outputs and incorrect results.

## Core Principle

**Reference files in context > Web search > Data fabrication (never)**

## Workflow

### Step 1: Scan Task Context for Reference Files

Before taking any action, identify all files provided in the task context:
- Look for file attachments, uploads, or references in the task description
- Common formats: `.xlsx`, `.csv`, `.json`, `.pdf`, `.docx`, `.txt`
- Check for phrases like "attached", "provided", "reference file", "see file"

### Step 2: Read Reference Files First

Use the appropriate tool to read each reference file:

```python
# For Excel files
read_file(filetype="xlsx", file_path="path/to/file.xlsx")

# For CSV files  
read_file(filetype="csv", file_path="path/to/file.csv")

# For PDF files
read_file(filetype="pdf", file_path="path/to/file.pdf")

# For JSON files
read_file(filetype="json", file_path="path/to/file.json")

# For text files
read_file(filetype="txt", file_path="path/to/file.txt")
```

**If read_file fails on .docx files** (returns error, empty content, or 'unknown error'):

**Fallback Approach 1: Direct zipfile/XML extraction via run_shell**
```bash
# .docx files are ZIP archives containing XML; extract document.xml directly
unzip -p path/to/file.docx word/document.xml | grep -oP '(?<=<w:t>)[^<]+' | tr '\n' ' '
```
Or for more complete extraction:
```bash
mkdir -p /tmp/docx_extract && cd /tmp/docx_extract && unzip path/to/file.docx && cat word/document.xml
```

**Fallback Approach 2: Use shell_agent for complex extraction**
If direct extraction fails, delegate to shell_agent:
```
shell_agent(task="Extract text content from path/to/file.docx using zipfile and XML parsing")
```
The agent will attempt multiple extraction methods and report results.

**Fallback Approach 3: Verify extraction success before proceeding**
After any extraction method, confirm content was retrieved:
- Check that output is non-empty and contains expected document structure
- Look for familiar text from the document (headings, known phrases)
- If extraction appears incomplete, try an alternative method
- Document which method succeeded: "Content extracted using [method] after read_file failed"

**Important**: Never proceed to data fabrication if reference files exist but read_file fails. Always attempt at least one fallback extraction method first.

### Step 3: Extract and Validate Data

After reading:
1. Parse the content to understand the data structure
2. Identify relevant fields/columns for your task
3. Verify the data is complete and usable
4. Note any limitations or missing information

### Step 4: Use Reference Data as Primary Source

- Base all outputs on the reference file data
- Only supplement with web search if reference data is incomplete
- NEVER fabricate data when reference files exist
- If reference data is insufficient, explicitly state what's missing

### Step 5: Document Data Source

In your outputs, acknowledge the source:
- "Data sourced from [filename]"
- "Based on provided reference file: [filename]"
- This confirms you used the actual provided data

## Anti-Patterns to Avoid

❌ **Ignoring reference files** and searching the web instead
❌ **Fabricating data** when structured data is available
❌ **Assuming file contents** without reading them
❌ **Using outdated web data** when current reference files exist
❌ **Giving up after read_file fails** without trying fallback extraction methods

## Example

**Task Context**: "Create a property listings report. See Massabama_active_listings.xlsx for current data."

**Correct Approach**:
```
1. Read Massabama_active_listings.xlsx first
2. Extract property addresses, prices, specifications
3. Generate report using actual listing data
4. Note: "Data sourced from Massabama_active_listings.xlsx"
```

**Incorrect Approach**:
```
1. Search web for "Massabama property listings"
2. Fabricate property data from search results
3. Create report with unverified/generated data
```

## Verification Checklist

Before completing any task with reference files:

- [ ] I have identified all reference files in the task context
- [ ] I have read each reference file using appropriate tools
- [ ] I have extracted relevant data from the files
- [ ] My output is based on the reference file data
- [ ] I have documented the data source in my output

## When Web Search is Appropriate

Only search the web when:
- Reference files don't exist for the required data
- Reference files are incomplete AND supplemental info is needed
- You need to verify or update time-sensitive information
- You explicitly state what the reference files lack

---
