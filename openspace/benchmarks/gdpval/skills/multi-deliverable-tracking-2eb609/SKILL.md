---
name: multi-deliverable-tracking-2eb609
description: Systematic tracking and completion of all required deliverables before task termination
---

# Multi-Deliverable Tracking

This skill ensures that agents explicitly identify, track, and complete ALL required deliverables before stopping, preventing premature task completion when multiple outputs are required.

## Core Principle

**Never stop working until you can confirm every required deliverable is complete.**

## Step-by-Step Instructions

### 1. Identify All Deliverables Upfront

At the start of any task, explicitly list ALL expected outputs:

```markdown
## Required Deliverables Checklist
- [ ] Deliverable 1: [description]
- [ ] Deliverable 2: [description]
- [ ] Deliverable 3: [description]
```

**Action:** Parse the task description carefully for keywords like:
- "and" (e.g., "create a report AND a presentation")
- "both" (e.g., "both files must be created")
- "each" (e.g., "for each department, create...")
- Numbered requirements (e.g., "1) ..., 2) ...")
- File names mentioned (count them)

### 2. Track Completion Status

Maintain a visible checklist throughout execution. Update it after each deliverable:

```markdown
## Progress Tracker
| Deliverable | Status | Evidence |
|-------------|--------|----------|
| Email to CEO | ✅ Complete | File: ceo_email.md created |
| Email to CTO | ⏳ Pending | - |
```

**Action:** After completing each item, update the checklist BEFORE moving to the next task.

### 3. Verify Before Stopping

Before declaring a task complete, perform a final verification:

1. Review the original task requirements
2. Check each item in your deliverables checklist
3. Confirm file existence (use `ls` or `list_dir` if needed)
4. Only stop when ALL checkboxes are marked ✅

### 4. Recovery Protocol

If you discover a missing deliverable:

1. **Acknowledge the gap**: "I notice deliverable X was not completed"
2. **Prioritize it immediately**: Complete the missing item before any other work
3. **Update the checklist**: Mark it complete with evidence
4. **Re-verify**: Ensure no other deliverables were missed

## Code Examples

### Deliverable Tracking Template

```python
# Track deliverables programmatically
deliverables = {
    "ceo_email": {"required": True, "complete": False, "path": "ceo_email.md"},
    "cto_email": {"required": True, "complete": False, "path": "cto_email.md"},
}

def verify_all_complete():
    for name, info in deliverables.items():
        if info["required"] and not info["complete"]:
            return False, f"Missing: {name}"
    return True, "All deliverables complete"
```

### Shell Verification

```bash
# Verify multiple files exist before concluding
for file in ceo_email.md cto_email.md; do
    if [ ! -f "$file" ]; then
        echo "ERROR: Missing deliverable: $file"
        exit 1
    fi
done
echo "All deliverables verified"
```

## Common Pitfalls to Avoid

| Pitfall | Solution |
|---------|----------|
| Stopping after first deliverable | Always check for "and", "both", multiple file names |
| Assuming implicit completion | Explicitly verify each file/output exists |
| Losing track mid-task | Keep checklist visible in working notes |
| Not re-reading requirements | Re-parse task description before final verification |

## When to Apply

Use this skill whenever:
- Task mentions multiple files, reports, or outputs
- Requirements include conjunctions (and, both, each)
- Multiple stakeholders or recipients are mentioned
- Task has numbered or bulleted requirements

## Anti-Pattern Example

❌ **Wrong:** "I created the email to the CEO. Task complete."
*(Missing: email to CTO was also required)*

✅ **Correct:** "I created emails to both CEO and CTO. Verified both files exist. All 2 deliverables complete."