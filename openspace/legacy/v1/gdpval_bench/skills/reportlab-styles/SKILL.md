---
name: reportlab-styles
description: Use reportlab's pre-defined styles from getSampleStyleSheet() correctly
---

# ReportLab Pre-Defined Styles Reference

## Overview

ReportLab's `reportlab.lib.styles.getSampleStyleSheet()` provides a collection of pre-defined paragraph styles. Use these existing styles rather than redefining them to avoid `KeyError` exceptions.

## Available Pre-Defined Styles

The standard stylesheet includes these commonly-used styles:

| Style Name | Purpose |
|------------|---------|
| `Normal` | Default body text |
| `Title` | Document title (large, bold) |
| `Heading1` | Level 1 section heading |
| `Heading2` | Level 2 section heading |
| `Heading3` | Level 3 section heading |
| `Heading4` | Level 4 section heading |
| `Heading5` | Level 5 section heading |
| `Heading6` | Level 6 section heading |
| `Bullet` | Bulleted list items |
| `Definition` | Definition list terms |
| `Italic` | Italicized text |
| `Code` | Monospace code text |

## Correct Usage Pattern

```python
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph

# Get the stylesheet
styles = getSampleStyleSheet()

# Use existing styles directly (CORRECT)
title = Paragraph("My Document Title", styles['Title'])
heading = Paragraph("Section Header", styles['Heading1'])
body = Paragraph("Regular text content", styles['Normal'])
```

## Common Mistake to Avoid

```python
# WRONG - Don't redefine existing style names
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

styles = getSampleStyleSheet()

# This will cause KeyError when you try to use 'Title' later
styles.add(ParagraphStyle(
    name='Title',  # Overwrites the pre-defined 'Title' style
    fontSize=24,
    # ... incomplete definition
))

# Later code expecting the full 'Title' style will fail
paragraph = Paragraph("Title Text", styles['Title'])  # KeyError!
```

## Best Practices

1. **Use existing styles as-is** when they meet your needs
2. **Create new style names** for custom styles (e.g., `MyCustomTitle`, `CustomHeading`)
3. **Base custom styles on existing ones** when you need modifications:

```python
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

styles = getSampleStyleSheet()

# Create a NEW style with a unique name
custom_title = ParagraphStyle(
    name='CustomTitle',  # Unique name, doesn't conflict
    parent=styles['Title'],  # Base it on existing Title
    fontSize=28,
    spaceAfter=30
)
styles.add(custom_title)

# Now both styles are available
styles['Title']       # Original pre-defined style
styles['CustomTitle'] # Your custom variant
```

## Quick Reference Checklist

- [ ] Import `getSampleStyleSheet()` from `reportlab.lib.styles`
- [ ] Call `styles = getSampleStyleSheet()` once at the start
- [ ] Access styles via `styles['StyleName']` (case-sensitive)
- [ ] Do NOT add styles with names that already exist in the stylesheet
- [ ] Use unique names for any custom styles you create
- [ ] Common available styles: `Normal`, `Title`, `Heading1-6`, `Bullet`, `Code`, `Italic`