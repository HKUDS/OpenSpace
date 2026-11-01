---
name: shell-doc-gen-and-locate
description: Generate documents via shell commands and locate nested outputs using find.
---

# Shell Document Generation and Retrieval Workflow

## Objective
Enable reliable file creation (e.g., PowerPoint, PDFs) when direct API or library-based generation fails, ensuring the final artifact is located and moved to the working directory regardless of nested output paths.

## When to Use
- Standard document generation tools (e.g., direct Python library calls in-memory) fail or are unavailable.
- The `shell_agent` capability is available for executing scripts.
- Generated files may be saved in unpredictable nested directories within the workspace.

## Procedure

### 1. Attempt Shell-Based Generation
If direct tool invocation fails, delegate file creation to a shell-executed script.

- Write a script (e.g., `generate_presentation.py` or `make_doc.sh`) that produces the file.
- Execute the script via the shell agent.
- Ensure the script prints confirmation upon completion.

### 2. Locate the Output File
Do not assume the file is in the current directory. Shell scripts often create outputs in nested folders (e.g., `./workspace/output/`, `./build/`).

- Use the `find` command to search the workspace recursively.
- Example for PowerPoint files:

    find . -type f -name "*.pptx"

- Example for PDF files:

    find . -type f -name "*.pdf"

- Filter results by modification time if multiple versions exist (e.g., `-mmin -10`).

### 3. Retrieve to Working Directory
Once the file path is identified:

- Copy the file to the root working directory or the expected delivery location.

    cp /path/to/nested/file.pptx ./

- Verify the file exists and is non-zero size.

### 4. Verification
- Confirm the file opens correctly or meets size expectations.
- If the file is not found after `find`, check stderr of the generation script for path clues.

## Best Practices
- **Explicit Paths:** Whenever possible, configure generation scripts to output to a known relative path (e.g., `./output/filename.pptx`).
- **Cleanup:** Remove nested temporary copies after retrieving the final version to avoid confusion.
- **Logging:** Echo the final absolute path of the generated file in the script stdout to simplify locating.