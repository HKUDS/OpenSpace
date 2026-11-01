---
name: pdf-to-report-workflow
description: Complete PDF workflow: extract content from source PDFs and generate new PDF reports using command-line tools
---

# PDF-to-Report Generation Workflow

This skill provides a complete end-to-end workflow for processing PDF documents: extracting content from source PDFs, assembling report data, and generating new PDF output files—all using command-line tools when Python libraries are unavailable.

## When to Use This Skill

- Need to extract text/content from existing PDFs
- Need to create new PDF reports from extracted data
- Need to combine multiple PDF sources into a single report
- PyPDF2, reportlab, or similar Python PDF libraries are unavailable
- Working in minimal/containerized environments

## Core Tools

### Extraction Tools (poppler-utils)

#### 1. `pdfinfo` - Extract PDF Metadata

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
- `CreationDate`: When the PDF was created
- `ModDate`: Last modification date

#### 2. `pdftotext` - Extract Text Content

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

### Generation Tools (Choose based on availability)

#### 1. `enscript` + `ps2pdf` - Text to PDF (Recommended)

Convert plain text to PDF via PostScript:

```bash
# Install if needed
apt-get install -y enscript ghostscript

# Convert text file to PDF
enscript -B -o output.ps input.txt && ps2pdf output.ps output.pdf

# One-liner
enscript -B input.txt -o - | ps2pdf - output.pdf
```

**Options:**
- `-B`: No borders
- `-o`: Output file (- for stdout)
- `-f`: Font specification (e.g., `-fCourier10`)

#### 2. `wkhtmltopdf` - HTML to PDF

Convert HTML to PDF with full formatting support:

```bash
# Install if needed
apt-get install -y wkhtmltopdf

# Convert HTML file to PDF
wkhtmltopdf input.html output.pdf

# Convert from stdin
echo "<html><body><h1>Report</h1></body></html>" | wkhtmltopdf - output.pdf

# With options for better quality
wkhtmltopdf --page-size A4 --margin-top 25mm input.html output.pdf
```

#### 3. `libreoffice` - Document Conversion

Convert various document formats to PDF:

```bash
# Install if needed
apt-get install -y libreoffice-writer

# Convert to PDF (headless mode)
libreoffice --headless --convert-to pdf input.docx
libreoffice --headless --convert-to pdf input.odt
libreoffice --headless --convert-to pdf input.txt
```

#### 4. `pandoc` - Universal Document Converter

Convert between many formats including PDF:

```bash
# Install if needed
apt-get install -y pandoc texlive-latex-base

# Convert markdown to PDF
pandoc input.md -o output.pdf

# Convert text to PDF
pandoc input.txt -o output.pdf

# With custom template
pandoc input.md --template=template.tex -o output.pdf
```

#### 5. `pdftk` - PDF Manipulation

Merge, split, or modify existing PDFs:

```bash
# Install if needed
apt-get install -y pdftk

# Merge multiple PDFs
pdftk file1.pdf file2.pdf file3.pdf cat output merged.pdf

# Extract pages
pdftk input.pdf cat 1-3 output extracted.pdf

# Split into individual pages
pdftk input.pdf burst
```

## Complete Workflow

### Phase 1: Check Tool Availability

```bash
# Check extraction tools
which pdfinfo || echo "pdfinfo not found"
which pdftotext || echo "pdftotext not found"

# Check generation tools (at least one should be available)
which enscript || echo "enscript not found"
which wkhtmltopdf || echo "wkhtmltopdf not found"
which libreoffice || echo "libreoffice not found"
which pandoc || echo "pandoc not found"
```

### Phase 2: Install Missing Tools

```bash
# Debian/Ubuntu - Full installation
apt-get update && apt-get install -y poppler-utils enscript ghostscript

# Or install wkhtmltopdf instead
apt-get install -y poppler-utils wkhtmltopdf

# Or install pandoc for markdown-based reports
apt-get install -y poppler-utils pandoc texlive-latex-base
```

### Phase 3: Extract Content from Source PDFs

```bash
# Create working directory
mkdir -p workdir/extracted
cd workdir

# Extract text from each source PDF
for pdf in ../source_pdfs/*.pdf; do
    filename=$(basename "$pdf" .pdf)
    pdftotext -layout "$pdf" "extracted/${filename}.txt"
    echo "Extracted: $filename"
done

# Verify extraction
for txt in extracted/*.txt; do
    lines=$(wc -l < "$txt")
    echo "$txt: $lines lines"
done
```

