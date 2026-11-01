---
name: structured-document-creation
description: Create coordinated internal/external documents from reference materials with consistent formatting and cross-references
---

# Structured Document Creation Workflow

This skill guides you through creating multiple audience-specific documents from a single reference source while maintaining consistency, proper formatting, and cross-references.

## When to Use This Skill

Use this pattern when you need to:
- Create both internal and external-facing documents from the same source material
- Maintain consistent information across multiple documents
- Extract structured requirements (timelines, roles, steps, policies)
- Ensure proper cross-referencing between related documents

## Step-by-Step Instructions

### Step 1: Read and Analyze Reference Material

First, thoroughly read the reference document to understand the source content.

```python
# Example: Read reference document
from docx import Document

def read_reference_doc(path):
    doc = Document(path)
    content = []
    for para in doc.paragraphs:
        content.append(para.text)
    return '\n'.join(content)

reference_content = read_reference_doc('Reference_Document.docx')
```

**Key actions:**
- Identify all key sections, policies, and requirements
- Note any timelines, deadlines, or time-sensitive information
- Extract role definitions and responsibilities
- Identify process steps and workflows
- Flag any sensitive/internal-only information

### Step 2: Extract Structured Requirements

Create a structured extraction of key information categories:

```python
structured_requirements = {
    'timelines': [],      # Deadlines, timeframes, schedules
    'roles': [],          # Who is responsible for what
    'steps': [],          # Process steps in order
    'policies': [],       # Rules and guidelines
    'contacts': [],       # Points of contact
    'internal_only': []   # Information not for external audiences
}
```

**Extraction checklist:**
- [ ] All dates and timeframes captured
- [ ] All roles and responsibilities identified
- [ ] All process steps documented in sequence
- [ ] All policies and rules extracted
- [ ] Sensitive information flagged for internal documents only

### Step 3: Plan Document Structure

Before creating documents, plan the structure for each audience:

| Document Type | Audience | Content Focus | Formatting |
|--------------|----------|---------------|------------|
| Internal | Staff/Team | Full details, sensitive info, operational specifics | Detailed, technical |
| External | Clients/Partners | Public-facing info, guidelines, high-level process | Clear, professional |

**Document planning template:**
```
Internal Document:
- Title: [Descriptive internal title]
- Sections: [List all sections with subsections]
- Cross-refs: [References to external document]
- Sensitive content: [Mark internal-only sections]

External Document:
- Title: [Client/partner-friendly title]
- Sections: [Public-appropriate sections only]
- Cross-refs: [References to internal document if applicable]
- Exclusions: [List what's omitted for external audience]
```

### Step 4: Create Documents with Consistent Formatting

Create each document maintaining:
- Consistent terminology across all documents
- Matching section numbering where applicable
- Aligned visual formatting (headers, bullets, tables)
- Clear cross-references between documents

```python
# Example: Create document with consistent structure
from docx import Document
from docx.shared import Inches, Pt
from docx.enum.text import WD_ALIGN_PARAGRAPH

def create_document_template(title, sections):
    doc = Document()
    
    # Title
    heading = doc.add_heading(title, 0)
    heading.alignment = WD_ALIGN_PARAGRAPH.CENTER
    
    # Add sections
    for section in sections:
        doc.add_heading(section['title'], level=1)
        for content in section['content']:
            doc.add_paragraph(content)
    
    return doc
```

### Step 5: Add Cross-References

Ensure documents reference each other appropriately:

**Internal document should include:**
- Reference to external document version
- Notes on what information is shared externally
- Links to related internal policies

**External document should include:**
- Reference to internal contact for questions
- Version/date information
- Clear scope boundaries

**Cross-reference example:**
```
For internal process details, refer to: [Internal Document Name]
For customer-facing guidelines, refer to: [External Document Name]
```

### Step 6: Quality Check

Before finalizing, verify:

**Consistency checks:**
- [ ] Same terminology used in both documents
- [ ] Dates and timelines match across documents
- [ ] Role definitions are consistent
- [ ] No contradictory information

**Audience appropriateness:**
- [ ] Internal document contains all necessary operational details
- [ ] External document excludes sensitive/internal-only information
- [ ] External document is clear and professional
- [ ] Both documents serve their intended purpose

**Cross-reference validation:**
- [ ] All cross-references are accurate
- [ ] Document titles/names match in references
- [ ] Version numbers are consistent

## Example Structure

```
Reference Document: Return_Issues.docx
    ↓
Structured Extraction
    ↓
┌─────────────────────────┬─────────────────────────┐
│  Internal_RA_Process    │  Key_Account_RA_Policy  │
│  (Internal Document)    │  (External Document)    │
├─────────────────────────┼─────────────────────────┤
│ • Full process details  │ • Customer guidelines   │
│ • Internal contacts     │ • External timelines    │
│ • Sensitive criteria    │ • Public policies       │
│ • Operational steps     │ • Contact information   │
│ • Cross-ref to external │ • Cross-ref to internal │
└─────────────────────────┴─────────────────────────┘
```

## Tips for Success

1. **Start with extraction** - Don't create documents until you've fully extracted structured requirements from the reference
2. **Maintain a content map** - Track which content goes in which document to avoid omissions or contradictions
3. **Use consistent headers** - Same section titles where content overlaps helps maintain alignment
4. **Review both together** - Always review documents side-by-side to catch inconsistencies
5. **Version together** - Keep both documents on the same version cycle to maintain alignment

## Common Pitfalls to Avoid

- ❌ Creating documents before fully understanding reference material
- ❌ Inconsistent terminology between internal and external versions
- ❌ Missing cross-references between related documents
- ❌ Exposing internal-only information in external documents
- ❌ Contradictory timelines or requirements across documents
- ❌ Forgetting to update both documents when source material changes