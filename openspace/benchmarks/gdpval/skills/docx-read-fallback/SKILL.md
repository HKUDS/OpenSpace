---
name: docx-read-fallback
description: Use run_shell with python-docx as reliable fallback when read_file fails on .docx files
---

# DOCX Read Fallback

When `read_file` or `execute_code_sandbox` fails to read `.docx` files, use `run_shell` with python-docx as a reliable workaround.

## When to Use

- `read_file` fails, times out, or returns errors on `.docx` files
- `execute_code_sandbox` attempts to read the docx fail
- You need to extract text content from a Word document
- Multiple standard approaches have been exhausted

## How to Use

### Basic Text Extraction

```bash
python -c "import docx; doc = docx.Document('path/to/file.docx'); print('\n'.join([p.text for p in doc.paragraphs]))"
```

### Using run_shell Tool

```
run_shell command="python -c \"import docx; doc = docx.Document('path/to/file.docx'); print('\n'.join([p.text for p in doc.paragraphs]))\"" timeout=60
```

### Extract Paragraphs with Indices

```bash
python -c "import docx; doc = docx.Document('file.docx'); [print(f'P{i}: {p.text}') for i, p in enumerate(doc.paragraphs) if p.text.strip()]"
```

### Extract Tables

```bash
python -c "import docx; doc = docx.Document('file.docx'); [[print([[cell.text for cell in row.cells] for row in table.rows]) for table in doc.tables]]"
```

### Extract Headings (by style)

```bash
python -c "import docx; doc = docx.Document('file.docx'); [print(p.text) for p in doc.paragraphs if p.style.name.startswith('Heading')]"
```

## Prerequisites

Ensure python-docx is available:

```bash
python -c "import docx; print('docx available')"
```

If not installed:

```bash
pip install python-docx
```

## Tips

- Use absolute paths to avoid working directory issues
- Set appropriate `timeout` (30-60 seconds for large documents)
- Escape quotes properly when embedding in shell commands
- For large documents, extract content in chunks or filter by paragraph index
- This approach bypasses file type detection issues in read_file

## Example Workflow

1. Try `read_file` on the .docx file
2. If it fails, verify python-docx availability
3. Use `run_shell` with the python-docx extraction command
4. Parse the stdout to get document content
5. Proceed with your analysis using the extracted text