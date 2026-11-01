---
name: graceful-tool-failure
description: Report tool failures transparently and attempt minimal viable output with disclaimers
---

# Graceful Tool Failure Workflow

This skill guides agents on how to respond when critical tools (like PDF extraction, web search, data APIs) fail repeatedly and block task completion.

## When to Apply This Skill

Use this pattern when:
- A core tool required for task completion has failed multiple times (3+ attempts with different approaches)
- Alternative approaches to achieve the same goal have also failed
- The task cannot be fully completed without the blocked capability

## Step-by-Step Instructions

### 1. Explicitly Report Blocking Issues

Clearly document what failed and why:

```markdown
## Blocking Issues

The following tools/approaches were attempted but failed:

1. **PDF Extraction (pdftotext)**: Command returned error - file appears corrupted or uses non-standard encoding
2. **PDF Extraction (PyMuPDF)**: Library could not parse document structure
3. **Web Search**: Search queries returned no relevant results for [specific topic]

These failures prevent completion of [specific deliverable].
```

### 2. Attempt Minimal Viable Output

Using available domain knowledge, create the best possible output with clear limitations:

```markdown
## Partial Output (Based on Domain Knowledge)

*Note: The following content is derived from general domain knowledge rather than the requested source materials.*

[Insert best-effort content here]

**Confidence Level**: Low - content has not been verified against source documents
**Recommendation**: Verify all claims against original source materials when available
```

### 3. Avoid Claiming Success

Never state the task is complete when core deliverables are missing or unverified:

**Incorrect**:
> "Task completed successfully."

**Correct**:
> "Task partially completed. Core deliverable [X] could not be produced due to tool failures. Minimal viable output provided with disclaimers. Manual review or alternative data sources recommended."

### 4. Provide Actionable Next Steps

Suggest concrete actions for human operators or alternative approaches:

```markdown
## Recommended Next Steps

1. Obtain the source document in an alternative format (e.g., request text version from document owner)
2. Manually extract key information from the PDF and provide as text input
3. Use alternative tools: [list specific tools or services]
4. Proceed with partial output acknowledging limitations: [describe what can still be accomplished]
```

## Code Examples

### Example: Python Script with Fallback Behavior

```python
def extract_with_fallback(pdf_path):
    """Attempt PDF extraction with multiple methods, fail gracefully."""
    
    methods = [
        ("pdftotext", lambda p: run_shell(f"pdftotext {p} -")),
        ("PyMuPDF", lambda p: extract_with_pymupdf(p)),
        ("pdfplumber", lambda p: extract_with_pdfplumber(p)),
    ]
    
    failures = []
    for method_name, method_func in methods:
        try:
            result = method_func(pdf_path)
            if result:
                return {"success": True, "content": result}
        except Exception as e:
            failures.append(f"{method_name}: {str(e)}")
    
    # All methods failed - return graceful failure
    return {
        "success": False,
        "failures": failures,
        "message": "All extraction methods failed. Consider manual extraction or alternative source."
    }
```

### Example: Response Template for Failed Tasks

```markdown
## Task Status: PARTIALLY COMPLETE

### What Was Accomplished
- [List completed subtasks]

### What Could Not Be Completed
- [Deliverable X]: Blocked by [specific tool failure]
- [Deliverable Y]: Requires source material that could not be accessed

### Blocking Issues Summary
| Tool/Method | Attempts | Error |
|-------------|----------|-------|
| Tool A | 3 | [error details] |
| Tool B | 2 | [error details] |

### Partial Output (With Disclaimers)
[Insert best-effort work product]

### Recommendations
1. [Action item 1]
2. [Action item 2]
```

## Key Principles

1. **Transparency Over Optimism**: Clearly state what failed rather than obscuring limitations
2. **Document Attempts**: Record which approaches were tried to prevent redundant work
3. **Preserve Value**: Even partial output is better than no output, if properly disclaimed
4. **Enable Handoff**: Make it easy for a human or different system to pick up where you left off
5. **No False Claims**: Never mark a task as "complete" when core requirements are unmet

## Anti-Patterns to Avoid

- ❌ Silently skipping failed steps without documentation
- ❌ Claiming success when deliverables are missing or unverified
- ❌ Providing output without disclaimers about its reliability
- ❌ Repeating the same failed approach without variation
- ❌ Omitting specific error messages that would help diagnose the issue