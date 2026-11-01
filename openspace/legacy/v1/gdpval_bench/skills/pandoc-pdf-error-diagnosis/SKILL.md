---
name: pandoc-pdf-error-diagnosis
description: Systematic diagnostic workflow for resolving pandoc PDF conversion errors
---

# Pandoc PDF Error Diagnosis Workflow

When pandoc reports an "unknown error" during PDF conversion, follow this systematic diagnostic workflow to identify and resolve the issue.

## Step 1: Verify Pandoc Installation

First, confirm pandoc is installed and check its version:

```bash
pandoc --version
```

This verifies pandoc is available and shows which version and features are supported.

## Step 2: Check Available PDF Engines

Identify which PDF rendering engines are installed on the system:

```bash
which pdflatex xelatex lualatex wkhtmltopdf
```

Common engines and their characteristics:
- **pdflatex**: Fast, widely available, good for simple documents
- **xelatex**: Better Unicode/font support, handles modern fonts well
- **lualatex**: Advanced Lua scripting, modern TeX features
- **wkhtmltopdf**: HTML-to-PDF via WebKit, good for web-styled content

## Step 3: Attempt Conversion with Explicit Engine

Try conversion with an explicit engine specification, starting with xelatex (most forgiving):

```bash
pandoc input.md -o output.pdf --pdf-engine=xelatex
```

**Important**: Capture the full stderr output for diagnosis:

```bash
pandoc input.md -o output.pdf --pdf-engine=xelatex 2>&1 | tee conversion.log
```

## Step 4: Analyze Error Output

Examine the captured stderr for common issues:

| Error Pattern | Likely Cause | Solution |
|---------------|--------------|----------|
| `xelatex not found` | Engine not installed | Install TeX Live or try different engine |
| `Package xyz not found` | Missing LaTeX package | Install missing package or remove feature |
| `Font xyz not found` | Missing font | Install font or change document fonts |
| ` LaTeX Error: File xyz.sty not found` | Missing style file | Install texlive-extra or simplify document |

## Step 5: Fallback Strategy

If the primary engine fails, try alternatives in order:

```bash
# Try pdflatex (most basic)
pandoc input.md -o output.pdf --pdf-engine=pdflatex 2>&1

# Try lualatex (if xelatex failed)
pandoc input.md -o output.pdf --pdf-engine=lualatex 2>&1

# Try wkhtmltopdf (for HTML-heavy content)
pandoc input.md -o output.pdf --pdf-engine=wkhtmltopdf 2>&1
```

## Step 6: Install Missing Dependencies (if possible)

If a specific engine is needed but missing:

```bash
# Debian/Ubuntu
sudo apt-get install texlive-xetex texlive-fonts-recommended

# macOS with Homebrew
brew install --cask mactex-no-gui

# Check what's available
tlmgr install <package-name>
```

## Step 7: Simplify and Retry

If all engines fail, simplify the source document:

1. Remove complex tables or math
2. Replace custom fonts with standard fonts
3. Split large documents into sections
4. Convert to intermediate format first (e.g., HTML then PDF)

## Quick Reference Diagnostic Script

```bash
#!/bin/bash
# pandoc-pdf-diagnose.sh

INPUT="${1:-input.md}"
OUTPUT="${2:-output.pdf}"

echo "=== Pandoc PDF Conversion Diagnosis ==="
echo "Input: $INPUT"
echo "Output: $OUTPUT"
echo ""

echo "1. Pandoc version:"
pandoc --version | head -1
echo ""

echo "2. Available engines:"
for engine in pdflatex xelatex lualatex wkhtmltopdf; do
    if which $engine > /dev/null 2>&1; then
        echo "  ✓ $engine: $(which $engine)"
    else
        echo "  ✗ $engine: not found"
    fi
done
echo ""

echo "3. Attempting conversion with xelatex:"
pandoc "$INPUT" -o "$OUTPUT" --pdf-engine=xelatex 2>&1

if [ $? -eq 0 ]; then
    echo "✓ Conversion successful!"
else
    echo "✗ Conversion failed. Try alternative engines."
fi
```

## When to Use This Skill

- Pandoc reports "unknown error" during PDF conversion
- PDF output is empty or corrupted
- Need to determine which PDF engine works best for a document
- Troubleshooting CI/CD pipeline document generation failures