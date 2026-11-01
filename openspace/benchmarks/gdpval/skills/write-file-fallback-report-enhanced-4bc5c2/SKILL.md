---
name: context-anchored-fallback-report
description: Generate documents with write_file when retrieval tools fail, with explicit guardrails against task context drift
---

# Context-Anchored Fallback Report Generation

## Critical Warning: Context Drift Prevention

**DO NOT ABANDON THE ORIGINAL TASK** when tools fail. The most common failure mode is switching to an unrelated task after encountering tool errors.

```
CONTEXT_DRIFT_WARNING:
  IF tool_failures occur (read_webpage, search_web, execute_code_sandbox)
  THEN you MUST stay on the ORIGINAL task
  AND you MUST NOT switch to unrelated tasks
  AND you MUST generate output for the original goal using write_file
```

## When to Use This Skill

Activate this workflow when **multiple primary data source tools fail** during document/report generation:

### Trigger Decision Tree

```
START: Attempting to generate document/report
  │
  ├─→ Tool #1 fails (read_webpage/search_web/read_file/execute_code_sandbox)?
  │   │
  │   ├─→ YES → Count = 1, Continue attempting
  │   │
  │   └─→ NO → Proceed normally with retrieved data
  │
  ├─→ Tool #2 fails (same or different tool)?
  │   │
  │   ├─→ YES → Count = 2, TRIGGER FALLBACK NOW
  │   │         │
  │   │         ├─→ LOCK to original task goal (write it down)
  │   │         ├─→ Stop attempting failing tools
  │   │         └─→ Generate document with write_file + embedded knowledge
  │   │
  │   └─→ NO → Continue with 1-2 more attempts max
  │
  └─→ 2+ consecutive failures without document output?
      │
      └─→ CRITICAL ERROR: You are drifting from task. STOP and apply this skill.
```

### Tool Failure Indicators

```python
FALLBACK_TRIGGERS = {
    "read_file": "returns binary/image data instead of text, or file not found",
    "search_web": "returns unknown error, empty results, or rate limited",
    "read_webpage": "returns unknown error, connection timeout, or blocked access",
    "execute_code_sandbox": "fails with unknown error or timeout repeatedly",
    "any_retrieval_tool": "2+ consecutive failures on data retrieval attempts"
}
```

**Activation threshold**: 2+ failures from the above triggers = IMMEDIATE fallback activation.

## Step-by-Step Instructions

### Step 0: Context Anchor (BEFORE pivoting)

**CRITICAL STEP**: Before any fallback action, write down the original task:

```
CONTEXT_ANCHOR = "[Original task goal from user request]"
```

Example:
```
CONTEXT_ANCHOR = "Create PACT Act veterans benefits document explaining claim process"
```

**Rule**: Every subsequent action must reference and serve this CONTEXT_ANCHOR. Never deviate.

### Step 1: Detect and Declare Failure Pattern

When 2+ tool failures occur:

1. **State the limitation clearly** (once, briefly):
   ```
   Note: External data sources (web search, webpage access) are currently unavailable.
   This report will be generated using established domain knowledge.
   ```

2. **Declare the pivot explicitly**:
   ```
   Pivoting to write_file-based document generation with embedded knowledge.
   Task remains: [CONTEXT_ANCHOR]
   ```

3. **Do NOT**:
   - Apologize repeatedly
   - Continue retrying failed tools beyond 2 attempts
   - Suggest the task cannot be completed
   - Switch to a different task

### Step 2: Pre-Compute Available Knowledge

Before writing, inventory what you CAN provide:

```python
# Knowledge inventory for the task
available_knowledge = {
    "domain_frameworks": "General best practices, standard procedures",
    "structural_templates": "Professional document formats for this type",
    "actionable_guidance": "Step-by-step processes based on established patterns",
    "placeholder_markers": "Where specific data would enhance (clearly marked)"
}
```

### Step 3: Generate Document with write_file

Create professionally structured content:

```markdown
# [Document Title - aligned with CONTEXT_ANCHOR]

## Executive Summary
[2-3 sentences on what this document covers, noting data source limitations if relevant]

## Background & Scope
[Context based on embedded domain knowledge]

## Core Content
[Organized sections with:
  - Clear headers (##, ###)
  - Bullet points and numbered lists
  - Tables where appropriate
  - Code blocks for technical content
]

## Data Source Notes
> **Note**: Specific data points that would typically come from [expected sources]
> were unavailable at generation time. Content reflects established practices
> in this domain.

## Actionable Recommendations
1. [Concrete step 1]
2. [Concrete step 2]
3. [Next steps for obtaining specific data if needed]

## Task Completion Status
- **Original Goal**: [CONTEXT_ANCHOR]
- **Completion Method**: Generated via write_file with embedded domain knowledge
- **Data Limitations**: [Brief note on what was unavailable]
```

### Step 4: Execute and Verify

```python
# Execution pattern
write_file(
    path="[output_path].md",  # or .txt, .html based on task
    content=professionally_structured_markdown
)

# Verification
list_dir(path=".")  # Confirm file creation
# Optional: read_file to verify content quality
```

