---
name: regulatory-fallback-research
description: Handle tool failures when researching regulatory/government content by using fallback methods and domain knowledge
---

# Regulatory/Government Content Research with Fallback Strategy

## Overview

When researching regulatory, government, or compliance-related content, primary sources (official websites, PDFs, regulatory databases) often become inaccessible due to tool failures, access restrictions, or technical issues. This skill provides a resilient workflow to complete research tasks despite these obstacles.

## When to Use This Skill

Use this workflow when:
- You need to research regulatory/government content (FDA, CMS, DEA, state boards, etc.)
- Primary source tools (read_webpage, search_web) return 'unknown error' or fail repeatedly
- You need to produce compliance documentation, checklists, or regulatory summaries

## Step-by-Step Procedure

### Step 1: Attempt Primary Source Access

First, try to access official sources directly:

```
1. Use read_webpage on known regulatory URLs
2. Use search_web for specific regulatory queries
3. Attempt read_file on any available PDFs or documents
```

**Expected outcomes:**
- Success: Proceed with content extraction
- 'unknown error' or repeated failures: Document the failure and proceed to Step 2

### Step 2: Deploy Shell Agent for Secondary Research

When primary tools fail, use shell_agent for alternative information gathering:

```
shell_agent task: "Research [topic] compliance requirements using alternative sources. 
Search for summaries, guides, or cached versions of regulatory information. 
Focus on established compliance frameworks and best practices."
```

**Strategies for shell_agent:**
- Request research from multiple angles (state vs federal, industry guides, etc.)
- Ask for aggregated information from secondary sources
- Request compliance checklist templates from industry resources

### Step 3: Apply Domain Knowledge

When both primary and secondary research tools fail:

```
1. Acknowledge the tool limitations explicitly
2. State you will proceed using established domain knowledge
3. Create content based on well-known regulatory frameworks
```

**Key domains to leverage:**
- **Pharmacy compliance**: HIPAA, OBRA-90, controlled substance handling, prescription verification, drug storage, patient counseling
- **Healthcare compliance**: OSHA, HIPAA privacy/security, Medicare/Medicaid billing, infection control
- **General regulatory**: Documentation requirements, audit trails, staff training, record retention

### Step 4: Produce Deliverables Despite Limitations

Continue to completion:

```
1. Create required outputs (checklists, summaries, documentation)
2. Note any limitations due to inaccessible sources
3. Recommend verification against current official sources
4. Structure content for easy updating when sources become available
```

## Code Examples

### Example: Handling Multiple Tool Failures

```
# Document failures transparently
"Note: Primary regulatory sources (FDA.gov, state board websites) 
returned access errors. Content compiled from established 
compliance frameworks and industry standards."

# Proceed with domain knowledge
create_pharmacy_checklist(
    categories=["prescription_handling", "controlled_substances", 
                "patient_counseling", "record_keeping"],
    basis="OBRA-90 and standard pharmacy practice guidelines"
)

# Add verification disclaimer
"Recommendation: Verify all requirements against current state 
board regulations before implementation."
```

### Example: Shell Agent Fallback Query

```
shell_agent task: "Gather pharmacy compliance requirements from 
secondary sources including:
1. Industry association guidelines (NCPDP, ASHP)
2. Academic pharmacy practice resources
3. State board examination study materials
4. Compliance training program outlines

Compile into structured checklist format."
```

## Best Practices

1. **Never abandon the task** - Tool failures are common with government sites; persist using available methods

2. **Be transparent** - Always document which sources were inaccessible and what basis you used instead

3. **Structure for updates** - Create modular content that can be easily revised when sources become available

4. **Prioritize safety-critical items** - When uncertain, include conservative compliance measures

5. **Recommend verification** - Always advise end-users to verify against current official sources

## Common Failure Scenarios

| Failure | Fallback Action |
|---------|-----------------|
| read_webpage 'unknown error' | Try search_web, then shell_agent |
| search_web 'unknown error' | Try shell_agent research task |
| shell_agent timeout/failure | Proceed with domain knowledge |
| All tools fail | Use established domain knowledge, document limitations |

## Quality Criteria

Your output is successful when:
- [ ] All required deliverables are produced
- [ ] Tool failures are documented transparently
- [ ] Content is based on verifiable regulatory frameworks
- [ ] Verification recommendations are included
- [ ] Content structure allows easy updates

---
name: regulatory-fallback-research
description: Handle tool failures when researching regulatory/government content by using fallback methods and domain knowledge
---