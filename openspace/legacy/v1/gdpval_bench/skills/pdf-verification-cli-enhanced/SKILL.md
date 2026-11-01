---
name: pdf-report-workflow-cli
description: Complete PDF workflow: verify, extract content, assemble reports, and generate output PDFs using command-line tools
---

# PDF Report Generation with Command-Line Tools

When working with PDFs in minimal/containerized environments where Python libraries may be unavailable, this skill provides a complete end-to-end workflow: verify source PDFs, extract content, assemble structured reports, and generate final PDF output.

## When to Use This Skill

- Need to create new PDF reports from existing PDF sources
- Need to verify, extract, and combine PDF content
- Working in environments without Python PDF libraries
- Building document assembly pipelines in CI/CD or containers

## Complete Workflow Overview

```
[Source PDFs] → [Verify] → [Extract] → [Assemble] → [Generate Output PDF]
```

## Phase 1: Tool Availability Check

```bash
# Core extraction tools (from poppler-utils)
which pdfinfo && echo "✓ pdfinfo available" || echo "✗ pdfinfo missing"
which pdftotext && echo "✓ pdftotext available" || echo "✗ pdftotext missing"

# PDF generation tools (choose based on availability)
which pdftk && echo "✓ pdftk available (PDF merging)" || true
which wkhtmltopdf && echo "✓ wkhtmltopdf available (HTML→PDF)" || true
which pandoc && echo "✓ pandoc available (document conversion)" || true
which enscript && echo "✓ enscript available (text→PS)" || true
which ps2pdf && echo "✓ ps2pdf available (PS→PDF)" || true
```

### Install Required Tools

```bash
# Debian/Ubuntu - Core tools
apt-get update && apt-get install -y poppler-utils

# Optional: PDF generation tools (choose based on needs)
apt-get install -y pdftk          # PDF merging/manipulation
apt-get install -y wkhtmltopdf    # HTML to PDF
apt-get install -y pandoc         # Document conversion
apt-get install -y enscript ghostscript  # Text to PDF pipeline

# RHEL/CentOS/Fedora
yum install -y poppler-utils pdftk ghostscript
# or
dnf install -y poppler-utils pdftk ghostscript

# macOS (with Homebrew)
brew install poppler pdftk ghostscript
brew install --cask wkhtmltopdf  # GUI app, includes CLI
```

## Phase 2: Verify Source PDFs

### Check Page Count and Metadata

```bash
# Verify each source PDF exists and is readable
for pdf in source1.pdf source2.pdf source3.pdf; do
    if [ ! -f "$pdf" ]; then
        echo "ERROR: $pdf not found"
        exit 1
    fi
    
    pages=$(pdfinfo "$pdf" | grep Pages | awk '{print $2}')
    echo "$pdf: $pages pages"
done
```

### Validate Content Presence

```bash
# Check that required sections exist in source documents
REQUIRED_TERMS=("checklist" "summary" "references")

for term in "${REQUIRED_TERMS[@]}"; do
    if pdftotext source.pdf - | grep -qi "$term"; then
        echo "✓ Found: $term"
    else
        echo "⚠ Missing: $term"
    fi
done
```

## Phase 3: Extract Content from Source PDFs

### Extract Text to Temporary Files

```bash
# Create working directory
WORK_DIR=$(mktemp -d)
echo "Working directory: $WORK_DIR"

# Extract text from each source PDF
for i in source1.pdf source2.pdf source3.pdf; do
    base=$(basename "$i" .pdf)
    pdftotext -layout "$i" "$WORK_DIR/${base}.txt"
    echo "Extracted: $i → ${base}.txt"
done

# Also extract metadata for reference
pdfinfo source1.pdf > "$WORK_DIR/source1_metadata.txt"
```

### Extract Specific Sections (Optional)

```bash
# Extract only specific page ranges
pdftotext -f 1 -l 3 source1.pdf "$WORK_DIR/source1_pages1-3.txt"

# Extract and filter for specific content
pdftotext source1.pdf - | grep -A 10 "Summary" > "$WORK_DIR/summary_section.txt"
```

## Phase 4: Assemble Report Content

### Create Structured Report (Markdown Format)

