---
name: pdf-page-verification-correction
description: Iterative workflow to verify PDF page counts and adjust layout parameters until requirements are met
---

# PDF Page Count Verification and Correction

This skill provides a systematic approach to ensure PDFs meet target page count requirements through iterative verification and layout adjustment.

## When to Use

- Creating PDFs with strict page limits (e.g., reports, summaries, maps)
- When initial PDF generation may produce variable page counts
- When layout elements (images, tables, text) can cause unpredictable overflow

## Prerequisites

- Python with PyPDF2 or fitz (PyMuPDF) installed
- PDF generation capability (ReportLab, matplotlib, etc.)

## Workflow Steps

### Step 1: Initial PDF Creation

Generate the PDF with your initial layout parameters:

```python
def create_pdf(output_path, params):
    """Create PDF with given layout parameters"""
    # Your PDF generation logic here
    # params can include: image_size, table_density, font_size, margins
    pass
```

### Step 2: Verify Page Count

Check the generated PDF's page count:

```python
import fitz  # PyMuPDF

def verify_page_count(pdf_path):
    """Return the number of pages in the PDF"""
    doc = fitz.open(pdf_path)
    page_count = len(doc)
    doc.close()
    return page_count

# Alternative with PyPDF2
from PyPDF2 import PdfReader

def verify_page_count_pypdf2(pdf_path):
    reader = PdfReader(pdf_path)
    return len(reader.pages)
```

### Step 3: Compare Against Target

```python
def check_page_requirement(actual, target_max, target_exact=None):
    """
    Check if page count meets requirements
    
    Returns: (meets_requirement, adjustment_needed)
    """
    if target_exact is not None:
        meets = (actual == target_exact)
        direction = "shrink" if actual > target_exact else "expand" if actual < target_exact else None
    else:
        meets = (actual <= target_max)
        direction = "shrink" if actual > target_max else None
    
    return meets, direction
```

### Step 4: Adjust Layout Parameters

If page count exceeds target, adjust one or more parameters:

```python
# Common adjustment strategies
ADJUSTMENT_STRATEGIES = {
    'images': {
        'action': 'reduce_size',
        'param': 'image_scale',
        'step': 0.1,  # Reduce by 10%
        'min': 0.5
    },
    'tables': {
        'action': 'reduce_density',
        'param': 'rows_per_page',
        'step': 2,  # Reduce by 2 rows per page
        'min': 5
    },
    'fonts': {
        'action': 'reduce_size',
        'param': 'font_size',
        'step': 1,  # Reduce by 1pt
        'min': 8
    },
    'margins': {
        'action': 'reduce',
        'param': 'margin_inches',
        'step': 0.1,  # Reduce by 0.1 inches
        'min': 0.3
    }
}

def adjust_params(current_params, direction, strategy='images'):
    """Apply adjustment to parameters"""
    adjusted = current_params.copy()
    strat = ADJUSTMENT_STRATEGIES[strategy]
    
    if direction == 'shrink':
        param = strat['param']
        current_val = adjusted.get(param, 1.0)
        new_val = max(current_val - strat['step'], strat['min'])
        adjusted[param] = new_val
    
    return adjusted
```

### Step 5: Iterative Correction Loop

```python
def create_pdf_with_validation(
    output_path, 
    initial_params, 
    target_max_pages,
    max_iterations=5
):
    """
    Create PDF with iterative page count verification and correction
    
    Args:
        output_path: Where to save the final PDF
        initial_params: Starting layout parameters
        target_max_pages: Maximum allowed pages
        max_iterations: Maximum adjustment attempts
    
    Returns:
        dict with 'success', 'final_page_count', 'iterations', 'final_params'
    """
    params = initial_params.copy()
    
    for iteration in range(max_iterations):
        # Create PDF
        create_pdf(output_path, params)
        
        # Verify
        page_count = verify_page_count(output_path)
        
        # Check requirements
        meets_req, direction = check_page_requirement(
            page_count, target_max_pages
        )
        
        if meets_req:
            print(f"✓ PDF created successfully: {page_count} pages")
            return {
                'success': True,
                'final_page_count': page_count,
                'iterations': iteration + 1,
                'final_params': params
            }
        
        # Adjust and retry
        print(f"Iteration {iteration + 1}: {page_count} pages (need ≤{target_max_pages})")
        params = adjust_params(params, direction)
    
    # Failed to converge
    return {
        'success': False,
        'final_page_count': page_count,
        'iterations': max_iterations,
        'final_params': params,
        'error': f"Failed to meet page requirement after {max_iterations} iterations"
    }
```

## Example Usage

```python
# Initial parameters
params = {
    'image_scale': 1.0,
    'font_size': 10,
    'margin_inches': 0.5,
    'rows_per_page': 15
}

# Create map PDF limited to 1 page
result = create_pdf_with_validation(
    output_path='property_map.pdf',
    initial_params=params,
    target_max_pages=1,
    max_iterations=5
)

if not result['success']:
    # Consider more aggressive adjustments or content reduction
    print(f"Warning: {result['error']}")
```

## Tips

1. **Prioritize adjustments**: Start with less intrusive changes (margins, image scale) before more significant ones (font size, content removal)

2. **Track iteration history**: Log each iteration's parameters and page count to identify which adjustments are most effective

3. **Set reasonable limits**: Don't reduce parameters below readable/viewable thresholds

4. **Consider content-based solutions**: If layout adjustments fail, consider removing or summarizing content

5. **Use multiple strategies sequentially**: Try image reduction first, then table density, then fonts

## Common Adjustment Order

1. Reduce margins (least visible impact)
2. Reduce image sizes
3. Reduce table density / increase pagination
4. Reduce font sizes
5. Remove or summarize content (last resort)