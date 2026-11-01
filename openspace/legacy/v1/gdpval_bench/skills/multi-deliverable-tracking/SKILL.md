---
name: multi-deliverable-tracking
description: Track and complete all required deliverables before finishing a task
---

# Multi-Deliverable Task Completion

## Purpose
Ensure ALL required outputs are completed before concluding a task, preventing premature task termination when multiple deliverables are required. **This skill includes mandatory enforcement steps that MUST be followed.**

## Core Workflow

### Step 1: Identify All Deliverables Upfront
**CRITICAL: You MUST complete this step before producing ANY deliverable or taking any other action.**

At task start, explicitly enumerate EVERY required output:
- Parse task requirements carefully for action verbs: "create", "generate", "produce", "deliver", "write", "send"
- Count distinct deliverables (files, reports, emails, code modules, analyses, etc.)
- Document this list visibly in working notes
- Note any dependencies between deliverables

**MANDATORY OUTPUT FORMAT:** Before any other work, output this exact format:
```
DELIVERABLES_IDENTIFIED:
1. [filename/description] - [brief description]
2. [filename/description] - [brief description]
...
TOTAL_DELIVERABLES: N
```

**Example identification:**
```
Required Deliverables:
1. [ ] Email to Juvoxa CEO
2. [ ] Follow-up analysis report
Total: 2 deliverables
```

### Step 2: Create a Completion Tracker
**CRITICAL: This tracker MUST be created and visible throughout task execution.**

Maintain a visible checklist throughout task execution:
```
DELIVERABLE TRACKER
===================
[ ] Deliverable 1: [brief description]
[ ] Deliverable 2: [brief description]
[ ] Deliverable 3: [brief description]

Completed: 0/3
```

Update this tracker AFTER each deliverable is fully completed (not just started).

### Step 2.5: Filesystem Verification [MANDATORY BEFORE COMPLETION]
Before considering any deliverable complete, verify it exists:
- For file deliverables: Use `list_dir` or `run_shell` (ls/cat) to confirm the file exists
- For output deliverables: Verify the output was actually produced in the response stream
- Document verification with: `[✓] filename verified in filesystem`

### Step 3: Work Through Deliverables Systematically
- Complete each deliverable fully before marking it done
- Update the tracker immediately after completion
- If a deliverable fails, note the failure but continue to others if possible
- Return to failed items after completing others

### Step 4: Final Verification Before Stopping
Before concluding the task, run this mandatory checklist:
**CRITICAL ENFORCEMENT: You are BLOCKED from outputting <COMPLETE> until this verification passes.**

```
FINAL VERIFICATION (REQUIRED BEFORE <COMPLETE>)
==================
1. Review original task requirements
2. Count total deliverables identified: ___
3. Count deliverables marked complete: ___
4. Do counts match? YES/NO
5. If NO: Identify missing deliverables and complete them
6. If YES: Verify each deliverable exists and meets quality standards
7. Filesystem verification (REQUIRED):
   - [ ] file1.ext: VERIFIED (list_dir/run_shell confirmed)
   - [ ] file2.ext: VERIFIED (list_dir/run_shell confirmed)

**BLOCKING RULE:** If counts do not match OR any file cannot be verified in the filesystem, you MUST:
1. NOT output <COMPLETE>
2. Identify the missing/unverified deliverables
3. Complete and verify them
4. Re-run this verification checklist

**Only output <COMPLETE> when all deliverables are verified present in the filesystem or output stream.**
```

**Only stop when all deliverables are verified complete.**

### Step 5: Self-Check Questions
Before marking task complete, ask:
- "Did I create EVERY output the task requested?"
- "Are there any deliverables I started but didn't finish?"
- "Would someone reviewing my work see all required outputs?"
- "Have I verified EACH deliverable exists in the filesystem using list_dir or run_shell?"
- "Is my DELIVERABLES_IDENTIFIED output present and accurate?"

## Common Pitfalls to Avoid

❌ **Stopping after first deliverable** - Just because one output is done doesn't mean the task is complete
❌ **Stopping after first deliverable** - Just because one output is done doesn't mean the task is complete

❌ **Outputting <COMPLETE> without filesystem verification** - You must verify files exist before completion

❌ **Skipping DELIVERABLES_IDENTIFIED output** - This mandatory output must appear before any work begins

❌ **Assuming implicit completion** - Don't assume related outputs are "part of" a main deliverable unless explicitly stated

❌ **Losing track mid-task** - The tracker must stay visible and updated throughout

❌ **Counting drafts as complete** - Only mark done when the deliverable meets quality standards

## Code Example: Tracker Template

```python
# Deliverable tracking template
deliverables = {
    "email_to_ceo": {"status": "pending", "file": "ceo_email.txt"},
    "analysis_report": {"status": "pending", "file": "analysis.pdf"},
    "summary_doc": {"status": "pending", "file": "summary.docx"},
}

def mark_complete(name):
    deliverables[name]["status"] = "complete"
    print(f"✓ {name} completed")
    print(f"Progress: {sum(1 for d in deliverables.values() if d['status'] == 'complete')}/{len(deliverables)}")

def all_complete():
    return all(d["status"] == "complete" for d in deliverables.values())
```

## When to Apply This Skill

Use this pattern whenever a task involves:
- Multiple files to create
- Several analyses or reports
- Multiple communications (emails, messages)
- Code with multiple modules/components
- Any task with enumerated requirements (1), 2), 3)...)

## Remember
**The task is NOT complete until ALL deliverables are complete.** One done ≠ all done.