```bash
REPORT_MD="$WORK_DIR/report.md"

cat > "$REPORT_MD" << 'REPORT_HEADER'
# New Case Creation Report

**Generated:** $(date '+%Y-%m-%d %H:%M:%S')

---

## Executive Summary

REPORT_HEADER

# Add content from extracted sources
echo "## Case Details" >> "$REPORT_MD"
echo "" >> "$REPORT_MD"
cat "$WORK_DIR/source1.txt" >> "$REPORT_MD"
echo "" >> "$REPORT_MD"

echo "## Supporting Documentation" >> "$REPORT_MD"
echo "" >> "$REPORT_MD"
cat "$WORK_DIR/source2.txt" >> "$REPORT_MD"
echo "" >> "$REPORT_MD"

echo "## References" >> "$REPORT_MD"
echo "" >> "$REPORT_MD"
cat "$WORK_DIR/source3.txt" >> "$REPORT_MD"

echo "Report assembled: $REPORT_MD"
```

### Create HTML Report (Alternative for Better Formatting)

```bash
REPORT_HTML="$WORK_DIR/report.html"

cat > "$REPORT_HTML" << 'HTML_HEADER'
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Case Creation Report</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 40px; }
        h1 { color: #333; border-bottom: 2px solid #333; }
        h2 { color: #666; }
        .section { margin: 20px 0; }
        .metadata { font-size: 0.9em; color: #888; }
    </style>
</head>
<body>
<h1>New Case Creation Report</h1>
<p class="metadata">Generated: HTML_HEADER

date '+%Y-%m-%d %H:%M:%S' >> "$REPORT_HTML"

cat >> "$REPORT_HTML" << 'HTML_MIDDLE'
</p>

<div class="section">
<h2>Case Details</h2>
HTML_MIDDLE

# Convert text to HTML paragraphs (simple approach)
sed 's/^/<p>/; s/$/<\/p>/' "$WORK_DIR/source1.txt" >> "$REPORT_HTML"

cat >> "$REPORT_HTML" << 'HTML_END'
</div>
</body>
</html>
HTML_END

echo "HTML report created: $REPORT_HTML"
```

## Phase 5: Generate Final PDF Output

### Option A: Using pandoc (Recommended if available)

```bash
# Markdown to PDF
pandoc "$REPORT_MD" -o final_report.pdf \
    --pdf-engine=xelatex \
    -V geometry:margin=1in

# Or HTML to PDF
pandoc "$REPORT_HTML" -o final_report.pdf \
    --pdf-engine=webkit2png  # or wkhtmltopdf
```

### Option B: Using wkhtmltopdf (HTML to PDF)

```bash
wkhtmltopdf \
    --page-size A4 \
    --margin-top 20mm \
    --margin-bottom 20mm \
    --margin-left 15mm \
    --margin-right 15mm \
    "$REPORT_HTML" \
    final_report.pdf
```

### Option C: Using enscript + ps2pdf (Text to PDF)

```bash
# Text to PostScript, then to PDF
enscript \
    --media=A4 \
    --font=Courier10 \
    --margins=20:20:20:20 \
    -o "$WORK_DIR/report.ps" \
    "$REPORT_MD"

# PostScript to PDF
ps2pdf "$WORK_DIR/report.ps" final_report.pdf
```

### Option D: Merge Existing PDFs with pdftk

```bash
# If you have multiple PDFs to combine (not convert)
pdftk source1.pdf source2.pdf source3.pdf cat output combined_report.pdf

# Add bookmarks/outline
pdftk source1.pdf dump_data > "$WORK_DIR/bookmarks.txt"
# Edit bookmarks.txt, then:
pdftk source1.pdf update_info "$WORK_DIR/bookmarks.txt" output final_report.pdf
```

## Complete End-to-End Example

