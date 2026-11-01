---
name: search-fallback-workflow
description: Graceful degradation workflow for continuing tasks when web search tools fail, using internal knowledge with verification caveats
---

# Search Fallback Workflow

## When to Apply

Use this workflow when:
- `search_web` tool fails repeatedly (typically 2+ consecutive failures)
- External information is needed but unavailable due to tool limitations
- Task completion is still possible with reasonable confidence using internal knowledge
- The output can include appropriate caveats about verification needs

## Core Pattern

1. **Detect Search Failure**
   - Monitor `search_web` responses for errors, empty results, or timeouts
   - After 2 consecutive failures, assess whether the task can proceed without fresh data

2. **Assess Feasibility**
   - Determine if internal knowledge is sufficient for the task at hand
   - Identify which sections/claims require external verification
   - Evaluate risk level: Can the task proceed safely with caveats?

3. **Proceed with Internal Knowledge**
   - Continue the task using well-established facts, regulations, or principles
   - Prioritize accuracy over completeness when uncertain
   - Use conservative language for claims that cannot be verified

4. **Flag for Verification**
   - Clearly mark any claims that should be verified:
     ```markdown
     [VERIFICATION NEEDED: Search tool unavailable during generation]
     ```
   - Include a verification checklist in the execution summary
   - Distinguish between high-confidence and low-confidence assertions

5. **Document Limitations**
   - Include an explicit note in the output about search unavailability
   - List specific items requiring follow-up verification
   - Provide recommendations for manual verification steps

## Implementation Template

```markdown
## Execution Notes

**Search Tool Status**: Unavailable during generation

**Confidence Levels**:
- ✓ High confidence: Established regulations, well-documented facts
- ⚠ Medium confidence: Industry standards, commonly accepted practices  
- ✗ Requires verification: Time-sensitive data, recent changes, specific citations

**Verification Checklist**:
- [ ] Verify citation: [specific reference]
- [ ] Confirm current status: [specific item]
- [ ] Review recent updates: [specific topic]
```

## Code Pattern for Agents

```python
# Pseudocode for search fallback detection
search_attempts = 0
max_search_attempts = 2
search_failed = False

for query in required_searches:
    result = search_web(query)
    if result.success:
        process_result(result)
    else:
        search_attempts += 1
        if search_attempts >= max_search_attempts:
            search_failed = True
            log_warning("Search tool unavailable, proceeding with fallback")
            break

if search_failed:
    # Apply graceful degradation
    content = generate_with_internal_knowledge()
    add_verification_flags(content)
    document_limitations_in_summary()
```

## Example Output Structure

When using this fallback, structure documents with clear sections:

```markdown
# Document Title

## Disclaimer
This document was generated with limited access to external verification tools.
Items marked with [VERIFICATION NEEDED] should be confirmed before final use.

## Content Sections
[Standard content with internal knowledge]

## Items Requiring Verification
- Topic A: Current regulatory status needs confirmation
- Topic B: Recent policy changes should be checked
- Topic C: Specific citations require source validation

## Recommendations
1. Cross-reference with official sources
2. Verify time-sensitive information
3. Consult subject matter experts for critical decisions
```

## Best Practices

1. **Don't fabricate citations** - Better to flag as unverified than invent sources
2. **Use conservative language** - "Generally," "Typically," "Commonly" instead of absolutes
3. **Prioritize safety** - If misinformation could cause harm, recommend manual verification
4. **Track what's missing** - Maintain a clear list of items needing follow-up
5. **Be transparent** - Clearly communicate limitations to end users

## When NOT to Use This Pattern

- Tasks requiring current/real-time data (stock prices, news, weather)
- Legal or medical advice requiring authoritative sources
- Compliance documentation where citations are mandatory
- Situations where incorrect information could cause significant harm

## Related Patterns

- `citation-management`: Handling references and sources in documents
- `error-recovery-workflow`: General patterns for tool failure recovery
- `confidence-annotation`: Marking certainty levels in generated content