### Step 5: Context Integrity Check

Before declaring task complete, verify:

```
CONTEXT_CHECKLIST = [
    "✓ Output addresses ORIGINAL task goal",
    "✓ No unrelated task content included",
    "✓ Document is professionally structured",
    "✓ Limitations are transparent but not over-emphasized",
    "✓ Actionable guidance is provided",
    "✓ File was successfully created"
]
```

If any check fails, revise before completing the task.

## Code Example: Full Pattern

```python
def context_anchored_fallback(original_task, failed_tools):
    """
    Generate document when tools fail, maintaining task context.
    """
    # Step 0: Anchor context
    context_anchor = original_task
    print(f"CONTEXT ANCHOR: {context_anchor}")
    
    # Step 1: Declare pivot
    limitation_note = """
    Note: External data retrieval tools experienced failures.
    This document is generated using established domain knowledge.
    """
    
    # Step 2-3: Generate structured content
    document = f"""# {original_task} - Report

## Executive Summary
{limitation_note.strip()}
This report provides guidance based on established domain knowledge.

## Core Guidance
### Key Framework
[Structured content with headers, bullets, tables]

### Process Overview
1. Step one
2. Step two
3. Step three

## Data Source Notes
> Specific data from [expected sources] was unavailable.
> Recommendations reflect established best practices.

## Action Items
1. [Actionable step 1]
2. [Actionable step 2]

## Task Status
- **Goal**: {context_anchor}
- **Status**: Completed via fallback generation
- **Limitations**: Data sources unavailable, content based on domain knowledge
"""
    
    # Step 4: Execute
    output_path = "generated_report.md"
    write_file(path=output_path, content=document)
    
    # Step 5: Verify
    files = list_dir(path=".")
    assert output_path in [f['name'] for f in files]
    
    return f"Task completed: {context_anchor}"
```

## Guardrails Against Context Drift

### RED FLAGS (Stop immediately if you notice these)

| Red Flag | Corrective Action |
|----------|-------------------|
| Thinking about a different task than the original | STOP. Re-read CONTEXT_ANCHOR. |
| More than 3 tool retry attempts | STOP. Trigger fallback immediately. |
| Considering "maybe this task isn't possible" | STOP. Generate with available knowledge. |
| Output doesn't match original task goal | STOP. Regenerate aligned with CONTEXT_ANCHOR. |
| Spending >5 iterations on tool troubleshooting | STOP. Pivot to write_file approach. |

### GREEN FLAGS (You're on track)

- Every output references the original task goal
- Tool failures trigger fallback within 2 attempts
- Document is generated even with data limitations
- Final output clearly serves the original user request
- No unrelated task content appears in output

## Best Practices

| Do | Don't |
|----|-------|
| Write CONTEXT_ANCHOR before any fallback action | Start generating without anchoring to original task |
| Trigger fallback after 2 tool failures | Retry failing tools 5+ times |
| Generate complete document with available knowledge | Leave task incomplete due to missing data |
| Mark unverifiable specifics clearly | Present猜测 as verified facts |
| Verify output matches original task | Assume task is complete without checking |
| Use professional document structure | Output unstructured text |

## Common Pitfalls & Solutions

| Pitfall | Solution |
|---------|----------|
| **Context drift after tool failures** | Write CONTEXT_ANCHOR visibly before generating; check every output against it |
| **Over-apologizing for limitations** | State limitation once, then deliver value |
| **Under-delivering (no output)** | A structured partial report beats no report |
| **Misrepresenting certainty** | Use hedging: "typically", "generally", "established practice" |
| **Skipping verification** | Always run list_dir to confirm file creation |

## Success Criteria

Task is successfully completed when ALL are true:

- [ ] Document generated despite tool failures
- [ ] Output addresses ORIGINAL task goal (CONTEXT_ANCHOR)
- [ ] No unrelated task content in output
- [ ] Professional structure (headers, sections, lists, tables)
- [ ] Limitations transparently noted (not over-emphasized)
- [ ] Actionable guidance provided
- [ ] File successfully created and verified
- [ ] Fallback triggered within 2-3 tool failures (not after excessive retries)

## Quick Reference Card

```
┌─────────────────────────────────────────────────────────┐
│  CONTEXT-ANCHORED FALLBACK - QUICK TRIGGER              │
├─────────────────────────────────────────────────────────┤
│  IF 2+ retrieval tools fail                             │
│  THEN:                                                  │
│    1. Write CONTEXT_ANCHOR = [original task]            │
│    2. Stop retrying failed tools                        │
│    3. Generate document with write_file                 │
│    4. Use embedded domain knowledge                     │
│    5. Verify output matches CONTEXT_ANCHOR              │
│    6. Confirm file created with list_dir                │
│                                                         │
│  NEVER:                                                 │
│    - Switch to unrelated task                           │
│    - Retry >3 times without pivoting                    │
│    - Abandon original task goal                         │
└─────────────────────────────────────────────────────────┘
```