### Phase 4: Assemble Report Content

```bash
# Create report from extracted content
cat > final_report.txt << 'EOF'
===========================================
NEW CASE CREATION REPORT
Generated: $(date)
===========================================

EOF

# Add sections from each source
echo "SECTION 1: CASE CREATION GUIDE" >> final_report.txt
echo "-------------------------------------------" >> final_report.txt
cat extracted/case_creation_guide.txt >> final_report.txt
echo "" >> final_report.txt

echo "SECTION 2: CASE DETAIL SUMMARY" >> final_report.txt
echo "-------------------------------------------" >> final_report.txt
cat extracted/case_detail_summary.txt >> final_report.txt
echo "" >> final_report.txt

echo "SECTION 3: PATERNITY TEST RESULTS" >> final_report.txt
echo "-------------------------------------------" >> final_report.txt
cat extracted/paternity_test_results.txt >> final_report.txt
echo "" >> final_report.txt

echo "SECTION 4: ORDER OF CHILD SUPPORT" >> final_report.txt
echo "-------------------------------------------" >> final_report.txt
cat extracted/order_of_child_support.txt >> final_report.txt
echo "" >> final_report.txt

echo "===========================================" >> final_report.txt
echo "END OF REPORT" >> final_report.txt
echo "===========================================" >> final_report.txt
```

### Phase 5: Generate PDF Report

**Option A: Using enscript + ps2pdf**

```bash
# Convert text to PDF
enscript -B -fCourier10 -o report.ps final_report.txt && ps2pdf report.ps final_report.pdf

# Or one-liner
enscript -B final_report.txt -o - | ps2pdf - final_report.pdf

# Verify output
pdfinfo final_report.pdf | grep Pages
```

**Option B: Using wkhtmltopdf (with HTML formatting)**

```bash
# Convert text to simple HTML
cat > final_report.html << 'EOF'
<!DOCTYPE html>
<html>
<head>
    <style>
        body { font-family: monospace; margin: 40px; }
        h1 { text-align: center; }
        .section { margin-top: 30px; }
    </style>
</head>
<body>
EOF

# Add content (escape HTML special chars if needed)
sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g' final_report.txt | \
    sed 's/^=\{30,\}/<h1>/; s/$/<\/h1>/; /^<h1>/!s/^/--<br>/; /^----/!s/$/<br>/' >> final_report.html

echo "</body></html>" >> final_report.html

# Convert to PDF
wkhtmltopdf --page-size A4 --margin-top 25mm final_report.html final_report.pdf
```

**Option C: Using pandoc (markdown format)**

```bash
# Create markdown version
cat > final_report.md << 'EOF'
# New Case Creation Report

*Generated: $(date)*

---

EOF

# Add sections with markdown formatting
for txt in extracted/*.txt; do
    filename=$(basename "$txt" .txt)
    echo "## $filename" >> final_report.md
    echo "" >> final_report.md
    cat "$txt" >> final_report.md
    echo "" >> final_report.md
done

# Convert to PDF
pandoc final_report.md -o final_report.pdf
```

### Phase 6: Verify Generated Report

```bash
# Check PDF was created
if [ -f final_report.pdf ]; then
    echo "✓ PDF created successfully"
    
    # Verify page count
    pages=$(pdfinfo final_report.pdf | grep Pages | awk '{print $2}')
    echo "  Pages: $pages"
    
    # Verify file size
    size=$(ls -lh final_report.pdf | awk '{print $5}')
    echo "  Size: $size"
    
    # Verify content
    if pdftotext final_report.pdf - | grep -q "CASE CREATION REPORT"; then
        echo "✓ Content verified"
    else
        echo "✗ Content verification failed"
    fi
else
    echo "✗ PDF generation failed"
    exit 1
fi
```

## Python Integration Example