```bash
#!/bin/bash
# pdf-report-generator.sh - Complete PDF report generation workflow

set -e

# Configuration
SOURCE_PDFS=("case_guide.pdf" "case_summary.pdf" "test_results.pdf")
OUTPUT_PDF="new_case_report.pdf"
WORK_DIR=$(mktemp -d)

echo "=== PDF Report Generation Workflow ==="
echo "Working directory: $WORK_DIR"

# Phase 1: Verify tools
echo -e "\n[Phase 1] Checking tools..."
for tool in pdfinfo pdftotext; do
    if ! command -v "$tool" &> /dev/null; then
        echo "ERROR: $tool not found. Install poppler-utils."
        exit 1
    fi
done

# Phase 2: Verify source PDFs
echo -e "\n[Phase 2] Verifying source PDFs..."
for pdf in "${SOURCE_PDFS[@]}"; do
    if [ ! -f "$pdf" ]; then
        echo "ERROR: Source PDF not found: $pdf"
        exit 1
    fi
    pages=$(pdfinfo "$pdf" | grep Pages | awk '{print $2}')
    echo "✓ $pdf: $pages pages"
done

# Phase 3: Extract content
echo -e "\n[Phase 3] Extracting content..."
for i in "${!SOURCE_PDFS[@]}"; do
    pdf="${SOURCE_PDFS[$i]}"
    base=$(basename "$pdf" .pdf)
    pdftotext -layout "$pdf" "$WORK_DIR/${base}.txt"
    echo "✓ Extracted: $pdf"
done

# Phase 4: Assemble report
echo -e "\n[Phase 4] Assembling report..."
cat > "$WORK_DIR/report.md" << EOF
# New Case Creation Report

**Generated:** $(date '+%Y-%m-%d %H:%M:%S')

---

EOF

section_num=1
for pdf in "${SOURCE_PDFS[@]}"; do
    base=$(basename "$pdf" .pdf)
    cat >> "$WORK_DIR/report.md" << EOF

## Section $section_num: ${base//_/ }

EOF
    cat "$WORK_DIR/${base}.txt" >> "$WORK_DIR/report.md"
    ((section_num++))
done

echo "✓ Report assembled: $WORK_DIR/report.md"

# Phase 5: Generate PDF
echo -e "\n[Phase 5] Generating PDF..."
if command -v pandoc &> /dev/null; then
    pandoc "$WORK_DIR/report.md" -o "$OUTPUT_PDF" --pdf-engine=xelatex
    echo "✓ Generated with pandoc: $OUTPUT_PDF"
elif command -v enscript &> /dev/null; then
    enscript -o "$WORK_DIR/report.ps" "$WORK_DIR/report.md"
    ps2pdf "$WORK_DIR/report.ps" "$OUTPUT_PDF"
    echo "✓ Generated with enscript+ps2pdf: $OUTPUT_PDF"
else
    echo "⚠ No PDF generator available. Report saved as Markdown: $WORK_DIR/report.md"
    cp "$WORK_DIR/report.md" "./${OUTPUT_PDF%.pdf}.md"
fi

# Cleanup
echo -e "\n[Cleanup]"
echo "Working files preserved in: $WORK_DIR"
echo "=== Complete ==="
```

## Python Integration Example

