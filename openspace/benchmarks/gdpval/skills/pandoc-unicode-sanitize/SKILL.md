---
name: pandoc-unicode-sanitize
description: Pre-process Markdown to replace non-ASCII characters before pandoc PDF conversion
---

# Pandoc Unicode Sanitization for PDF Conversion

## Problem

LaTeX (used by pandoc for PDF output) may fail or produce errors when encountering non-ASCII Unicode characters like currency symbols (₹, €, £), special quotes, or other Unicode characters not supported by the default LaTeX engine.

## Solution

Pre-process Markdown files to replace problematic Unicode characters with ASCII equivalents before running pandoc PDF conversion.

## Steps

### 1. Identify Problematic Characters

Common problematic characters include:
- Currency symbols: ₹, €, £, ¥
- Special quotes: " " ' '
- Arrows: →, ←, ↑, ↓
- Bullets: •, ◦
- Other symbols: ©, ®, ™, ±, ×, ÷, —, –

### 2. Create a Sed Substitution Script

Create a shell script for reusable sanitization:

```bash
#!/bin/bash
# sanitize-for-pdf.sh

sed -e 's/₹/Rs/g' \
    -e 's/€/EUR/g' \
    -e 's/£/GBP/g' \
    -e 's/¥/JPY/g' \
    -e 's/"/"/g' \
    -e 's/"/"/g' \
    -e "s/'/'/g" \
    -e "s/'/'/g" \
    -e 's/→/->/g' \
    -e 's/←/<-/g' \
    -e 's/•/-/g' \
    -e 's/©/(c)/g' \
    -e 's/®/(R)/g' \
    -e 's/™/(TM)/g' \
    -e 's/±/+/ -/g' \
    -e 's/×/x/g' \
    -e 's/÷/\//g' \
    -e 's/—/--/g' \
    -e 's/–/-/g' \
    "$1"
```

### 3. Run Sanitization Before Pandoc

```bash
# Using the script
./sanitize-for-pdf.sh input.md > sanitized.md
pandoc sanitized.md -o output.pdf

# Or inline
cat input.md | sed 's/₹/Rs/g; s/€/EUR/g; s/£/GBP/g' | pandoc -o output.pdf
```

### 4. Alternative: Use iconv for Automatic Transliteration

For broader Unicode coverage:

```bash
iconv -f UTF-8 -t ASCII//TRANSLIT < input.md > sanitized.md
pandoc sanitized.md -o output.pdf
```

### 5. Alternative: Use XeLaTeX or LuaLaTeX Engine

If you want to preserve Unicode characters, use a Unicode-capable engine:

```bash
pandoc input.md -o output.pdf --pdf-engine=xelatex
# or
pandoc input.md -o output.pdf --pdf-engine=lualatex
```

## Best Practices

- Sanitize files before conversion rather than debugging LaTeX errors after
- Keep a mapping of your organization's common symbols and preferred replacements
- Test with a small sample before processing large documents
- Consider using XeLaTeX/LuaLaTeX if Unicode preservation is required
- Document which characters cause issues in your specific environment

## Common Error Signs

Look for these LaTeX errors that indicate Unicode issues:
- `Package inputenc Error: Unicode character ... not set up`
- `LaTeX Error: Unicode character ... cannot be used with pdflatex`
- Missing or garbled characters in the output PDF