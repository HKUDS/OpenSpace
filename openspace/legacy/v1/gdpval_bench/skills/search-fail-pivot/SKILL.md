---
name: search-fail-pivot
description: Heuristic for detecting search tool failures and pivoting to domain knowledge for document generation
---

# Search Tool Failure Detection and Pivot Strategy

## Purpose

When using `search_web` or similar external data retrieval tools, repeated failures waste iterations. This skill provides a diagnostic heuristic to recognize when to abandon search attempts and proceed with existing domain knowledge.

## Core Heuristic

**If `search_web` fails 2+ consecutive times with 'unknown error' or similar non-recoverable errors, pivot to domain knowledge + `run_shell` for document generation.**

Do not attempt more than 2 retries on the same or similar queries before pivoting.

## Step-by-Step Instructions

### Step 1: Track Search Failures

Monitor `search_web` outcomes during your task:
- Count consecutive failures (empty results, 'unknown error', timeout, access denied)
- Note the error type - distinguish between:
  - **Recoverable**: Rate limiting, temporary timeout (retry 1-2 times)
  - **Non-recoverable**: 'unknown error', persistent empty results, access denied

### Step 2: Apply the 2-Failure Rule

```
IF search_web failures >= 2 (same or similar queries)
THEN:
  1. Stop attempting search_web
  2. Document what information you attempted to retrieve
  3. Proceed with existing domain knowledge
```

### Step 3: Pivot to Domain Knowledge

When pivoting:
1. **Acknowledge the limitation**: Note that external verification was attempted but unavailable
2. **Use established knowledge**: Draw on training data for well-known facts, laws, cases, standards
3. **Generate with run_shell**: Create documents using Python/bash scripts rather than waiting for external data

Example pivot workflow:
```python
# Instead of continuing search_web attempts:
# 1. Compile known information from domain knowledge
known_facts = {
    "law": "COPPA requirements for children's data",
    "precedent": "FTC v. Google/YouTube settlement patterns",
    "jurisdiction": "California privacy law framework"
}

# 2. Generate document directly
# Use run_shell with Python to create PDF/DOC
```

### Step 4: Document Generation Pattern

When generating documents after pivoting:

```bash
# Use run_shell to execute document generation
# Example: Create legal memo with Python + reportlab
python << 'EOF'
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

# Generate document with domain knowledge
c = canvas.Canvas("memo.pdf", pagesize=letter)
c.drawString(100, 750, "Legal Analysis based on established precedents")
# ... continue with known information
c.save()
EOF
```

## When This Applies

This heuristic is particularly useful for:

| Task Type | Why It Applies |
|-----------|----------------|
| Legal research | Case law and statutes are well-documented in training |
| Technical documentation | Standards and best practices are established knowledge |
| Historical analysis | Past events and data are in training corpus |
| Regulatory compliance | Major regulations (GDPR, COPPA, HIPAA) are well-known |

## When NOT to Pivot

Do NOT apply this heuristic when:

- You need **current/time-sensitive data** (stock prices, weather, news < 24hrs)
- The task **explicitly requires** external verification
- You have **not yet attempted** the search (first attempt should complete)
- Errors are clearly **recoverable** (rate limit with retry-after header)

## Example Decision Flow

```
Attempt search_web → Success? → Yes → Use results
                         ↓
                        No
                         ↓
            Is this failure #1? → Yes → Retry once with modified query
                         ↓
                        No (failure #2+)
                         ↓
            Pivot to domain knowledge + run_shell document generation
```

## Benefits

- **Saves iterations**: Avoids wasting 3-4 attempts on unavailable data
- **Maintains progress**: Task continues instead of stalling on search
- **Leverages training**: Uses the substantial knowledge already available
- **Clear handoff**: Explicitly documents the pivot decision for transparency

## Anti-Patterns to Avoid

❌ Continuing search_web attempts beyond 2 failures  
❌ Not documenting why you pivoted from search  
❌ Applying this to tasks requiring real-time data  
❌ Abandoning search on first failure without retry  

✅ Apply 2-failure rule consistently  
✅ Document the pivot decision in your output  
✅ Use run_shell efficiently after pivoting  
✅ Distinguish recoverable vs non-recoverable errors