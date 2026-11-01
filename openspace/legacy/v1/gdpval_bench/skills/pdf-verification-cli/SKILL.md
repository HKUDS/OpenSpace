---
name: pdf-verification-cli
description: Verify PDF page count and content using command-line tools when Python libraries unavailable
---

# PDF Verification with Command-Line Tools

When verifying PDF files during task execution, Python libraries like PyPDF2 may not be available in the environment. This skill provides a reliable alternative using standard command-line tools from the `poppler-utils` package.

## When to Use This Skill

- Need to verify PDF page count
- Need to inspect PDF text content
- PyPDF2 or similar Python PDF libraries are unavailable
- Working in minimal/containerized environments

## Core Tools

### 1. `pdfinfo` - Extract PDF Metadata

Use `pdfinfo` to get page count and other metadata:

```bash
# Get full PDF info
pdfinfo document.pdf

# Get only page count
pdfinfo document.pdf | grep Pages

# Extract page count as a number
pdfinfo document.pdf | grep Pages | awk '{print $2}'
```

**Key metadata fields:**
- `Pages`: Number of pages in the PDF
- `Title`: Document title
- `Author`: Document author
- `Creator`: Application that created the PDF
- `Producer`: Application that processed the PDF
- `CreationDate`: When the PDF was created
- `ModDate`: Last modification date

### 2. `pdftotext` - Extract Text Content

Use `pdftotext` to inspect the actual content of the PDF:

```bash
# Extract all text to stdout
pdftotext document.pdf -

# Extract text to a file
pdftotext document.pdf output.txt

# Extract text from specific page range
pdftotext -f 1 -l 3 document.pdf output.txt

# Preserve layout (rough formatting)
pdftotext -layout document.pdf output.txt
```

## Verification Workflow

### Step 1: Check Tool Availability

```bash
# Check if tools are installed
which pdfinfo
which pdftotext

# Or test with --help
pdfinfo --help 2>&1 | head -1
```

### Step 2: Install if Needed

```bash
# Debian/Ubuntu
apt-get update && apt-get install -y poppler-utils

# RHEL/CentOS/Fedora
yum install -y poppler-utils
# or
dnf install -y poppler-utils

# macOS (with Homebrew)
brew install poppler
```

### Step 3: Verify PDF Properties

```bash
# Verify page count matches expected
EXPECTED_PAGES=4
ACTUAL_PAGES=$(pdfinfo document.pdf | grep Pages | awk '{print $2}')

if [ "$ACTUAL_PAGES" -eq "$EXPECTED_PAGES" ]; then
    echo "✓ Page count verified: $ACTUAL_PAGES pages"
else
    echo "✗ Page count mismatch: expected $EXPECTED_PAGES, got $ACTUAL_PAGES"
fi
```

### Step 4: Verify PDF Content

```bash
# Check for required sections/content
pdftotext document.pdf - | grep -i "checklist" && echo "✓ Contains checklist section"
pdftotext document.pdf - | grep -i "references" && echo "✓ Contains references section"

# Count occurrences of key terms
pdftotext document.pdf - | grep -ci "assessment"  # Case-insensitive count
```

## Python Integration Example

```python
import subprocess

def get_pdf_page_count(pdf_path):
    """Get page count using pdfinfo"""
    result = subprocess.run(
        ['pdfinfo', pdf_path],
        capture_output=True,
        text=True
    )
    for line in result.stdout.split('\n'):
        if line.startswith('Pages:'):
            return int(line.split(':')[1].strip())
    return None

def extract_pdf_text(pdf_path):
    """Extract all text from PDF using pdftotext"""
    result = subprocess.run(
        ['pdftotext', pdf_path, '-'],
        capture_output=True,
        text=True
    )
    return result.stdout

def verify_pdf(pdf_path, expected_pages, required_terms):
    """Verify PDF has expected page count and contains required terms"""
    # Check page count
    pages = get_pdf_page_count(pdf_path)
    if pages != expected_pages:
        return False, f"Expected {expected_pages} pages, got {pages}"
    
    # Check content
    text = extract_pdf_text(pdf_path).lower()
    missing = [term for term in required_terms if term.lower() not in text]
    
    if missing:
        return False, f"Missing terms: {missing}"
    
    return True, "PDF verification passed"
```

## Common Use Cases

| Task | Command |
|------|---------|
| Count pages | `pdfinfo file.pdf \| grep Pages` |
| Check if PDF has text | `pdftotext file.pdf - \| head -5` |
| Search for keyword | `pdftotext file.pdf - \| grep -i "keyword"` |
| Extract first page | `pdftotext -f 1 -l 1 file.pdf out.txt` |
| Get PDF title | `pdfinfo file.pdf \| grep Title` |

## Troubleshooting

**`pdfinfo: command not found`**
- Install poppler-utils (see Step 2 above)
- Ensure PATH includes the installation directory

**`pdftotext` returns empty output**
- PDF may be image-only (scanned) - requires OCR
- PDF may be encrypted/password-protected
- Try `pdftotext -layout` for better text extraction

**Page count seems wrong**
- Some PDFs have blank pages counted
- Verify with `pdftotext` to see actual content per page

## Best Practices

1. **Always verify both structure and content** - Page count alone doesn't guarantee content quality
2. **Use case-insensitive searches** - Content may vary in capitalization
3. **Handle errors gracefully** - Tools may fail on corrupted or encrypted PDFs
4. **Combine with file existence checks** - Verify PDF exists before running tools