```python
import subprocess
import tempfile
import os
from pathlib import Path

class PDFReportGenerator:
    """Complete PDF workflow: verify, extract, assemble, generate"""
    
    def __init__(self, work_dir=None):
        self.work_dir = Path(work_dir) if work_dir else Path(tempfile.mkdtemp())
        self.extracted_files = []
    
    def verify_pdf(self, pdf_path):
        """Verify PDF exists and get metadata"""
        result = subprocess.run(
            ['pdfinfo', str(pdf_path)],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            raise ValueError(f"Cannot read PDF: {pdf_path}")
        
        metadata = {}
        for line in result.stdout.split('\n'):
            if ':' in line:
                key, val = line.split(':', 1)
                metadata[key.strip()] = val.strip()
        return metadata
    
    def extract_pdf(self, pdf_path, output_name=None):
        """Extract text from PDF"""
        if output_name is None:
            output_name = Path(pdf_path).stem + '.txt'
        
        output_path = self.work_dir / output_name
        subprocess.run(
            ['pdftotext', '-layout', str(pdf_path), str(output_path)],
            check=True
        )
        self.extracted_files.append(output_path)
        return output_path.read_text()
    
    def assemble_report(self, sections, output_md=None):
        """Assemble extracted content into markdown report"""
        if output_md is None:
            output_md = self.work_dir / 'report.md'
        
        with open(output_md, 'w') as f:
            f.write("# Generated Report\n\n")
            f.write(f"**Created:** {subprocess.check_output(['date']).decode().strip()}\n\n---\n\n")
            
            for i, (title, content) in enumerate(sections, 1):
                f.write(f"## Section {i}: {title}\n\n{content}\n\n")
        
        return output_md
    
    def generate_pdf(self, input_file, output_pdf=None):
        """Generate PDF from markdown or HTML"""
        if output_pdf is None:
            output_pdf = self.work_dir / 'report.pdf'
        
        # Try pandoc first
        if self._command_exists('pandoc'):
            subprocess.run(
                ['pandoc', str(input_file), '-o', str(output_pdf), '--pdf-engine=xelatex'],
                check=True
            )
        # Fall back to enscript + ps2pdf
        elif self._command_exists('enscript'):
            ps_file = self.work_dir / 'report.ps'
            subprocess.run(['enscript', '-o', str(ps_file), str(input_file)], check=True)
            subprocess.run(['ps2pdf', str(ps_file), str(output_pdf)], check=True)
        else:
            raise RuntimeError("No PDF generator available (need pandoc or enscript)")
        
        return output_pdf
    
    def _command_exists(self, cmd):
        return subprocess.run(['which', cmd], capture_output=True).returncode == 0
    
    def full_workflow(self, source_pdfs, output_pdf):
        """Complete workflow: verify → extract → assemble → generate"""
        # Verify and extract
        sections = []
        for pdf in source_pdfs:
            meta = self.verify_pdf(pdf)
            content = self.extract_pdf(pdf)
            sections.append((Path(pdf).stem, content))
        
        # Assemble and generate
        report_md = self.assemble_report(sections)
        self.generate_pdf(report_md, output_pdf)
        
        return Path(output_pdf)

# Usage example
generator = PDFReportGenerator()
final_pdf = generator.full_workflow(
    source_pdfs=['guide.pdf', 'summary.pdf', 'results.pdf'],
    output_pdf='final_report.pdf'
)
print(f"Report generated: {final_pdf}")
```

## Troubleshooting

**No PDF generation tools available**
- Minimum: Use `enscript` + `ps2pdf` (usually available with ghostscript)
- Alternative: Generate HTML report and let browser print to PDF
- Last resort: Output as Markdown for manual conversion

**pdftotext returns empty/garbled output**
- PDF may be image-based (scanned) - requires OCR tools like `tesseract`
- PDF may be encrypted - check with `pdfinfo` for "Encrypted" field
- Try `pdftotext -layout` or `pdftotext -raw` for different extraction modes

**pandoc fails with missing LaTeX**
- Install minimal LaTeX: `apt-get install texlive-latex-base`
- Or use `--pdf-engine=wkhtmltopdf` instead of xelatex
- Or fall back to enscript method

**Report formatting is poor**
- Use HTML intermediate format for better styling control
- Adjust pandoc variables: `-V geometry:margin=1in`
- Add CSS when using wkhtmltopdf

## Best Practices

1. **Verify before processing** - Always check source PDFs exist and are readable before starting the workflow
2. **Use temporary directories** - Keep intermediate files isolated with `mktemp -d`
3. **Preserve extraction context** - Save extracted text with clear naming that maps back to sources
4. **Choose generation tool wisely** - pandoc for best quality, enscript for minimum dependencies
5. **Handle failures gracefully** - Check tool availability at runtime, provide fallbacks
6. **Document the workflow** - Include generation timestamp and source list in output

## Tool Comparison

| Tool | Purpose | Pros | Cons |
|------|---------|------|------|
| pdfinfo | Metadata extraction | Fast, reliable | Metadata only |
| pdftotext | Text extraction | Preserves structure | Struggles with complex layouts |
| pandoc | Document conversion | Best quality, flexible | Requires LaTeX for PDF |
| wkhtmltopdf | HTML→PDF | Great styling support | Larger dependency |
| enscript | Text→PS | Minimal dependencies | Basic formatting |
| pdftk | PDF manipulation | Powerful merging | Not for content generation |
*** End Files
