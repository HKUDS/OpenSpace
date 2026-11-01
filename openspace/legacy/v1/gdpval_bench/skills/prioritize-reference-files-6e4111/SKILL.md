---
name: prioritize-reference-files-6e4111
description: Always read and use provided reference files for data before attempting external searches or fabricating information
---

# Prioritize Reference Files

This skill ensures agents correctly prioritize provided reference files over external data sources, preventing the use of fabricated or incorrect information when structured data is already available.

## Core Principle

**When reference files are provided in task context, you MUST read and prioritize them for data before attempting web searches or generating synthetic data.**

## Step-by-Step Instructions

### Step 1: Scan Task Context for Files
Before taking any action, identify all files provided in the task context:
- Look for file attachments, uploads, or references
- Check for common data formats: `.xlsx`, `.csv`, `.json`, `.pdf`, `.docx`, `.txt`
- Note the file names and their apparent purpose

### Step 2: Read Reference Files First
Read all relevant reference files **before** any web searches:
```
# Example: Read provided Excel file
file_content = read_file(file_path="Massabama active listings.xlsx", filetype="xlsx")

# Example: Read provided CSV
file_content = read_file(file_path="data.csv", filetype="csv")

# Example: Read provided PDF
file_content = read_file(file_path="report.pdf", filetype="pdf")
```

**Important: Handling DOCX Read Failures**

If `read_file(filetype='docx')` fails with an error (common issue), use this fallback method:

```bash
# Method 1: Unzip and parse XML directly (docx is a zip archive)
mkdir -p temp_docx && cd temp_docx
unzip -o ../document.docx
# Extract text from word/document.xml
cat word/document.xml | grep -oP '(?<=>)[^<>]+' > extracted_text.txt
```

```python
# Method 2: Use python-docx via shell
run_shell(command="python -c \"from docx import Document; doc = Document('document.docx'); print('\n'.join([p.text for p in doc.paragraphs]))\"")
```

```python
# Method 3: Use shell_agent for robust extraction
extraction_result = shell_agent(task="Extract all text content from document.docx using any reliable method (unzip+XML, python-docx, or pandoc)")
```

After extracting via fallback:
1. Verify the extracted content is complete and readable
2. Proceed to use this extracted data as your reference file content
3. Continue with Step 3 (Extract and Validate Data) using the fallback-extracted content

### Step 3: Extract and Validate Data
- Extract structured data from the reference files
- Verify the data is complete and relevant to your task
- Map the file data to your required output format

### Step 4: Use Reference Data as Primary Source
- Populate outputs using data from reference files
- Cite the reference files when using their data
- Only mark data as "to be verified" if reference files are incomplete

### Step 5: Search Web Only When Necessary
Web searches should only occur when:
- Reference files do not contain the needed information
- You need to verify or supplement reference file data
- The task explicitly requires current/external information

```python
# Decision flow example
if has_reference_file("listings.xlsx"):
    data = extract_from_excel("listings.xlsx")
    # Use this data for output
else:
    # Only then search web
    data = search_web("property listings")
```

## Anti-Patterns to Avoid

❌ **DO NOT** ignore provided files and search the web first
❌ **DO NOT** fabricate data when reference files exist
❌ **DO NOT** assume reference files are irrelevant without reading them
❌ **DO NOT** create placeholder/synthetic data before checking available files

## Checklist

Before completing any data-dependent task:

- [ ] I have identified all files provided in the task context
- [ ] I have read all relevant reference files
- [ ] I have extracted available data from reference files
- [ ] I am using reference file data as my primary source
- [ ] Web searches are only for gaps not covered by reference files
- [ ] I have cited reference files in my output where applicable

## Example Application

**Task**: Create property listings PDF from provided data

**Correct Approach**:
1. Find `Massabama active listings.xlsx` in task context
2. Read the Excel file to extract property addresses, prices, details
3. Use this real data to populate the PDF
4. Only search web if verifying map locations or supplementing missing fields

**Incorrect Approach**:
1. Ignore the Excel file
2. Search web for "Massabama properties"
3. Fabricate placeholder property data
4. Create PDF with synthetic/fabricated information
