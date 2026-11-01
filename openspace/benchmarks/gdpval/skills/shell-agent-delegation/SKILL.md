---
name: shell-agent-delegation
description: Delegate complex tasks to shell_agent when direct tool execution fails, leveraging autonomous error recovery and library selection
---

# Shell Agent Delegation for Resilient Workflow Execution

## When to Use This Skill

Apply this pattern when:
- Direct tool execution (execute_code_sandbox, read_webpage, search_web) fails with 'unknown error'
- Multiple tool attempts have failed in sequence
- The task requires complex document generation or data processing
- You need a tool that can autonomously select libraries and handle multi-step workflows

## Why This Works

The `shell_agent` tool differs from direct execution tools in key ways:
- **Autonomous tool selection**: Decides whether to use Python or Bash based on the task
- **Built-in error recovery**: Automatically retries and fixes errors (up to several rounds)
- **Iterative execution**: Writes code, executes, inspects output, and adapts
- **Full workflow ownership**: Handles the entire task end-to-end without manual intervention

## Step-by-Step Instructions

### Step 1: Recognize the Failure Pattern

Identify when to pivot to shell_agent:
```
- execute_code_sandbox returned 'unknown error'
- read_webpage/search_web failed multiple times
- Direct approaches are struggling with the task complexity
```

### Step 2: Formulate the Delegation Task

Create a clear, self-contained task description for shell_agent:

**Good task description:**
```
Create a 1-page SBAR Template PDF document. Include sections for:
- Situation: Brief description of the current situation
- Background: Relevant context and history
- Assessment: Current assessment and analysis
- Recommendation: Proposed actions and next steps
Use a professional layout with clear headings and adequate whitespace.
```

**Key elements to include:**
- The end goal (what should be produced)
- Required sections/components
- Format requirements (PDF, DOCX, etc.)
- Any style or layout preferences

### Step 3: Execute the Delegation

Call shell_agent with your task description:

```python
# Conceptual example
shell_agent(task="Create a professional SBAR Template PDF with Situation, Background, Assessment, and Recommendation sections. Include clear headings and professional formatting.")
```

### Step 4: Monitor and Verify

After shell_agent completes:
1. Check that the output file was created in the working directory
2. Verify the content meets requirements
3. If issues remain, provide refined instructions to shell_agent

## Code Example

```python
# When direct approaches fail:
# execute_code_sandbox(code="...")  # Returns 'unknown error'
# search_web(query="...")  # Returns 'unknown error'

# Pivot to shell_agent:
shell_agent(
    task="Generate a professional one-page template document in PDF format. "
         "Include clearly labeled sections with appropriate spacing and formatting. "
         "Select the most appropriate Python library for PDF generation.",
    timeout=300  # Allow time for iteration and error recovery
)
```

## Best Practices

1. **Be specific about the output**: Clearly describe what the final product should look like
2. **Trust the autonomy**: Let shell_agent decide on libraries and implementation details
3. **Allow sufficient timeout**: Set timeout to 300+ seconds for complex tasks requiring iteration
4. **One task at a time**: Give shell_agent a complete, self-contained objective
5. **Don't micromanage**: Avoid prescribing specific libraries or code unless necessary

## Common Use Cases

- Document generation (PDF, DOCX, reports, templates)
- Data processing pipelines with multiple steps
- Web scraping with fallback handling
- Complex file manipulation tasks
- Tasks requiring library discovery and selection

## Anti-Patterns to Avoid

❌ Don't use shell_agent for simple, single-command tasks (use run_shell instead)
❌ Don't provide overly prescriptive code instructions (defeats the autonomous benefit)
❌ Don't set timeout too low (<60 seconds for complex tasks)
❌ Don't split a coherent task into multiple shell_agent calls unnecessarily