```python
import subprocess
import os
from datetime import datetime

class PDFReportGenerator:
    def __init__(self, workdir="workdir"):
        self.workdir = workdir
        os.makedirs(workdir, exist_ok=True)
        os.makedirs(f"{workdir}/extracted", exist_ok=True)
    
    def check_tools(self):
        """Check available tools and return best option"""
        tools = {}
        for tool in ['pdfinfo', 'pdftotext', 'enscript', 'ps2pdf', 
                     'wkhtmltopdf', 'pandoc']:
            result = subprocess.run(['which', tool], 
                                   capture_output=True, text=True)
            tools[tool] = result.returncode == 0
        return tools
    
    def extract_pdf(self, pdf_path, output_txt=None):
        """Extract text from PDF"""
        if output_txt is None:
            output_txt = f"{self.workdir}/extracted/{os.path.basename(pdf_path).replace('.pdf', '.txt')}"
        
        result = subprocess.run(
            ['pdftotext', '-layout', pdf_path, output_txt],
            capture_output=True, text=True
        )
        
        if result.returncode != 0:
            raise Exception(f"Extraction failed: {result.stderr}")
        
        return output_txt
    
    def generate_report_pdf(self, text_content, output_pdf):
        """Generate PDF from text content using best available tool"""
        tools = self.check_tools()
        
        # Write content to temp file
        temp_txt = f"{self.workdir}/temp_report.txt"
        with open(temp_txt, 'w') as f:
            f.write(text_content)
        
        if tools.get('enscript') and tools.get('ps2pdf'):
            # Use enscript + ps2pdf
            temp_ps = f"{self.workdir}/temp_report.ps"
            subprocess.run(['enscript', '-B', '-fCourier10', '-o', temp_ps, temp_txt], check=True)
            subprocess.run(['ps2pdf', temp_ps, output_pdf], check=True)
            return output_pdf
        
        elif tools.get('pandoc'):
            # Use pandoc
            temp_md = f"{self.workdir}/temp_report.md"
            with open(temp_md, 'w') as f:
                f.write(f"# Report\n\nGenerated: {datetime.now()}\n\n---\n\n")
                f.write(text_content)
            subprocess.run(['pandoc', temp_md, '-o', output_pdf], check=True)
            return output_pdf
        
        elif tools.get('wkhtmltopdf'):
            # Use wkhtmltopdf
            temp_html = f"{self.workdir}/temp_report.html"
            with open(temp_html, 'w') as f:
                f.write(f"""<!DOCTYPE html>
<html><head><style>body{{font-family:monospace;margin:40px;}}</style></head>
<body><pre>{text_content}</pre></body></html>""")
            subprocess.run(['wkhtmltopdf', temp_html, output_pdf], check=True)
            return output_pdf
        
        else:
            raise Exception("No PDF generation tools available")
    
    def get_pdf_info(self, pdf_path):
        """Get PDF metadata"""
        result = subprocess.run(['pdfinfo', pdf_path], 
                               capture_output=True, text=True)
        info = {}
        for line in result.stdout.split('\n'):
            if ':' in line:
                key, value = line.split(':', 1)
                info[key.strip()] = value.strip()
        return info
    
    def create_report_from_pdfs(self, source_pdfs, output_pdf, report_title="Report"):
        """Complete workflow: extract from multiple PDFs and create report"""
        extracted_texts = []
        
        # Extract from each source
        for pdf in source_pdfs:
            txt = self.extract_pdf(pdf)
            with open(txt, 'r') as f:
                content = f.read()
            extracted_texts.append((os.path.basename(pdf), content))
        
        # Assemble report
        report_content = f"""{'='*50}
{report_title}
Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
{'='*50}

"""
        
        for filename, content in extracted_texts:
            report_content += f"\n{'-'*50}\n"
            report_content += f"SOURCE: {filename}\n"
            report_content += f"{'-'*50}\n\n"
            report_content += content + "\n"
        
        report_content += f"\n{'='*50}\nEND OF REPORT\n{'='*50}\n"
        
        # Generate PDF
        return self.generate_report_pdf(report_content, output_pdf)

# Usage example
if __name__ == "__main__":
    generator = PDFReportGenerator()
    
    source_pdfs = [
        "source_pdfs/case_creation_guide.pdf",
        "source_pdfs/case_detail_summary.pdf",
        "source_pdfs/paternity_test_results.pdf",
        "source_pdfs/order_of_child_support.pdf"
    ]
    
    output = generator.create_report_from_pdfs(
        source_pdfs, 
        "final_report.pdf",
        "NEW CASE CREATION REPORT"
    )
    
    print(f"Report created: {output}")
    print(f"Pages: {generator.get_pdf_info(output).get('Pages', 'Unknown')}")
```

## Common Workflows

