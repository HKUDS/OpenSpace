---
name: pptx-file-validation
description: Validate PowerPoint files using python-pptx when standard file readers fail
---

# PPTX File Validation Workflow

This skill provides a reliable method to validate PowerPoint (.pptx) files when the built-in `read_file` function fails or returns insufficient information.

## When to Use

- `read_file` fails to read a .pptx file
- You need to verify presentation contents (slide count, titles, structure)
- Standard file readers return incomplete or unreadable data

## Step-by-Step Instructions

### Step 1: Detect read_file Failure

Attempt to read the PowerPoint file using `read_file`. If it fails, returns garbled content, or doesn't provide the structural information you need, proceed to Step 2.

### Step 2: Use run_shell with python-pptx

Execute a Python script using `run_shell` with the `python-pptx` library to inspect the presentation:

```bash
python3 -c "
from pptx import Presentation
prs = Presentation('path/to/file.pptx')
print(f'Total slides: {len(prs.slides)}')
for i, slide in enumerate(prs.slides, 1):
    print(f'Slide {i}: {slide.shapes.title.text if slide.shapes.title else \"[No Title]\"}')
"
```

### Step 3: Parse and Validate Output

Analyze the output to verify:
- **Slide count** matches expected number
- **Slide titles** align with required topics
- **Structure** is complete and properly formatted

### Step 4: Report Validation Results

Document the validation findings, including:
- Total number of slides
- List of slide titles
- Any missing or problematic content

## Example Usage

```python
# In your agent workflow
run_shell(command="python3 -c \"from pptx import Presentation; prs = Presentation('output.pptx'); print(f'Slides: {len(prs.slides)}'); [print(f'{i}: {s.shapes.title.text}') for i, s in enumerate(prs.slides, 1) if s.shapes.title]\"")
```

## Prerequisites

Ensure `python-pptx` is available in the environment:

```bash
pip install python-pptx
```

## Benefits

- **Reliable**: Works when built-in readers fail
- **Detailed**: Provides slide-by-slide breakdown
- **Programmatic**: Easy to parse and validate against requirements
- **Lightweight**: No need for PowerPoint software installation