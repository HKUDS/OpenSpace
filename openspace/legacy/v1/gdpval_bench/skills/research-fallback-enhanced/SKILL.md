---
name: research-fallback-robust
description: Handle web research failures with explicit error recognition and domain-specific fallback strategies
---

# Research Fallback Workflow

This skill provides a workflow for handling tasks that require external research when web search or webpage reading tools fail due to network issues or tool unavailability.

## When to Use

- Task requires external research (legal analysis, market research, technical documentation, anatomical/medical information, etc.)
- Web search or webpage reading tools are failing or unavailable
- You have sufficient internal knowledge to proceed with reasonable accuracy
- Task completion is prioritized over perfect external sourcing

## Workflow Steps

### Step 1: Attempt External Research First

Begin by attempting to use available web research tools:

```
1. Use search_web for current information, statistics, or recent developments
2. Use read_webpage for detailed source material when URLs are provided
3. Document what you attempted to find
```

### Step 2: Detect Tool Failure

Recognize when to pivot — **including explicit error patterns**:

- Web search returns errors, timeouts, or empty results
- **`unknown error` from search_web** — immediately recognize this as a tool failure trigger (do not retry excessively)
- Page extraction fails repeatedly
- Network errors persist after 1-2 retry attempts
- Tool explicitly reports unavailability

**Do not** spend excessive time retrying failed tools. After 1-2 attempts with `unknown error` or similar failures, proceed to Step 3.

### Step 3: Pivot to Internal Knowledge

When external tools fail:

1. **Acknowledge the limitation**: Note that external research was attempted but tools were unavailable
2. **Assess internal knowledge**: Determine what you know from training that covers the topic
3. **Identify gaps**: Be transparent about what information may be dated or unavailable
4. **Proceed with available knowledge**: Generate content using internal understanding

### Step 4: Generate Content with Appropriate Disclaimers

When producing deliverables:

```
- Include a note that external verification is recommended for time-sensitive information
- Flag any claims that would benefit from current source verification
- Focus on established principles, frameworks, and well-documented facts
- Avoid making specific claims about very recent events or statistics
```

### Step 5: Complete File Creation

Proceed with the original task deliverable:

- Create the requested file format (PDF, DOCX, markdown, etc.)
- Ensure content meets quality standards despite the research limitation
- Stay within any specified constraints (page limits, word counts, etc.)

## Example Decision Flow

```
Task requires research on [TOPIC]
         |
         v
Attempt search_web / read_webpage
         |
    +----+----+
    |         |
 Success    Failure (after 1-2 attempts)
    |         |
    |         | **Note: `unknown error` = immediate pivot signal**
    v         v
Use sources  Pivot to internal knowledge
    |         |
    +----+----+
         |
         v
Generate content with appropriate disclaimers
         |
         v
Create deliverable file
```

## Best Practices

1. **Recognize error patterns quickly**: `unknown error` from search_web is a clear signal to pivot, not a reason for extended retries
2. **Be transparent**: Clearly note when external verification wasn't possible
3. **Prioritize accuracy**: Only assert what you're confident about from training
4. **Flag uncertainties**: Mark areas where current sources would be valuable
5. **Don't block progress**: Tool failures shouldn't prevent task completion
6. **Maintain quality**: Internal knowledge can still produce high-quality, useful content
7. **Leverage stable knowledge domains**: Anatomical, medical, legal, and technical fundamentals from training are reliable even without external verification

## Example Applications

**Example 1: Legal Memo**

**Task**: Create legal memo on privacy law violations

**Workflow**:
1. Attempt search_web for recent COPPA enforcement cases → `unknown error`
2. Attempt read_webpage on provided URLs → Connection timeout
3. Pivot: Use internal knowledge of COPPA requirements, California privacy laws, established case law principles
4. Generate memo with note that recent case citations should be verified
5. Create PDF deliverable within page limits

**Outcome**: Comprehensive memo produced despite tool failures, with appropriate disclaimers about verification.

**Example 2: Anatomical/Medical Content**

**Task**: Create proposal with anatomical utilization analysis and procedure capacity estimates

**Workflow**:
1. Attempt search_web for anatomical area statistics → `unknown error`
2. Attempt execute_code_sandbox for analysis → `unknown error`
3. Pivot: Use internal training knowledge of:
   - Anatomical structures and terminology
   - Standard medical procedures and their requirements
   - Established frameworks for capacity estimation
   - Common cost structures in medical/cadaver programs
4. Generate content using reliable internal knowledge (anatomical facts are well-established)
5. Create DOCX deliverable with appropriate verification notes for any statistics

**Outcome**: High-quality proposal with anatomical tables, procedure estimates, and cost analysis created using internal knowledge, with transparent disclaimers about statistical verification.

## When NOT to Use This Skill

- Task explicitly requires current, verified external sources (e.g., real-time stock prices, breaking news)
- Legal/medical advice where current regulations are critical and disclaimer is insufficient
- Client specifically requires cited external sources for the deliverable

In these cases, report the tool failure and request guidance rather than proceeding with internal knowledge alone.
