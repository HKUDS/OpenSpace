---
name: pptx-shell-verify
description: Verify PowerPoint presentation contents using python-pptx via shell when standard file readers fail
---

# PowerPoint Shell Verification

## When to Use This Skill

Use this skill when:
- `read_file` or other built-in tools fail to read PowerPoint (.pptx) files
- You need to verify presentation structure (slide count, titles, content)
- Standard file inspection methods return errors or incomplete data

## Prerequisites

- Python 3.x installed in the execution environment
- `python-pptx` library available (install if needed)

## Step-by-Step Instructions

### Step 1: Attempt Standard File Read

First, try to read the file using standard methods:

```
read_file: path/to/presentation.pptx
```

If this fails or returns unusable data, proceed to Step 2.

### Step 2: Verify python-pptx Availability

Check if the library is installed:

```
run_shell: python -c "import pptx; print('pptx available')"
```

If not available, install it:

```
run_shell: pip install python-pptx
```

### Step 3: Create Verification Script

Create a Python script to extract presentation metadata:

```python
# verify_pptx.py
from pptx import Presentation
import sys
import json

def verify_presentation(filepath):
    try:
        prs = Presentation(filepath)
        data = {
            "slide_count": len(prs.slides),
            "slides": []
        }
        
        for i, slide in enumerate(prs.slides):
            slide_data = {
                "slide_number": i + 1,
                "title": None,
                "shapes_count": len(slide.shapes)
            }
            
            # Extract title if present
            if slide.shapes.title:
                slide_data["title"] = slide.shapes.title.text
            
            data["slides"].append(slide_data)
        
        print(json.dumps(data, indent=2))
        return True
    except Exception as e:
        print(f"ERROR: {str(e)}", file=sys.stderr)
        return False

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python verify_pptx.py <file.pptx>", file=sys.stderr)
        sys.exit(1)
    
    success = verify_presentation(sys.argv[1])
    sys.exit(0 if success else 1)
```

### Step 4: Execute Verification

Run the verification script:

```
run_shell: python verify_pptx.py path/to/presentation.pptx
```

### Step 5: Interpret Results

Expected output format:

```json
{
  "slide_count": 10,
  "slides": [
    {
      "slide_number": 1,
      "title": "Introduction",
      "shapes_count": 3
    },
    ...
  ]
}
```

Use this data to:
- Confirm expected slide count matches
- Verify slide titles are present
- Validate presentation structure before delivery

## Example Validation Checks

```python
# Check slide count matches requirement
assert data["slide_count"] >= expected_min_slides

# Check all slides have titles
for slide in data["slides"]:
    assert slide["title"] is not None, f"Slide {slide['slide_number']} missing title"
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Corrupted file | Verify file was created successfully, check file size |
| Permission denied | Ensure file path is accessible, check read permissions |
| Library not found | Install with `pip install python-pptx` |
| Empty presentation | File may be 0 bytes or incomplete write |

## Best Practices

1. **Always verify after creation**: Run verification immediately after generating a PowerPoint file
2. **Keep verification script reusable**: Store the script for future presentations
3. **Validate before delivery**: Never deliver a presentation without confirming structure
4. **Log verification results**: Keep verification output for audit trails