---
name: deliverable-completion
description: Clarifies that file creation tasks are complete when the deliverable is successfully written—no submission step required
---

# Deliverable Completion Protocol

## Purpose

This skill addresses a common misconception during document and file creation tasks: agents sometimes search for or attempt to use a `submit_work`, `finalize`, or similar tool after creating the deliverable. **No such step is required.** Task completion is achieved when the file is successfully created with the required content.

## Core Principle

**File creation = Task completion**

When a task requests you to create a document, report, script, or any file-based deliverable, the task is complete once:
1. The file has been written to disk
2. The file contains the required content
3. The file is in the correct format and location

There is **no additional submission, finalization, or confirmation step** needed.

## Execution Workflow

### Step 1: Create the Deliverable

Use the appropriate file creation method for your task:

```python
# For programmatic file creation
with open('deliverable.docx', 'wb') as f:
    f.write(document_content)
```

```bash
# For shell-based creation
echo "content" > output.txt
```

Or use available tools like `write_file`, `create_file`, etc.

### Step 2: Verify Creation

Confirm the file exists and contains expected content:

```bash
ls -la deliverable.docx
# or
cat output.txt
```

### Step 3: Declare Completion

Once verification passes, **the task is complete**. Do not:
- Search for a `submit_work` tool
- Look for a `finalize_task` function
- Attempt to "upload" or "submit" the file elsewhere
- Add extra confirmation steps

Simply report that the deliverable has been created successfully.

## Common Mistakes to Avoid

| ❌ Incorrect | ✅ Correct |
|-------------|-----------|
| Creating file, then searching for submit tool | Creating file, verifying, declaring done |
| Assuming a finalization API exists | Treating file creation as the final step |
| Adding unnecessary confirmation steps | Completing after successful write |

## Example Task Flow

**Task:** "Create a negotiation strategy document covering BATNA, ZOPA, and timeline."

**Correct Execution:**
1. Write the document content
2. Save as `negotiation_strategy.docx`
3. Verify file exists (~43KB, contains all sections)
4. Report: "Negotiation strategy document created successfully"
5. **Task complete** — no further action needed

## When This Applies

- Document creation (.docx, .pdf, .md, .txt)
- Code file generation (.py, .sh, .js)
- Data exports (.csv, .json, .xlsx)
- Configuration files (.yaml, .toml, .ini)
- Any file-based deliverable

## When This Does NOT Apply

- Tasks explicitly requiring external submission (e.g., "submit to API endpoint")
- Tasks requiring human review/approval workflows
- Tasks where the file is an intermediate step (not the final deliverable)