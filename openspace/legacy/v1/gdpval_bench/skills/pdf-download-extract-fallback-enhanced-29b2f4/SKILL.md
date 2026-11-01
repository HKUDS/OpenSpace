---
name: pdf-extract-create-workflow
description: Complete PDF lifecycle: download, extract, and generate structured documents with reportlab
---

# PDF Extract and Create Workflow

This skill provides a **complete PDF lifecycle workflow** for acquiring PDF documents from web sources or local files, extracting their text content, AND generating new structured PDFs from processed data—with multiple fallback mechanisms throughout.

## Overview

When working with PDFs, you may need to:
1. **Download** PDFs from web sources (with anti-bot protection)
2. **Extract** text content from PDFs (with fallback strategies)
3. **Generate** new PDFs from processed data (with professional formatting)

This workflow ensures maximum success rate through progressive fallback strategies for extraction and templated approaches for generation.

## Entry Point: Determine Your Starting Point

**Before beginning, identify your scenario:**

| Scenario | Start Here | Skip |
|----------|-----------|------|
| PDF already on local disk | Step 2 (Verify File Type) | Step 1 (Download) |
| PDF at a web URL | Step 1 (Download) | None |
| Need to CREATE a PDF from data | Mode C (Generate) | Modes A & B |
| Need to extract AND create | Mode A/B → Mode C | None |

---

## Mode A: Web URL Download

### Step 1: Download PDF with Browser User-Agent

Many PDF hosting sites use JavaScript-based redirects or block automated requests. Use curl with a realistic browser user-agent:

```bash
curl -L -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36" -o output.pdf "URL_HERE"
```

Key flags:
- `-L`: Follow redirects
- `-A`: Set user-agent header to mimic a real browser
- `-o`: Specify output filename

**Additional headers for difficult sites:**
```bash
curl -L \
  -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36" \
  -H "Accept: application/pdf,*/*" \
  -H "Accept-Language: en-US,en;q=0.9" \
  -H "Connection: keep-alive" \
  -o output.pdf "URL_HERE"
```

---

## Mode B: Local File Processing & Extraction

If you already have the PDF file locally, **skip Step 1** and begin here:

### Step 2: Verify File Type Before Parsing

Always validate the downloaded file is actually a PDF before attempting extraction:

```bash
file output.pdf
```

Expected output should contain "PDF document". If not:
- The URL may have redirected to an HTML error page
- The file may be corrupted
- Access may be blocked

### Step 3: Primary Extraction with pdftotext

First attempt extraction using the standard `pdftotext` utility (part of poppler-utils):

```bash
pdftotext output.pdf output.txt
```

If `pdftotext` is not available, install it:
```bash
# Debian/Ubuntu
apt-get update && apt-get install -y poppler-utils

# macOS
brew install poppler

# RHEL/CentOS
yum install -y poppler-utils
```

### Step 4: Fallback to PyMuPDF (fitz)

If `pdftotext` fails or produces poor results, use Python's PyMuPDF library:

```python
import fitz  # PyMuPDF

doc = fitz.open("output.pdf")
text = ""
for page in doc:
    text += page.get_text()
doc.close()

with open("output.txt", "w") as f:
    f.write(text)
```

Install if needed:
```bash
pip install pymupdf
```

### Step 5: Graceful Degradation to Domain Knowledge

If the PDF cannot be accessed or extracted after all attempts:

1. Document the failure mode (network issue, corrupted file, access denied, etc.)
2. Extract any partial content that was successfully retrieved
3. Supplement missing content from established domain knowledge
4. Clearly mark which portions are from source vs. generated from knowledge
5. Provide citations for any claimed requirements or specifications

Example degradation note:
```
NOTE: Source document [URL] was inaccessible due to [reason]. 
Content below combines partial extraction with established domain knowledge 
for [topic]. Verify against official sources when available.
```

---

## Mode C: PDF Generation with ReportLab

After extracting or processing data, generate professional PDFs using Python's reportlab library.

