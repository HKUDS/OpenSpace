---
name: docx-parse-resilient
description: Extract text from DOCX files with shell-primary approach and Python zipfile fallback for maximum reliability
---

# Resilient DOCX Text Extraction

Extract text from Microsoft Word (.docx) files using a robust two-tier approach: shell-based extraction as the primary method, with Python zipfile fallback when shell commands fail or return no output.

## When to Use

- Python environment may lack `python-docx` but `zipfile` module is available (standard library)
- Working in constrained or inconsistent environments (containers, minimal images, CI/CD)
- Shell `unzip` command returns errors or no output
- Need reliable extraction with automatic fallback

## Core Technique

DOCX files are ZIP archives containing XML files. This skill provides two extraction methods:

1. **Primary (Shell)**: `unzip -p` + `sed` for fast extraction
2. **Fallback (Python)**: `zipfile` module for reliable extraction when shell fails

## Step-by-Step Instructions

### 1. Verify the DOCX file exists

```bash
ls -la document.docx
```

### 2. Test shell extraction first (recommended)

Try the shell-based approach:

```bash
unzip -p document.docx word/document.xml 2>/dev/null | sed -e 's/<[^>]*>//g'
```

### 3. Check if shell extraction produced output

Verify the shell method returned content:

```bash
content=$(unzip -p document.docx word/document.xml 2>/dev/null | sed -e 's/<[^>]*>//g')
if [ -z "$content" ]; then
    echo "Shell extraction returned no output, trying Python fallback..."
fi
```

### 4. Use Python zipfile fallback if needed

When shell commands fail or return empty output, use Python's standard `zipfile` module:

```bash
python3 -c "
import zipfile
import sys
import re

try:
    with zipfile.ZipFile('document.docx', 'r') as z:
        content = z.read('word/document.xml').decode('utf-8')
        # Strip XML tags
        text = re.sub(r'<[^>]*>', '', content)
        # Clean whitespace
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        print('\n'.join(lines))
except Exception as e:
    print(f'Error: {e}', file=sys.stderr)
    sys.exit(1)
"
```

### 5. Save extracted text to file

```bash
# Try shell first
unzip -p document.docx word/document.xml 2>/dev/null | \
  sed -e 's/<[^>]*>//g' > output.txt

# Verify output has content
if [ ! -s output.txt ]; then
    # Fallback to Python
    python3 -c "
import zipfile, re
with zipfile.ZipFile('document.docx', 'r') as z:
    content = z.read('word/document.xml').decode('utf-8')
    text = re.sub(r'<[^>]*>', '', content)
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    print('\n'.join(lines))
" > output.txt
fi
```

## Complete Shell Function with Fallback

Add this resilient function to your scripts:

```bash
parse_docx_resilient() {
    local file="$1"
    local output="$2"
    
    if [ ! -f "$file" ]; then
        echo "Error: File not found: $file" >&2
        return 1
    fi
    
    # Primary: Shell extraction
    local content
    content=$(unzip -p "$file" word/document.xml 2>/dev/null | \
        sed -e 's/<[^>]*>//g' | \
        sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' | \
        sed -e '/^$/d')
    
    # Check if shell extraction succeeded
    if [ -n "$content" ]; then
        echo "$content" > "${output:-/dev/stdout}"
        return 0
    fi
    
    # Fallback: Python zipfile
    echo "Shell extraction failed, using Python fallback..." >&2
    python3 -c "
import zipfile, sys, re
try:
    with zipfile.ZipFile('$file', 'r') as z:
        content = z.read('word/document.xml').decode('utf-8')
        text = re.sub(r'<[^>]*>', '', content)
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        print('\n'.join(lines))
except Exception as e:
    print(f'Python extraction failed: {e}', file=sys.stderr)
    sys.exit(1)
" > "${output:-/dev/stdout}" || return 1
}

# Usage examples:
# parse_docx_resilient document.docx          # Output to stdout
# parse_docx_resilient document.docx out.txt  # Output to file
```

## Python Script Alternative

For complex workflows, save as a standalone script:

```python
#!/usr/bin/env python3
"""DOCX text extractor with resilient fallback."""

import zipfile
import sys
import re
import subprocess

def extract_with_shell(filepath):
    """Try shell-based extraction first."""
    try:
        result = subprocess.run(
            ['unzip', '-p', filepath, 'word/document.xml'],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            text = re.sub(r'<[^>]*>', '', result.stdout)
            lines = [l.strip() for l in text.split('\n') if l.strip()]
            return '\n'.join(lines)
    except Exception:
        pass
    return None

def extract_with_python(filepath):
    """Fallback Python zipfile extraction."""
    with zipfile.ZipFile(filepath, 'r') as z:
        content = z.read('word/document.xml').decode('utf-8')
        text = re.sub(r'<[^>]*>', '', content)
        lines = [l.strip() for l in text.split('\n') if l.strip()]
        return '\n'.join(lines)

def parse_docx_resilient(filepath):
    """Extract text with automatic fallback."""
    # Try shell first
    content = extract_with_shell(filepath)
    if content:
        return content, 'shell'
    
    # Fallback to Python
    content = extract_with_python(filepath)
    return content, 'python'

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: parse_docx_resilient.py <file.docx>", file=sys.stderr)
        sys.exit(1)
    
    content, method = parse_docx_resilient(sys.argv[1])
    if content:
        print(f"# Extracted using {method} method", file=sys.stderr)
        print(content)
    else:
        print("Failed to extract text from DOCX", file=sys.stderr)
        sys.exit(1)
```

## Limitations

- Does not preserve formatting, images, or tables structure
- May include some residual XML entity references
- Works best for simple text extraction needs
- DOCX must be a valid Office Open XML format
- Python fallback requires Python 3 with standard library (no external packages)

## Verification

Confirm extraction worked by checking output:

```bash
# Test shell method
parse_docx_resilient document.docx | head -20

# Test with file output
parse_docx_resilient document.docx extracted.txt
wc -l extracted.txt  # Should show line count > 0

# Verify content
grep -c "[a-zA-Z]" extracted.txt  # Should show character content
```

## Troubleshooting

**Shell returns "unknown error" or no output:**
- This is expected in some environments
- The function automatically falls back to Python zipfile
- Check `which unzip` to verify unzip is available

**Python also fails:**
- Verify the file is a valid DOCX: `file document.docx`
- Check if file is corrupted: `unzip -t document.docx`
- Ensure Python 3 is available: `python3 --version`

**File not found errors:**
- Use absolute path or verify working directory
- Check file permissions: `ls -la document.docx`

## Environment Detection

To pre-detect which method to use:

```bash
# Check if unzip is available
if command -v unzip &> /dev/null; then
    echo "Shell method available"
else
    echo "Only Python method available"
fi

# Check if Python 3 is available
if command -v python3 &> /dev/null; then
    echo "Python fallback available"
else
    echo "Warning: No extraction method available!"
fi
```
