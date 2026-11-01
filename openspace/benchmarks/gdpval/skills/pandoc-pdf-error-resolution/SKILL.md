---
name: pandoc-pdf-error-resolution
description: Systematic debugging workflow for pandoc PDF conversion errors
---

# Pandoc PDF Error Resolution

This skill provides a step-by-step workflow for diagnosing and resolving "unknown error" messages when using pandoc to generate PDFs.

## When to Use

Use this skill when:
- Pandoc returns an "unknown error" during PDF conversion
- The conversion succeeds for other formats (e.g., markdown, HTML) but fails for PDF
- You need to identify which PDF engine is available and working

## Step-by-Step Procedure

### Step 1: Verify Pandoc Installation

First, confirm pandoc is installed and check its version:

```bash
pandoc --version
```

This confirms pandoc is available and shows the version number. Note that pandoc itself doesn't generate PDFs directly—it delegates to external PDF engines.

### Step 2: Check Available PDF Engines

Identify which PDF engines are installed on the system:

```bash
which pdflatex xelatex lualatex wkhtmltopdf pdfroff 2>/dev/null || echo "Checking individual engines..."
which pdflatex
which xelatex
which lualatex
which wkhtmltopdf
```

Common engines and their characteristics:
- **pdflatex**: Fast, widely available, good for basic documents
- **xelatex**: Better Unicode/font support, recommended for complex documents
- **lualatex**: Advanced typography, Lua scripting support
- **wkhtmltopdf**: HTML-to-PDF conversion, good for web-style documents

### Step 3: Attempt Conversion with Explicit Engine

Retry the conversion with an explicit `--pdf-engine` flag:

```bash
pandoc input.md -o output.pdf --pdf-engine=xelatex
```

Try engines in this order:
1. `xelatex` (best Unicode support)
2. `pdflatex` (most widely available)
3. `lualatex` (if xelatex fails)
4. `wkhtmltopdf` (for HTML-heavy content)

### Step 4: Capture Full stderr Output

Always capture the complete error output before falling back:

```bash
pandoc input.md -o output.pdf --pdf-engine=xelatex 2>&1 | tee pandoc_error.log
```

This preserves the full error message for analysis. Common error patterns:
- **Font errors**: Missing LaTeX packages or fonts
- **Package errors**: Missing LaTeX packages (use `tlmgr install <package>`)
- **Syntax errors**: Issues in the source document

### Step 5: Install Missing Dependencies (if needed)

If errors indicate missing LaTeX packages:

```bash
# For TeX Live
tlmgr install <package-name>

# For Debian/Ubuntu
apt-get install texlive-latex-extra texlive-fonts-recommended

# For macOS with Homebrew
brew install --cask mactex
```

### Step 6: Fallback Strategies

If all engines fail, consider:
1. Convert to HTML first, then use wkhtmltopdf
2. Simplify the document (remove complex formatting)
3. Use an online conversion service
4. Generate PDF from a different format (e.g., DOCX via LibreOffice)

## Example Complete Workflow

```bash
#!/bin/bash
# pandoc-pdf-debug.sh

INPUT_FILE="$1"
OUTPUT_FILE="${INPUT_FILE%.md}.pdf"

echo "=== Pandoc PDF Conversion Debug ==="
echo "Input: $INPUT_FILE"
echo "Output: $OUTPUT_FILE"

# Step 1: Verify pandoc
echo -e "\n[1] Checking pandoc version..."
pandoc --version | head -1

# Step 2: Check engines
echo -e "\n[2] Checking available PDF engines..."
for engine in pdflatex xelatex lualatex wkhtmltopdf; do
    if which "$engine" &>/dev/null; then
        echo "  ✓ $engine: $(which $engine)"
    else
        echo "  ✗ $engine: not found"
    fi
done

# Step 3: Try conversion with xelatex
echo -e "\n[3] Attempting conversion with xelatex..."
pandoc "$INPUT_FILE" -o "$OUTPUT_FILE" --pdf-engine=xelatex 2>&1 | tee /tmp/pandoc_error.log

if [ $? -eq 0 ]; then
    echo "✓ Conversion successful!"
else
    echo "✗ Conversion failed. Check /tmp/pandoc_error.log for details"
    echo "Attempting fallback with pdflatex..."
    pandoc "$INPUT_FILE" -o "$OUTPUT_FILE" --pdf-engine=pdflatex 2>&1
fi
```

## Quick Reference

| Command | Purpose |
|---------|---------|
| `pandoc --version` | Verify pandoc installation |
| `which xelatex` | Check if xelatex engine exists |
| `pandoc file.md -o file.pdf --pdf-engine=xelatex` | Convert with explicit engine |
| `... 2>&1 \| tee error.log` | Capture full stderr output |
| `tlmgr install <pkg>` | Install missing LaTeX package |