### Installation

```bash
pip install reportlab
```

### Step C1: Basic Document Structure

Create a multi-page PDF with title page, sections, and proper formatting:

```python
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_CENTER, TA_LEFT

def create_structured_pdf(output_path, title, sections):
    """
    Create a structured PDF with title page and sections.
    
    Args:
        output_path: Path for output PDF
        title: Document title
        sections: List of dicts with 'heading' and 'content' keys
    """
    doc = SimpleDocTemplate(
        output_path,
        pagesize=letter,
        rightMargin=72,
        leftMargin=72,
        topMargin=72,
        bottomMargin=72
    )
    
    styles = getSampleStyleSheet()
    story = []
    
    # Title Page
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=24,
        alignment=TA_CENTER,
        spaceAfter=30
    )
    story.append(Paragraph(title, title_style))
    story.append(Spacer(1, 2*inch))
    story.append(PageBreak())
    
    # Content Sections
    heading_style = ParagraphStyle(
        'CustomHeading',
        parent=styles['Heading2'],
        fontSize=16,
        spaceBefore=12,
        spaceAfter=6
    )
    body_style = ParagraphStyle(
        'CustomBody',
        parent=styles['Normal'],
        fontSize=11,
        leading=14,
        spaceAfter=12
    )
    
    for section in sections:
        story.append(Paragraph(section['heading'], heading_style))
        # Handle long text by splitting into paragraphs
        for paragraph in section['content'].split('\n\n'):
            if paragraph.strip():
                story.append(Paragraph(paragraph, body_style))
        story.append(Spacer(1, 0.2*inch))
    
    doc.build(story)
    print(f"PDF created: {output_path}")
```

### Step C2: Adding Tables

For structured data, include tables with proper formatting:

```python
from reportlab.platypus import Table, TableStyle
from reportlab.lib import colors

def create_table(data, col_widths=None):
    """
    Create a formatted table for PDF.
    
    Args:
        data: List of lists (rows x columns)
        col_widths: Optional list of column widths
    """
    table = Table(data, colWidths=col_widths)
    table.setStyle(TableStyle([
        # Header row
        ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 12),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        # Data rows
        ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
        ('TEXTCOLOR', (0, 1), (-1, -1), colors.black),
        ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 1), (-1, -1), 10),
        # Grid
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.lightgrey]),
    ]))
    return table
```

### Step C3: Multi-Section Document Template

Complete example creating an organized document with multiple sections:

