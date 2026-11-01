---
name: docx-shell-extract
description: Extract text from DOCX files using shell commands when python-docx is unavailable
---

# DOCX Shell Extraction

## When to Use This Skill

Use this pattern when you need to read or extract text from Microsoft Word (.docx) files in constrained environments where:
- The `python-docx` library is not available
- You cannot install additional Python packages
- You need a quick, reliable shell-based solution

## Core Technique

DOCX files are ZIP archives containing XML files. The main document content is stored in `word/document.xml`. You can extract and parse this using standard shell tools.

## Step-by-Step Instructions

### Step 1: Extract the document.xml content

```bash
unzip -p filename.docx word/document.xml
```

The `-p` flag pipes the content to stdout without extracting to disk.

### Step 2: Strip XML tags to get plain text

```bash
unzip -p filename.docx word/document.xml | sed 's/<[^>]*>//g'
```

This removes all XML tags, leaving the text content.

### Step 3: Clean up whitespace (optional)

For cleaner output, add additional sed processing:

```bash
unzip -p filename.docx word/document.xml | \
  sed 's/<[^>]*>//g' | \
  sed 's/&[^;]*;//g' | \
  sed 's/^[[:space:]]*//' | \
  sed 's/[[:space:]]*$//' | \
  sed '/^$/d'
```

This removes:
- XML tags
- XML entities (like `&amp;`, `&lt;`)
- Leading/trailing whitespace
- Empty lines

### Step 4: Save to a text file (optional)

```bash
unzip -p filename.docx word/document.xml | \
  sed 's/<[^>]*>//g' > output.txt
```

## Complete Example

```bash
# Extract text from a Word document
DOCX_FILE="report.docx"
OUTPUT_FILE="report_text.txt"

unzip -p "$DOCX_FILE" word/document.xml | \
  sed 's/<[^>]*>//g' | \
  sed 's/&[^;]*;//g' | \
  sed '/^$/d' > "$OUTPUT_FILE"

echo "Extracted text saved to $OUTPUT_FILE"
```

## Verification

After extraction, verify the content was captured:

```bash
# Check if output file has content
if [ -s "$OUTPUT_FILE" ]; then
    echo "Successfully extracted $(wc -l < "$OUTPUT_FILE") lines"
    head -5 "$OUTPUT_FILE"
else
    echo "Warning: Output file is empty"
fi
```

## Limitations

- This method extracts raw text without formatting
- Complex layouts, tables, and images are not preserved
- Some special characters may need additional handling
- Works best for text-heavy documents

## Alternatives to Explore

If this approach fails or the DOCX structure differs:
- Check for `word/document.xml` existence: `unzip -l filename.docx | grep document.xml`
- Some documents may use `word/*.xml` with different naming
- Consider `pandoc` if available: `pandoc filename.docx -t plain`