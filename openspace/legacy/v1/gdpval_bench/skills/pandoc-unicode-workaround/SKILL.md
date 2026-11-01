---
name: pandoc-unicode-workaround
description: Handle LaTeX Unicode errors in pandoc PDF generation by normalizing special characters to ASCII
---

# Pandoc Unicode Workaround

This skill provides a workflow for generating PDFs from Markdown using pandoc when LaTeX compilation fails due to Unicode character errors.

## When to Use

Use this skill when:
- `pandoc document.md -o document.pdf` fails with LaTeX/Unicode errors
- Error messages mention "Unicode character", "LaTeX Error", or specific characters like ✓, →, —, etc.
- You need to produce PDF output but pandoc's default LaTeX engine cannot handle special characters

## Step-by-Step Procedure

### Step 1: Attempt Initial PDF Generation

```bash
pandoc input.md -o output.pdf
```

If this succeeds, you're done. If it fails with Unicode/LaTeX errors, proceed to Step 2.

### Step 2: Normalize Unicode Characters

Replace problematic Unicode characters with ASCII equivalents. Common replacements:

| Unicode Character | ASCII Replacement | Alternative |
|-------------------|-------------------|-------------|
| ✓ (checkmark)     | [Y] or [X]        | OK, ✓ removed |
| ✗ or ✘ (cross)    | [N]               | FAIL |
| → (arrow right)   | ->                | => |
| ← (arrow left)    | <-                | |
| — (em dash)       | -- or -           | --- |
| – (en dash)       | -                 | |
| • (bullet)        | -                 | * |
| © (copyright)     | (c)               | Copyright |
| ® (registered)    | (R)               | |
| ™ (trademark)     | (TM)              | |
| … (ellipsis)      | ...               | |
| " (smart quotes)  | " or '            | |

### Step 3: Apply Replacements

Option A - Manual edit:
Open the Markdown file and manually replace the characters using find/replace.

Option B - Automated with sed (Linux/Mac):
```bash
sed -i 's/✓/[Y]/g' input.md
sed -i 's/✗/[N]/g' input.md
sed -i 's/→/->/g' input.md
sed -i 's/—/--/g' input.md
```

Option C - Automated with Python:
```python
replacements = {
    '✓': '[Y]',
    '✗': '[N]',
    '→': '->',
    '—': '--',
    '–': '-',
    '…': '...',
    '"': '"',
    '"': '"',
}

with open('input.md', 'r', encoding='utf-8') as f:
    content = f.read()

for orig, repl in replacements.items():
    content = content.replace(orig, repl)

with open('input.md', 'w', encoding='utf-8') as f:
    f.write(content)
```

### Step 4: Regenerate PDF

```bash
pandoc input.md -o output.pdf
```

### Step 5: Verify Output

Check that the PDF was generated successfully and review the content to ensure character replacements are acceptable for your use case.

## Alternative Approaches

If Unicode normalization is not acceptable:

1. Use a different PDF engine:
   ```bash
   pandoc input.md -o output.pdf --pdf-engine=wkhtmltopdf
   ```

2. Use XeLaTeX (better Unicode support):
   ```bash
   pandoc input.md -o output.pdf --pdf-engine=xelatex
   ```

3. Add LaTeX packages for Unicode:
   ```bash
   pandoc input.md -o output.pdf -H header.tex
   ```
   Where header.tex contains:
   ```latex
   \usepackage{fontspec}
   \usepackage{xunicode}
   ```

## Tips

- Keep the original Markdown file before normalization if you need to preserve characters
- Document which characters were replaced for future reference
- Test with a small sample first if working with large documents
- For Word documents (.docx), Unicode issues are less common; this pattern is primarily for PDF generation

## Error Indicators

Common LaTeX Unicode errors to watch for:
- ! LaTeX Error: Unicode character ... not set up for use with LaTeX
- Package inputenc Error: Unicode character ... not set up
- ! Missing character: There is no ... in font ...