```python
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.lib import colors

def create_report_pdf(output_path, title, subtitle, sections, table_data=None):
    """
    Create a complete report PDF with title, sections, and optional tables.
    
    Args:
        output_path: Output PDF path
        title: Main title
        subtitle: Subtitle or date
        sections: List of {'heading': str, 'content': str} dicts
        table_data: Optional list of lists for tables
    """
    doc = SimpleDocTemplate(
        output_path,
        pagesize=letter,
        rightMargin=50,
        leftMargin=50,
        topMargin=50,
        bottomMargin=50
    )
    
    styles = getSampleStyleSheet()
    story = []
    
    # Title Page
    title_style = ParagraphStyle(
        'Title',
        parent=styles['Heading1'],
        fontSize=28,
        alignment=TA_CENTER,
        spaceAfter=20,
        fontName='Helvetica-Bold'
    )
    subtitle_style = ParagraphStyle(
        'Subtitle',
        parent=styles['Normal'],
        fontSize=14,
        alignment=TA_CENTER,
        spaceAfter=50,
        textColor=colors.darkgrey
    )
    
    story.append(Paragraph(title, title_style))
    story.append(Paragraph(subtitle, subtitle_style))
    story.append(PageBreak())
    
    # Content
    heading_style = ParagraphStyle(
        'SectionHeading',
        parent=styles['Heading2'],
        fontSize=16,
        spaceBefore=20,
        spaceAfter=10,
        fontName='Helvetica-Bold',
        textColor=colors.darkblue
    )
    body_style = ParagraphStyle(
        'Body',
        parent=styles['Normal'],
        fontSize=11,
        leading=15,
        spaceAfter=12
    )
    
    for i, section in enumerate(sections):
        story.append(Paragraph(section['heading'], heading_style))
        
        # Split content into paragraphs
        for para in section['content'].split('\n\n'):
            if para.strip():
                # Handle very long paragraphs
                story.append(Paragraph(para, body_style))
        
        # Add table after specific section if provided
        if table_data and i == 0:
            story.append(Spacer(1, 0.3*inch))
            table = Table(table_data)
            table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.darkblue),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.lightgrey]),
            ]))
            story.append(table)
            story.append(Spacer(1, 0.3*inch))
        
        if i < len(sections) - 1:
            story.append(PageBreak())
    
    doc.build(story)
    return output_path

# Example usage
if __name__ == "__main__":
    sections = [
        {
            'heading': 'Section 1: Overview',
            'content': 'This is the first section content...\n\nAdditional paragraph here.'
        },
        {
            'heading': 'Section 2: Details',
            'content': 'Detailed information goes here...'
        }
    ]
    
    table_data = [
        ['Header 1', 'Header 2', 'Header 3'],
        ['Row 1 Col 1', 'Row 1 Col 2', 'Row 1 Col 3'],
        ['Row 2 Col 1', 'Row 2 Col 2', 'Row 2 Col 3'],
    ]
    
    create_report_pdf(
        "output_report.pdf",
        "Report Title",
        "Generated: 2024",
        sections,
        table_data
    )
```

### Step C4: Error Handling for PDF Generation

PDF generation can fail in multiple ways. Handle gracefully:

```python
def safe_pdf_generation(output_path, title, sections, max_retries=3):
    """
    Generate PDF with retry logic and error handling.
    """
    import traceback
    from reportlab.lib.utils import ImageReader
    
    for attempt in range(max_retries):
        try:
            create_report_pdf(output_path, title, sections)
            # Verify file was created
            import os
            if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                print(f"✓ PDF generated successfully: {output_path}")
                return True
            else:
                raise Exception("PDF file empty or not created")
        except Exception as e:
            print(f"Attempt {attempt + 1}/{max_retries} failed: {e}")
            if attempt < max_retries - 1:
                import time
                time.sleep(1)  # Brief delay before retry
            else:
                print(f"PDF generation failed after {max_retries} attempts")
                print(traceback.format_exc())
                # Fallback: create minimal text file
                with open(output_path.replace('.pdf', '.txt'), 'w') as f:
                    f.write(f"Title: {title}\n\n")
                    for section in sections:
                        f.write(f"{section['heading']}\n{section['content']}\n\n")
                return False
```

### Step C5: Best Practices for PDF Generation

1. **Page Breaks**: Insert `PageBreak()` between major sections for readability
2. **Consistent Styling**: Define ParagraphStyle objects once and reuse
3. **Text Wrapping**: ReportLab handles wrapping automatically; split long content with `\n\n`
4. **Margins**: Use at least 50-72 point margins for standard letter size
5. **Font Selection**: Stick to Helvetica, Times-Roman, or Courier for compatibility
6. **File Verification**: Always check the PDF was created and has content > 0 bytes
7. **Error Recovery**: Have a fallback (e.g., .txt output) if PDF generation fails
8. **Memory Management**: For very large documents, build in chunks or use separate files

---

## Complete Workflow Script (Handles Download, Extract, and Generate)

