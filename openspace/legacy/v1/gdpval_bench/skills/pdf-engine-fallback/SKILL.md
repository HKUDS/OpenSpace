---
name: pdf-engine-fallback
description: Retry PDF generation with alternative engines when pandoc fails
---

# PDF Engine Fallback

This skill provides a diagnostic-retry workflow for PDF generation when Pandoc's default PDF engine fails. It systematically checks available engines and retries with alternatives optimized for different content types (e.g., xelatex for Unicode).

## When to Use

- Pandoc PDF generation fails with errors
- Document contains non-ASCII characters (Unicode, CJK, etc.)
- Need robust PDF generation across different system configurations
- Working in CI/CD or automated document pipelines

## Steps

### 1. Attempt Initial PDF Generation

First, try generating with pandoc's default engine:

```bash
pandoc input.md -o output.pdf
```

### 2. Diagnose on Failure

If the initial attempt fails, check which PDF engines are available:

```bash
# Check for pdflatex
which pdflatex && echo "pdflatex: available" || echo "pdflatex: not found"

# Check for xelatex (better Unicode support)
which xelatex && echo "xelatex: available" || echo "xelatex: not found"

# Check for wkhtmltopdf (HTML-based rendering)
which wkhtmltopdf && echo "wkhtmltopdf: available" || echo "wkhtmltopdf: not found"
```

### 3. Retry with Alternative Engine

Based on available engines and document content, retry with an appropriate engine:

```bash
# For documents with Unicode/special characters - prefer xelatex
pandoc input.md -o output.pdf --pdf-engine=xelatex

# Alternative: use pdflatex for simple Latin documents
pandoc input.md -o output.pdf --pdf-engine=pdflatex

# For HTML-heavy content or complex layouts
pandoc input.md -o output.pdf --pdf-engine=wkhtmltopdf
```

### 4. Handle Missing Engines

If no PDF engines are available, install one:

```bash
# Ubuntu/Debian - install texlive with xelatex
sudo apt-get install texlive-xetex

# Or install minimal teTeX
sudo apt-get install texlive-latex-base texlive-latex-extra

# macOS with Homebrew
brew install basictex
```

## Complete Fallback Script

```bash
#!/bin/bash
# pdf-fallback.sh - Robust PDF generation with engine fallback

INPUT_FILE="${1:-input.md}"
OUTPUT_FILE="${2:-output.pdf}"

echo "Attempting PDF generation: $INPUT_FILE -> $OUTPUT_FILE"

# Try default engine first
if pandoc "$INPUT_FILE" -o "$OUTPUT_FILE" 2>/dev/null; then
    echo "SUCCESS: PDF generated with default engine"
    exit 0
fi

echo "Default engine failed. Checking available engines..."

# Check and try engines in priority order
if command -v xelatex &>/dev/null; then
    echo "Retrying with xelatex (Unicode support)..."
    if pandoc "$INPUT_FILE" -o "$OUTPUT_FILE" --pdf-engine=xelatex; then
        echo "SUCCESS: PDF generated with xelatex"
        exit 0
    fi
fi

if command -v pdflatex &>/dev/null; then
    echo "Retrying with pdflatex..."
    if pandoc "$INPUT_FILE" -o "$OUTPUT_FILE" --pdf-engine=pdflatex; then
        echo "SUCCESS: PDF generated with pdflatex"
        exit 0
    fi
fi

if command -v wkhtmltopdf &>/dev/null; then
    echo "Retrying with wkhtmltopdf..."
    if pandoc "$INPUT_FILE" -o "$OUTPUT_FILE" --pdf-engine=wkhtmltopdf; then
        echo "SUCCESS: PDF generated with wkhtmltopdf"
        exit 0
    fi
fi

echo "FAILURE: No available PDF engine succeeded"
echo "Install a PDF engine (texlive-xetex recommended) and retry"
exit 1
```

## Usage

```bash
# Make executable
chmod +x pdf-fallback.sh

# Run with defaults (input.md -> output.pdf)
./pdf-fallback.sh

# Run with custom files
./pdf-fallback.sh report.md report.pdf
```

## Engine Selection Guide

| Engine | Best For | Unicode | Speed |
|--------|----------|---------|-------|
| xelatex | Non-Latin scripts, modern fonts | Excellent | Moderate |
| pdflatex | Simple Latin documents | Limited | Fast |
| wkhtmltopdf | HTML/CSS styling | Good | Fast |

## Troubleshooting

- **Font errors**: Install required fonts or use xelatex with system fonts
- **Package errors**: Install missing LaTeX packages via `tlmgr`
- **Large documents**: Try pdflatex for speed, xelatex for compatibility
- **Citation issues**: Ensure pandoc-citeproc or citeproc is installed