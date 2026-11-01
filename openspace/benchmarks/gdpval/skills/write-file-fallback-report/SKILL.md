---
name: write-file-fallback-report
description: Generate professional documents using write_file when primary data sources fail, leveraging embedded domain knowledge instead of external retrieval
---

# Write-File Fallback Report Generation

## When to Use This Skill

Use this workflow when attempting to generate a document or report, but **multiple primary data source tools fail simultaneously**:

- `read_file` returns binary/image data instead of text (common with PDFs)
- `search_web` returns errors or no results
- `execute_code_sandbox` fails unexpectedly
- Other data retrieval tools are unavailable

**Key insight**: Rather than getting stuck on failed data retrieval, pivot immediately to generating the document directly with `write_file` using professionally structured content and embedded domain knowledge.

## Step-by-Step Instructions

### Step 1: Detect Tool Failure Pattern

Recognize when you're in a fallback scenario:

```
TOOL_FAILURE_INDICATORS = [
    "read_file returns binary or image data",
    "search_web returns unknown error or empty results",
    "execute_code_sandbox fails repeatedly",
    "Multiple consecutive tool failures on data retrieval"
]
```

**Decision point**: If 2+ indicators are present, proceed to Step 2.

### Step 2: Pivot to Write-File-First Approach

**Stop attempting** to fix the failing tools. Instead:

1. Acknowledge the limitation briefly in your output
2. Commit to generating the document with available knowledge
3. Use `write_file` as your **primary** tool (not a last resort)

### Step 3: Structure the Document Professionally

Create a well-organized markdown document with:

```markdown
# [Document Title]

## Executive Summary
[Brief overview of key findings/content]

## Background
[Context and scope - use embedded knowledge]

## Main Content
[Organized sections with headers, lists, tables as appropriate]

## Limitations & Notes
[Transparent about data source limitations if relevant]

## Recommendations/Next Steps
[Actionable guidance based on available information]
```

### Step 4: Leverage Embedded Domain Knowledge

When external data is unavailable:

- Use **general domain knowledge** appropriately
- Clearly distinguish between verified facts and general guidance
- Include **actionable frameworks** rather than specific unverified data
- Add **placeholder notes** where specific data would enhance the document

Example:
```markdown
> **Note**: Specific [metric/data point] would typically be sourced from 
> [expected source]. The guidance below reflects established best practices 
> in this domain.
```

### Step 5: Execute and Validate

```python
# Example execution pattern
write_file(
    path="output/report.md",
    content=professionally_structured_markdown
)
# Verify the file was created successfully
list_dir(path=".")  # Confirm file exists
```

## Code Example

```python
# Detection and pivot pattern
def detect_and_pivot(task_goal):
    # After detecting tool failures:
    
    report_content = f"""# {task_goal} Report

## Executive Summary
This report was generated using established domain knowledge due to 
temporary unavailability of primary data sources.

## Key Frameworks and Guidance
[Structured content with headers, bullets, tables]

## Limitations
- Specific data points from [expected sources] were unavailable
- Recommendations based on general best practices

## Action Items
1. [Concrete step 1]
2. [Concrete step 2]
"""
    
    write_file(path="generated_report.md", content=report_content)
    return "Report generated successfully with embedded knowledge"
```

## Best Practices

| Do | Don't |
|----|-------|
| Pivot quickly after 2+ tool failures | Keep retrying failing tools 5+ times |
| Be transparent about limitations | Claim unverified specifics as facts |
| Provide actionable frameworks | Leave the task incomplete |
| Use professional document structure | Output unstructured text walls |
| Include next-step recommendations | End without clear guidance |

## Common Pitfalls

1. **Over-apologizing**: Acknowledge limitations once, then deliver value
2. **Under-delivering**: A well-structured partial report beats no report
3. **Misrepresenting certainty**: Use appropriate hedging language
4. **Skipping structure**: Professional formatting increases usability

## Success Criteria

The skill is successfully applied when:

- [ ] Document is generated despite tool failures
- [ ] Content is professionally structured (headers, sections, lists)
- [ ] Limitations are transparently noted
- [ ] Actionable guidance is provided
- [ ] File is successfully created and verified