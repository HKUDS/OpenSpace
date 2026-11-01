---
name: pandoc-pdf-error-recovery
description: Systematic approach to diagnose and recover from pandoc PDF conversion unknown errors
---

# Pandoc PDF Error Recovery

## Overview

When pandoc fails to convert documents to PDF with an "unknown error" or similar vague message, follow this systematic diagnostic and recovery workflow.

## Step-by-Step Procedure

### 1. Verify Pandoc Installation

First confirm pandoc is installed and accessible:

```bash
pandoc --version
```

If this fails, install pandoc before proceeding:
- macOS: `brew install pandoc`
- Linux: `sudo apt-get install pandoc` or `sudo yum install pandoc`
- Windows: Download from https://pandoc.org/installing.html

### 2. Check Available PDF Engines

Identify which PDF rendering engines are available on the system:

```bash
which pdflatex xelatex lualatex wkhtmltopdf context
```

Common engines and their typical use cases:
- **pdflatex**: Standard LaTeX, fast but limited Unicode support
- **xelatex**: Full Unicode support, recommended for most modern documents
- **lualatex**: Lua scripting support, good for complex layouts
- **wkhtmltopdf**: HTML/CSS rendering, good for web-style documents
- **context**: ConTeXt typesetting system

### 3. Attempt Conversion with Explicit Engine

Try conversion with xelatex first (most versatile for general documents):

```bash
pandoc input.md -o output.pdf --pdf-engine=xelatex 2>&1 | tee conversion.log
```

The `2>&1 | tee` pattern captures both stdout and stderr to a log file for analysis.

### 4. Analyze Error Output

If conversion fails, examine the captured log:

```bash
cat conversion.log
```

Common error patterns and solutions:
- **Missing LaTeX packages**: Install with `tlmgr install <package>` or system package manager
- **Font issues**: Specify fonts with `-V mainfont="Font Name"`
- **Missing engine**: Fall back to available engine (go to Step 5)

### 5. Try Alternative Engines

Systematically try other available engines:

```bash
# Fallback to pdflatex
pandoc input.md -o output.pdf --pdf-engine=pdflatex 2>&1 | tee conversion-pdflatex.log

# Or try wkhtmltopdf for HTML-rich content
pandoc input.md -o output.pdf --pdf-engine=wkhtmltopdf 2>&1 | tee conversion-wkhtmltopdf.log

# Or try lualatex for complex documents
pandoc input.md -o output.pdf --pdf-engine=lualatex 2>&1 | tee conversion-lualatex.log
```

### 6. Install Missing Dependencies

If all engines fail or are missing, install required packages:

```bash
# Full TeX Live installation (comprehensive but large)
sudo apt-get install texlive-full

# Or minimal installation with common packages
sudo apt-get install texlive-latex-base texlive-fonts-recommended

# For xelatex font support
sudo apt-get install fonts-noto fonts-dejavu
```

### 7. Simplify and Retry

If errors persist, try conversion with minimal options:

```bash
pandoc input.md -o output.pdf --pdf-engine=xelatex --standalone
```

Remove complex formatting, custom headers, or advanced LaTeX features that may be causing issues.

## Quick Reference Command

Complete diagnostic one-liner:

```bash
pandoc --version && which pdflatex xelatex wkhtmltopdf && pandoc input.md -o output.pdf --pdf-engine=xelatex -v
```

## Tips

- Always capture stderr (`2>&1`) to see the actual error message
- Use `-v` (verbose) flag for more detailed output during troubleshooting
- For non-English documents, xelatex or lualatex are usually required
- HTML/CSS-heavy documents may work better with wkhtmltopdf
- Check system disk space - PDF generation can require significant temporary space