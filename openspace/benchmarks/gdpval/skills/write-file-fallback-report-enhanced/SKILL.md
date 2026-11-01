---
name: fallback-doc-with-delegation
description: Generate professional documents after tool failures using shell_agent for complex formats or write_file for simple text/markdown
---

# Fallback Document Generation with Delegation

## When to Use This Skill

Use this workflow when attempting to generate a document or report, but **multiple primary data source tools fail simultaneously**:

- `read_file` returns binary/image data instead of text (common with PDFs)
- `search_web` returns errors or no results
- `execute_code_sandbox` fails unexpectedly
- Other data retrieval tools are unavailable

**Key insight**: Rather than getting stuck on failed data retrieval, pivot immediately to generating the document using the most appropriate available method—either `shell_agent` for complex formats or `write_file` for simple text.

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

### Step 2: Choose the Appropriate Fallback Method

**Critical Decision**: Select between `shell_agent` and `write_file` based on document complexity:

| Use `shell_agent` when... | Use `write_file` when... |
|---------------------------|--------------------------|
| Generating PDFs or formatted documents | Creating simple markdown/text files |
| Complex layout or styling needed | Plain structured content is sufficient |
| External libraries required (reportlab, pandoc) | No special formatting libraries needed |
| Multi-step generation process | Single-pass content generation |
| Verification/validation needed | Direct file creation is adequate |

**Decision Tree**:
```
Is the output a PDF or complex formatted document?
├── YES → Use shell_agent (delegates to skilled agent)
└── NO → Is it markdown or plain text?
    ├── YES → Use write_file (direct content generation)
    └── NO → Use shell_agent (handles uncertainty)
```

### Step 3A: Execute with shell_agent (Complex Documents)

For PDFs and complex formatting:

```python
# Delegate to shell_agent with clear task description
shell_agent(
    task="Generate a professional [document type] about [topic]. "
         "Use appropriate libraries (e.g., reportlab for PDFs). "
         "Include: [list key sections/requirements]. "
         "Save to working directory as [filename]."
)
```

**Advantages of shell_agent**:
- Agent autonomously handles library selection and usage
- Automatically retries and fixes errors
- Can use multiple tools internally (run_shell, execute_code_sandbox)
- Validates output before completion

**Example**:
```python
shell_agent(
    task="Create a 1-page SBAR (Situation-Background-Assessment-Recommendation) "
         "template PDF with 4 sections. Each section should have guiding points "
         "and blank lined spaces for user input. Use reportlab library. "
         "Save as SBAR_template.pdf in current directory."
)
```

### Step 3B: Execute with write_file (Simple Documents)

For markdown and plain text:

```python
# Generate content with embedded domain knowledge
report_content = f"""# [Document Title]

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
"""

write_file(path="output/report.md", content=report_content)
```

### Step 4: Structure Content Professionally

Regardless of method, ensure professional organization:

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

### Step 5: Leverage Embedded Domain Knowledge

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

### Step 6: Validate Output

```python
# For shell_agent: verify the output exists and is valid
list_dir(path=".")  # Confirm file was created

# For PDFs, optionally verify with pdfinfo via run_shell
# For markdown, optionally read to confirm content

# For write_file: confirm file exists
list_dir(path=".")  # Confirm file exists
```

## Code Example

```python
# Complete detection and pivot pattern
def generate_fallback_document(task_goal, output_format="markdown"):
    # After detecting tool failures:
    
    # Step 1: Choose method based on format
    if output_format == "pdf" or "complex" in task_goal:
        # Use shell_agent for complex formats
        shell_agent(
            task=f"Generate a professional {output_format} document about "
                 f"{task_goal}. Use appropriate libraries. Include executive "
                 f"summary, main content, limitations, and recommendations. "
                 f"Save to working directory."
        )
        return "Document generated via shell_agent delegation"
    
    else:
        # Use write_file for simple formats
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
        return "Report generated with write_file"
```

## Best Practices

| Do | Don't |
|----|-------|
| Choose method based on document complexity | Default to write_file for PDFs without consideration |
| Delegate complex formatting to shell_agent | Keep retrying failing tools 5+ times |
| Be transparent about limitations | Claim unverified specifics as facts |
| Provide actionable frameworks | Leave the task incomplete |
| Use professional document structure | Output unstructured text walls |
| Validate the generated output | Assume success without verification |

## Common Pitfalls

1. **Wrong tool selection**: Using write_file for PDF generation instead of delegating to shell_agent
2. **Over-apologizing**: Acknowledge limitations once, then deliver value
3. **Under-delivering**: A well-structured partial report beats no report
4. **Misrepresenting certainty**: Use appropriate hedging language
5. **Skipping validation**: Always confirm the file was created successfully

## Success Criteria

The skill is successfully applied when:

- [ ] Document is generated despite tool failures
- [ ] Appropriate method chosen (shell_agent for complex, write_file for simple)
- [ ] Content is professionally structured (headers, sections, lists)
- [ ] Limitations are transparently noted
- [ ] Actionable guidance is provided
- [ ] File is successfully created and verified

## Method Comparison

| Aspect | shell_agent | write_file |
|--------|-------------|------------|
| Best for | PDFs, complex layouts, multi-step generation | Markdown, plain text, single-pass content |
| Error handling | Autonomous retry and fix | Manual error handling required |
| Library access | Full library ecosystem | Content-only (no external libs) |
| Processing time | Longer (agent reasoning) | Faster (direct write) |
| Verification | Built-in validation | Manual verification needed |
| Cost | Higher (agent execution) | Lower (simple operation) |

## Decision Checklist

Before selecting method, ask:

1. Is the output format PDF or other binary format? → **shell_agent**
2. Does it require external libraries (reportlab, pandoc, etc.)? → **shell_agent**
3. Is it markdown or plain text only? → **write_file**
4. Does it need multi-step processing or validation? → **shell_agent**
5. Is quick, simple content generation sufficient? → **write_file**
