---
name: pptx-validation-fallback
description: Validate PowerPoint files using python-pptx when read_file fails
---

# PowerPoint Validation Fallback

## When to Use

Use this skill when `read_file` fails to properly read or validate PowerPoint (.pptx) files. This provides a reliable alternative for verifying presentation contents including slide count, titles, and structure.

## Procedure

### Step 1: Check if read_file Failed

If `read_file` with filetype `pptx` returns errors, incomplete content, or cannot extract slide information, proceed to the fallback method.

### Step 2: Use run_shell with python-pptx

Execute Python code via `run_shell` using the `python-pptx` library to inspect the presentation:

```python
from pptx import Presentation

# Load the presentation
prs = Presentation('path/to/file.pptx')

# Get basic info
slide_count = len(prs.slides)
print(f"Total slides: {slide_count}")

# Extract slide titles
for i, slide in enumerate(prs.slides):
    if slide.shapes.title:
        title = slide.shapes.title.text
        print(f"Slide {i+1}: {title}")
    else:
        print(f"Slide {i+1}: [No title]")
```

### Step 3: Validate Structure

Check that the presentation meets expected requirements:

```python
from pptx import Presentation

prs = Presentation('path/to/file.pptx')

# Validation checks
expected_slides = 10  # Adjust based on requirements
actual_slides = len(prs.slides)

if actual_slides >= expected_slides:
    print(f"✓ Slide count OK: {actual_slides} slides")
else:
    print(f"✗ Insufficient slides: {actual_slides}/{expected_slides}")

# Check for content on each slide
for i, slide in enumerate(prs.slides):
    has_title = slide.shapes.title is not None
    has_content = len(slide.shapes) > 1
    print(f"Slide {i+1}: title={has_title}, content={has_content}")
```

### Step 4: Extract Detailed Content (Optional)

For deeper validation, extract text content from all shapes:

```python
from pptx import Presentation

prs = Presentation('path/to/file.pptx')

for i, slide in enumerate(prs.slides):
    print(f"\n=== Slide {i+1} ===")
    for shape in slide.shapes:
        if hasattr(shape, "text") and shape.text.strip():
            print(shape.text)
```

## Expected Output

Successful validation should provide:
- Confirmed slide count
- List of slide titles
- Verification that each slide has expected content structure

## Common Use Cases

- Verifying generated presentations have correct number of slides
- Confirming slide titles match required topics
- Validating presentation structure before delivery
- Debugging why read_file cannot parse a .pptx file

## Dependencies

The `python-pptx` library must be available in the execution environment. Most Python environments support it via pip:

```bash
pip install python-pptx
```