```bash
#!/bin/bash
# pdf-lifecycle-workflow.sh
# Handles URL downloads, local files, and PDF generation

INPUT="$1"
MODE="${2:-extract}"  # extract, generate, or both
OUTPUT_PDF="downloaded.pdf"
OUTPUT_TXT="extracted.txt"
OUTPUT_REPORT="generated_report.pdf"

if [[ "$MODE" == "generate" ]]; then
    echo "Mode: PDF Generation"
    python3 generate_pdf.py
    exit $?
fi

if [[ "$INPUT" =~ ^https?:// ]]; then
    # Mode A: URL download
    PDF_URL="$INPUT"
    echo "Downloading PDF from URL..."
    curl -L -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36" -o "$OUTPUT_PDF" "$PDF_URL"
else
    # Mode B: Local file
    if [ ! -f "$INPUT" ]; then
        echo "ERROR: Local file not found: $INPUT"
        exit 1
    fi
    OUTPUT_PDF="$INPUT"
    echo "Using local file: $INPUT"
fi

# Step 2: Verify file type
echo "Verifying file type..."
if ! file "$OUTPUT_PDF" | grep -q "PDF document"; then
    echo "WARNING: File is not a valid PDF"
    echo "Attempting fallback extraction anyway..."
fi

# Step 3: Try pdftotext
echo "Attempting pdftotext extraction..."
if command -v pdftotext &> /dev/null; then
    if pdftotext "$OUTPUT_PDF" "$OUTPUT_TXT" 2>/dev/null; then
        echo "Extraction successful with pdftotext"
        if [[ "$MODE" == "both" ]]; then
            echo "Proceeding to PDF generation..."
            python3 generate_pdf.py
        fi
        exit 0
    fi
fi

# Step 4: Fallback to PyMuPDF
echo "Falling back to PyMuPDF..."
python3 << 'PYTHON_SCRIPT'
import fitz
import sys

try:
    doc = fitz.open("downloaded.pdf")
    text = ""
    for page in doc:
        text += page.get_text()
    doc.close()
    with open("extracted.txt", "w") as f:
        f.write(text)
    print("Extraction successful with PyMuPDF")
except Exception as e:
    print(f"PyMuPDF failed: {e}")
    sys.exit(1)
PYTHON_SCRIPT

# Step 5: Handle complete failure
if [ $? -ne 0 ]; then
    echo "ERROR: All extraction methods failed."
    echo "ACTION: Generate content from domain knowledge and clearly mark source limitations."
fi

if [[ "$MODE" == "both" ]]; then
    echo "Proceeding to PDF generation with extracted/fallback content..."
    python3 generate_pdf.py
fi
```

---

## Common Failure Modes & Solutions

| Symptom | Cause | Solution |
|---------|-------|----------|
| HTML content in PDF | URL redirected to error page | Check HTTP status, try alternate URL |
| Empty extraction | Password-protected or scanned PDF | Try OCR tools or request accessible version |
| Garbled text | Encoding issues | Try PyMuPDF with different extraction mode |
| Curl blocked | Anti-bot measures | Add more headers, use delay between requests |
| PDF generation fails | Missing fonts or memory | Use standard fonts, build in chunks |
| ReportLab errors | Version incompatibility | Use `pip install --upgrade reportlab` |
| Unknown shell_agent error | Timeout on complex operations | Use direct Python execution instead |

---

## When to Use This Skill

| Mode | Use Case |
|------|----------|
| **Mode A (URL download)** | Downloading regulatory documents from government websites |
| **Mode B (Local file)** | Processing PDFs already saved to disk |
| **Mode C (Generate)** | Creating reports from extracted/processed data |
| **Both (extract + generate)** | Full pipeline: acquire → process → report |

### Specific Scenarios

- Extracting content from technical manuals or handbooks
- Processing PDFs in automated pipelines where reliability matters
- Creating structured reports from multiple data sources
- Generating documentation with consistent formatting
- Any situation where PDF access may be unreliable or restricted
- Producing professional PDFs from text data with tables and sections

---

## Quick Reference: Mode Selection

```
Need to get PDF from web?          → Mode A (Download)
Have PDF file already?             → Mode B (Extract)
Need to CREATE a PDF?              → Mode C (Generate)
Need full pipeline?                → Mode A/B → Mode C
Extraction failed?                 → Step 5 (Domain knowledge fallback)
Generation failed?                 → Step C4 (Error handling + txt fallback)
```
