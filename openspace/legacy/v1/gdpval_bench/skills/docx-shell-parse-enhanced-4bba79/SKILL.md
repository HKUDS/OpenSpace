---
name: docx-dual-parse
description: Extract text from DOCX files using shell or Python zipfile, with environment-aware fallback
---

# DOCX Dual-Method Text Extraction

Extract text from Microsoft Word (.docx) files using either shell commands or Python's zipfile module, automatically selecting the most reliable method for your environment.

## When to Use

- Need reliable DOCX text extraction in varying environments (containers, sandboxes, minimal images)
- Python environment may lack `python-docx` but has standard library access
- Shell utilities (`unzip`, `sed`) may be unavailable or restricted
- Want environment-aware fallback without manual intervention

## Core Technique

DOCX files are ZIP archives containing XML files. This skill provides two extraction methods:

**Method A (Shell):** `unzip -p` + `sed` for tag stripping
**Method B (Python):** `zipfile` module for archive access + string parsing

## Environment Detection

Before extraction, detect which method will work:

```bash
# Quick shell method test
if unzip -v >/dev/null 2>&1; then
    echo "Shell method available"
else
    echo "Shell method unavailable, try Python"
fi
```

```python
# Quick Python method test
python3 -c "import zipfile; print('Python method available')" 2>/dev/null
```

## Method A: Shell-Based Extraction

Use when `unzip` and `sed` are available and the environment allows shell operations.

### Step-by-Step Instructions

**1. Verify the DOCX file exists**
```bash
ls -la document.docx
```

**2. Extract raw XML content**
```bash
unzip -p document.docx word/document.xml
```

**3. Strip XML tags from content**
```bash
unzip -p document.docx word/document.xml | sed -e 's/<[^>]*>//g'
```

**4. Clean up whitespace (optional)**
```bash
unzip -p document.docx word/document.xml | \
  sed -e 's/<[^>]*>//g' | \
  sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' | \
  sed -e '/^$/d'
```

**5. Save extracted text to file**
```bash
unzip -p document.docx word/document.xml | \
  sed -e 's/<[^>]*>//g' > output.txt
```

### Reusable Shell Function

```bash
parse_docx_shell() {
    local file="$1"
    if [ ! -f "$file" ]; then
        echo "Error: File not found: $file" >&2
        return 1
    fi
    if ! command -v unzip >/dev/null 2>&1; then
        echo "Error: unzip not available" >&2
        return 1
    fi
    unzip -p "$file" word/document.xml 2>/dev/null | \
        sed -e 's/<[^>]*>//g' | \
        sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' | \
        sed -e '/^$/d'
}

# Usage: parse_docx_shell document.docx
```

## Method B: Python Zipfile Extraction

Use when shell method fails or Python environment is more reliable than shell.

### Step-by-Step Instructions

**1. Verify the DOCX file exists**
```bash
ls -la document.docx
```

**2. Run Python extraction via run_shell**
```bash
run_shell 'python3 -c "
import zipfile
import re
with zipfile.ZipFile(\"document.docx\", \"r\") as z:
    content = z.read(\"word/document.xml\").decode(\"utf-8\")
    text = re.sub(r\"<[^>]*>\", \"\", content)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for line in lines:
        print(line)
"'
```

**3. Save to file by redirecting output**
```bash
run_shell 'python3 -c "
import zipfile
import re
with zipfile.ZipFile(\"document.docx\", \"r\") as z:
    content = z.read(\"word/document.xml\").decode(\"utf-8\")
    text = re.sub(r\"<[^>]*>\", \"\", content)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    with open(\"output.txt\", \"w\") as f:
        for line in lines:
            f.write(line + \"\\n\")
"'
```

### Reusable Python Function (via run_shell)

```bash
parse_docx_python() {
    local file="$1"
    local output="$2"
    if [ ! -f "$file" ]; then
        echo "Error: File not found: $file" >&2
        return 1
    fi
    run_shell "python3 -c \"
import zipfile
import re
import sys
try:
    with zipfile.ZipFile(\\'$file\\', \\'r\\') as z:
        content = z.read(\\'word/document.xml\\').decode(\\'utf-8\\')
        text = re.sub(r\\'<[^>]*>\\', \\'\\', content)
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        for line in lines:
            print(line)
except Exception as e:
    print(f\\'Error: {e}\\', file=sys.stderr)
    sys.exit(1)
\""
}

# Usage: parse_docx_python document.docx
# Or to file: parse_docx_python document.docx > output.txt
```

## Unified Dual-Method Function

Automatically tries shell first, falls back to Python if shell fails:

```bash
parse_docx() {
    local file="$1"
    if [ ! -f "$file" ]; then
        echo "Error: File not found: $file" >&2
        return 1
    fi
    
    # Try shell method first
    if command -v unzip >/dev/null 2>&1; then
        result=$(unzip -p "$file" word/document.xml 2>/dev/null | \
            sed -e 's/<[^>]*>//g' | \
            sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' | \
            sed -e '/^$/d')
        if [ -n "$result" ]; then
            echo "$result"
            return 0
        fi
    fi
    
    # Fallback to Python method
    python3 -c "
import zipfile
import re
import sys
try:
    with zipfile.ZipFile('$file', 'r') as z:
        content = z.read('word/document.xml').decode('utf-8')
        text = re.sub(r'<[^>]*>', '', content)
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        for line in lines:
            print(line)
except Exception as e:
    print(f'Error: {e}', file=sys.stderr)
    sys.exit(1)
"
}

# Usage: parse_docx document.docx
```

## Alternative: Extract to Temporary Directory

For complex parsing needs or debugging:

**Shell approach:**
```bash
tmpdir=$(mktemp -d)
unzip document.docx -d "$tmpdir"
cat "$tmpdir/word/document.xml" | sed -e 's/<[^>]*>//g'
rm -rf "$tmpdir"
```

**Python approach:**
```bash
python3 -c "
import zipfile
import tempfile
import os
with zipfile.ZipFile('document.docx', 'r') as z:
    tmpdir = tempfile.mkdtemp()
    z.extractall(tmpdir)
    with open(os.path.join(tmpdir, 'word/document.xml')) as f:
        print(f.read())
"
```

## Verification

Confirm extraction worked:

```bash
# Check output has content
parse_docx document.docx | head -20

# Verify file was created (if saving to file)
ls -la output.txt
wc -l output.txt
```

## Method Selection Guide

| Environment | Recommended Method |
|-------------|-------------------|
| Standard Linux with unzip | Shell (faster, simpler) |
| Container without unzip | Python zipfile |
| Sandboxed execution | Python via execute_code_sandbox or run_shell |
| Minimal/busybox images | Shell if unzip available |
| Unknown/restricted | Use unified `parse_docx` function |

## Limitations

- Does not preserve formatting, images, or table structure
- May include some residual XML entity references (&nbsp;, etc.)
- Works best for simple text extraction needs
- DOCX must be a valid Office Open XML format
- Protected/encrypted DOCX files require additional handling

## Error Handling Tips

1. Always check file existence before parsing
2. Test method availability in the target environment
3. Capture stderr for debugging failed extractions
4. Validate output is non-empty before proceeding
5. Handle XML entity decoding if needed (sed can expand basic entities)
