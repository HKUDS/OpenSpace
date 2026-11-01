---
name: docx-shell-parse
description: Extract text from DOCX files using shell commands when python-docx is unavailable
---

# DOCX Shell Parsing Workaround

When you need to read content from Microsoft Word (.docx) files but python-docx or similar libraries are unavailable, use this shell-based approach to extract text reliably.

## When to Use

- Python environment lacks `python-docx` or similar libraries
- You need quick text extraction without installing dependencies
- Working in constrained environments (containers, minimal images, etc.)

## Core Technique

DOCX files are ZIP archives containing XML files. Extract and parse the main document XML:

```bash
unzip -p filename.docx word/document.xml | sed -e 's/<[^>]*>//g'
```

## Step-by-Step Instructions

### 1. Verify the DOCX file exists

```bash
ls -la document.docx
```

### 2. Extract raw XML content

Use `unzip -p` to pipe the document.xml content directly to stdout:

```bash
unzip -p document.docx word/document.xml
```

### 3. Strip XML tags from content

Pipe through `sed` to remove all XML tags:

```bash
unzip -p document.docx word/document.xml | sed -e 's/<[^>]*>//g'
```

### 4. Clean up whitespace (optional)

For cleaner output, remove excessive whitespace and newlines:

```bash
unzip -p document.docx word/document.xml | \
  sed -e 's/<[^>]*>//g' | \
  sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' | \
  sed -e '/^$/d'
```

### 5. Save extracted text to file

```bash
unzip -p document.docx word/document.xml | \
  sed -e 's/<[^>]*>//g' > output.txt
```

## Complete Shell Function

Add this reusable function to your scripts:

```bash
parse_docx() {
    local file="$1"
    if [ ! -f "$file" ]; then
        echo "Error: File not found: $file" >&2
        return 1
    fi
    unzip -p "$file" word/document.xml 2>/dev/null | \
        sed -e 's/<[^>]*>//g' | \
        sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' | \
        sed -e '/^$/d'
}

# Usage: parse_docx document.docx
```

## Limitations

- Does not preserve formatting, images, or tables structure
- May include some residual XML entity references
- Works best for simple text extraction needs
- DOCX must be a valid Office Open XML format

## Verification

Confirm extraction worked by checking output:

```bash
parse_docx document.docx | head -20
```

## Alternative: Extract to Temporary Directory

For more complex parsing needs:

```bash
tmpdir=$(mktemp -d)
unzip document.docx -d "$tmpdir"
cat "$tmpdir/word/document.xml" | sed -e 's/<[^>]*>//g'
rm -rf "$tmpdir"
```