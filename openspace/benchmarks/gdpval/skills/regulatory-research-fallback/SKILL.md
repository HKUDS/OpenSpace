---
name: regulatory-research-fallback
description: Fallback workflow for regulatory research when web extraction tools fail on government PDFs
---

# Regulatory Research Fallback Workflow

This skill provides a systematic approach to gathering regulatory/compliance information when primary web extraction tools fail on government or regulatory website URLs.

## When to Use This Skill

- `read_webpage` returns "unknown error" on .gov or regulatory PDF URLs
- `search_web` fails to retrieve current regulatory information
- You need to create compliance checklists or regulatory documentation
- Primary sources are inaccessible but the task must be completed

## Step-by-Step Procedure

### Step 1: Attempt Primary Source Extraction

First, try standard web extraction tools:

```
1. Use read_webpage on the regulatory URL
2. Use search_web for supplementary regulatory information
3. Document which tools failed and with what errors
```

### Step 2: Activate Fallback - Shell Agent Research

When primary tools fail, delegate to shell_agent for secondary research:

```
Task: "Research [TOPIC] compliance requirements using available system tools. 
Gather information from alternative sources including:
- State/federal regulatory summaries
- Industry compliance guides
- Professional association resources
Focus on actionable checklist items for [SPECIFIC REQUIREMENT]"
```

Example shell_agent task for pharmacy compliance:
```
"Research pharmacy compliance requirements for [state]. Gather information 
about licensing, storage, dispensing, and documentation requirements from 
available sources. Create structured notes on mandatory compliance areas."
```

### Step 3: Apply Domain Knowledge

When sources remain inaccessible, use established domain knowledge:

```
For pharmacy compliance, standard areas include:
- Licensing and registration requirements
- Controlled substance handling
- Prescription record-keeping
- Storage and security protocols
- Patient counseling requirements
- Continuing education obligations
```

### Step 4: Create Documentation with Confidence Levels

Structure output with clear sourcing transparency:

```markdown
## [Requirement Area]

**Status**: Based on standard regulatory framework (primary sources inaccessible)

**Key Requirements**:
- [Item 1] - Standard industry requirement
- [Item 2] - Common regulatory expectation
- [Item 3] - Best practice guideline

**Note**: Verify with [specific agency] for jurisdiction-specific requirements
```

### Step 5: Flag Verification Needs

Always include disclaimers when using fallback approach:

```
⚠️ **Verification Required**: This checklist was created using secondary 
sources due to primary regulatory website inaccessibility. Please verify 
all requirements with the official [Agency Name] before implementation.
```

## Key Principles

1. **Persevere through failures** - Tool errors don't mean task failure
2. **Layer your research** - Try multiple approaches before concluding information is unavailable
3. **Be transparent** - Clearly mark when information comes from secondary sources
4. **Provide value anyway** - A best-effort checklist is better than no guidance
5. **Flag verification needs** - Help users understand what needs official confirmation

## Example Output Structure

```markdown
# [Compliance Area] Checklist

## Source Transparency
This document was created using secondary research methods due to 
inaccessibility of primary regulatory sources. All items should be 
verified with official authorities.

## Core Requirements
[Checklist items based on standard regulatory framework]

## Verification Steps
[List of specific items requiring official confirmation]

## Recommended Next Actions
1. Contact [Agency] for current requirements
2. Review [Official Publication] when accessible
3. Consult with [Professional Association] for updates
```

## Applicable Domains

This workflow applies to:
- Pharmacy/healthcare compliance
- Financial regulatory requirements
- Environmental compliance
- Building codes and permits
- Employment law requirements
- Any jurisdiction-specific regulatory research

---
name: regulatory-research-fallback
category: workflow
version: 1.0
---