| Task | Command Sequence |
|------|------------------|
| Extract single PDF | `pdftotext -layout file.pdf output.txt` |
| Extract multiple PDFs | `for f in *.pdf; do pdftotext -layout "$f" "${f%.pdf}.txt"; done` |
| Text to PDF (enscript) | `enscript -B input.txt -o - \| ps2pdf - output.pdf` |
| Text to PDF (pandoc) | `pandoc input.md -o output.pdf` |
| Merge PDFs | `pdftk file1.pdf file2.pdf cat output merged.pdf` |
| Verify PDF | `pdfinfo file.pdf \| grep Pages` |
| Check PDF content | `pdftotext file.pdf - \| grep -i "keyword"` |

## Troubleshooting

**No PDF generation tools available**
- Install at least one: `apt-get install enscript ghostscript` (simplest)
- Or: `apt-get install pandoc texlive-latex-base` (best formatting)
- Or: `apt-get install wkhtmltopdf` (HTML support)

**`enscript` produces garbled output**
- Check character encoding: `file input.txt`
- Try adding `-r` option for raw output
- Use `-fCourier10` for fixed-width font

**`ps2pdf` produces large files**
- Add compression: `ps2pdf -dPDFSETTINGS=/ebook input.ps output.pdf`
- Or: `ps2pdf -dPDFSETTINGS=/screen input.ps output.pdf` (smaller, lower quality)

**`pdftotext` returns empty output**
- PDF may be image-only (scanned) - requires OCR tools
- PDF may be encrypted/password-protected
- Try `pdftotext -layout` for better extraction

**Report formatting looks poor**
- Use `pandoc` with markdown for better formatting
- Use `wkhtmltopdf` with HTML/CSS for full control
- Add `-layout` flag to `pdftotext` to preserve structure

## Best Practices

1. **Always verify tools before starting** - Check which generation tools are available
2. **Preserve layout during extraction** - Use `pdftotext -layout` for better structure
3. **Test with sample content first** - Generate a test PDF before full report
4. **Validate output PDF** - Check page count and verify content was included
5. **Handle special characters** - Escape HTML entities when using wkhtmltopdf
6. **Clean up temporary files** - Remove intermediate .ps, .html, .txt files after generation
7. **Document tool choices** - Note which generation method was used for reproducibility

## Quick Start Template

```bash
#!/bin/bash
# Quick PDF Report Generation Script

set -e

# Configuration
SOURCE_DIR="${1:-source_pdfs}"
OUTPUT_PDF="${2:-final_report.pdf}"
WORKDIR="workdir_$$"

# Setup
mkdir -p "$WORKDIR/extracted"
trap "rm -rf $WORKDIR" EXIT

# Check tools
for tool in pdfinfo pdftotext; do
    if ! which "$tool" &>/dev/null; then
        echo "ERROR: $tool not found. Install poppler-utils."
        exit 1
    fi
done

# Extract all source PDFs
echo "Extracting PDFs from $SOURCE_DIR..."
for pdf in "$SOURCE_DIR"/*.pdf; do
    [ -f "$pdf" ] || continue
    name=$(basename "$pdf" .pdf)
    pdftotext -layout "$pdf" "$WORKDIR/extracted/${name}.txt"
    echo "  Extracted: $name"
done

# Assemble report
echo "Assembling report..."
{
    echo "========================================"
    echo "REPORT GENERATED: $(date)"
    echo "========================================"
    echo ""
    for txt in "$WORKDIR/extracted"/*.txt; do
        [ -f "$txt" ] || continue
        name=$(basename "$txt" .txt)
        echo "=== $name ==="
        cat "$txt"
        echo ""
    done
    echo "========================================"
    echo "END OF REPORT"
} > "$WORKDIR/report.txt"

# Generate PDF
echo "Generating PDF..."
if which enscript &>/dev/null && which ps2pdf &>/dev/null; then
    enscript -B "$WORKDIR/report.txt" -o - | ps2pdf - "$OUTPUT_PDF"
elif which pandoc &>/dev/null; then
    pandoc "$WORKDIR/report.txt" -o "$OUTPUT_PDF"
else
    echo "ERROR: No PDF generation tool available"
    exit 1
fi

# Verify
echo "Verifying output..."
pages=$(pdfinfo "$OUTPUT_PDF" | grep Pages | awk '{print $2}')
echo "✓ Report created: $OUTPUT_PDF ($pages pages)"
```
