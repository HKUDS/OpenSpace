---
name: research-fallback
description: Handle web research failures by pivoting to internal knowledge for content generation
---

# Research Fallback Workflow

This skill provides a workflow for handling tasks that require external research when web search or webpage reading tools fail due to network issues or tool unavailability.

## When to Use

- Task requires external research (legal analysis, market research, technical documentation, etc.)
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

Recognize when to pivot:

- Web search returns errors, timeouts, or empty results
- Page extraction fails repeatedly
- Network errors persist after 1-2 retry attempts
- Tool explicitly reports unavailability

**Do not** spend excessive time retrying failed tools. After 1-2 attempts, proceed to Step 3.

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

1. **Be transparent**: Clearly note when external verification wasn't possible
2. **Prioritize accuracy**: Only assert what you're confident about from training
3. **Flag uncertainties**: Mark areas where current sources would be valuable
4. **Don't block progress**: Tool failures shouldn't prevent task completion
5. **Maintain quality**: Internal knowledge can still produce high-quality, useful content

## Example Application

**Task**: Create legal memo on privacy law violations

**Workflow**:
1. Attempt search_web for recent COPPA enforcement cases → Tool fails
2. Attempt read_webpage on provided URLs → Connection timeout
3. Pivot: Use internal knowledge of COPPA requirements, California privacy laws, established case law principles
4. Generate memo with note that recent case citations should be verified
5. Create PDF deliverable within page limits

**Outcome**: Comprehensive memo produced despite tool failures, with appropriate disclaimers about verification.

## When NOT to Use This Skill

- Task explicitly requires current, verified external sources (e.g., real-time stock prices, breaking news)
- Legal/medical advice where current regulations are critical and disclaimer is insufficient
- Client specifically requires cited external sources for the deliverable

In these cases, report the tool failure and request guidance rather than proceeding with internal knowledge alone.