---
name: verify-all-deliverables
description: Ensure all required output files are created and verified before marking a task complete
---

# Verify All Deliverables Before Completion

## Purpose

This skill prevents premature task completion in multi-deliverable workflows by ensuring every required output file is created, verified to exist with non-zero size, and confirmed before declaring `<COMPLETE>`.

## When to Use

Use this skill when a task requires creating multiple output files (e.g., spreadsheet + document, report + summary, data file + visualization).

## Instructions

### Step 1: Identify All Deliverables Upfront

 **CRITICAL CHECKPOINT: Do not proceed to Step 2 until this step produces visible output.**

 Before starting any file creation, you MUST output a complete deliverable list in this exact format:

 ```
 REQUIRED DELIVERABLES CHECKLIST:
 ☐ [filename1.extension] - [brief description]
 ☐ [filename2.extension] - [brief description]
 ☐ [filename3.extension] - [brief description]
 ```

 **This checklist must appear as visible text in your output before executing any file creation commands.** Track completion by updating checkboxes (☐ → ☑) as each file is created.

### Step 2: Create Each Deliverable in Sequence

Execute the creation of each file one at a time. Do not proceed to the next step until the current file creation command has completed.

### Step 3: Verify Each File After Creation

After creating each file, immediately verify it exists and has non-zero size:

```bash
ls -la [filename.extension]
```

Expected output should show file size > 0 bytes. If size is 0 or file is missing, troubleshoot before proceeding.

### Step 4: Final Verification Before Completion

Before declaring `<COMPLETE>`, run a final verification of ALL deliverables:

```bash
ls -la file1.ext file2.ext file3.ext
```

Or use a loop for many files:

```bash
for file in file1.ext file2.ext file3.ext; do
  if [ -s "$file" ]; then
    echo "✓ $file exists ($(stat -c%s "$file") bytes)"
  else
    echo "✗ $file missing or empty"
    exit 1
  fi
done
```

### Step 5: Only Then Declare Complete

Only output `<COMPLETE>` after all files have been verified with non-zero size.

## Common Pitfalls

| Pitfall | Prevention |
|---------|------------|
| Creating only some deliverables | List all deliverables upfront and track completion |
| Not verifying file sizes | Always run `ls -la` after each file creation |
| Declaring complete too early | Final verification step is mandatory |
| Assuming success without checking | Explicitly check each file before proceeding |

## Example Workflow

```
Task: Create tracking spreadsheet and email template document

1. Identify deliverables:
   - June_2025_Declined_Payments_Outreach.xlsx
   - Email_Template.docx

2. Create spreadsheet → verify with ls -la → confirm size > 0

3. Create Word document → verify with ls -la → confirm size > 0

4. Final check: ls -la *.xlsx *.docx → confirm both files present

5. Only then: <COMPLETE>
```
