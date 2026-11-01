---
name: pdf-verification-check
description: Verify generated PDFs for integrity and page count using PyPDF2 before task completion.
---

# PDF Verification Check

Use this skill whenever your task involves generating PDF files. Do not declare task completion until you have programmatically verified that the PDFs exist, are not corrupted, and match the expected page counts.

## When to Use

- Immediately after generating one or more PDF files.
- Before reporting success or moving to the next phase of a task.
- When specific page counts are required (e.g., "1-page summary", "2-page listing").

## Verification Steps

1. **Identify Expected Outputs**: List the file paths and their expected page counts.
2. **Run Verification Script**: Use `run_shell` to execute a Python script using `PyPDF2`.
3. **Validate Results**: Ensure the script exits successfully and reports the correct page counts.
4. **Remediate**: If verification fails, inspect the generation logic and regenerate.

## Verification Script

Use the following Python snippet with `run_shell` to verify PDFs. Adjust the `expected_pages` dictionary to match your task requirements.

```python
import sys
from PyPDF2 import PdfReader

def verify_pdfs(files):
    """
    files: dict mapping filepath to expected_page_count (or None if any count is ok)
    """
    all_valid = True
    for path, expected_count in files.items():
        try:
            reader = PdfReader(path)
            actual_count = len(reader.pages)
            
            if expected_count is not None and actual_count != expected_count:
                print(f"FAIL: {path} has {actual_count} pages, expected {expected_count}")
                all_valid = False
            else:
                print(f"OK: {path} has {actual_count} pages")
                
        except Exception as e:
            print(f"ERROR: Cannot read {path} - {str(e)}")
            all_valid = False
            
    return 0 if all_valid else 1

# Example usage - modify this map for your specific task
files_to_check = {
    "output/listing.pdf": 2,
    "output/map.pdf": 1
}

sys.exit(verify_pdfs(files_to_check))
```

## Integration Example

When using `run_shell`, pass the script as a heredoc or save it temporarily:

```bash
python3 -c "
import sys
from PyPDF2 import PdfReader
# ... insert logic here ...
"
```

## Rules

- **Always Verify**: Never assume a PDF generation tool succeeded without checking the output file.
- **Check Integrity**: Successfully opening the file confirms it is not corrupted/empty.
- **Check Counts**: Page count mismatches often indicate formatting errors or missing content.
- **Fail Fast**: If verification fails, attempt to fix the generation code immediately rather than proceeding.

## Dependencies

Ensure `PyPDF2` is available in the environment. If not, install it first:

```bash
pip install PyPDF2
```