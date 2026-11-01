---
name: create-soap-note
description: Generate structured medical SOAP notes with all required sections in a single comprehensive file write.
---

# Create SOAP Note

This skill defines the workflow for creating structured medical documentation (SOAP notes) by writing comprehensive content directly to a file. It ensures all standard sections are included and properly formatted.

## Objective

Produce a complete medical visit record containing Subjective, Objective, Assessment, and Plan sections without fragmenting the output across multiple files or incomplete drafts.

## Prerequisites

- Patient demographic information (age, gender, ID).
- Visit details (date, provider, reason for visit).
- Clinical data (vitals, symptoms, exam findings, history).

## Workflow Steps

### 1. Prepare Content Structure
Organize the note into the four standard SOAP sections. Do not omit any section even if data is sparse (note "not applicable" or "deferred" where appropriate).

### 2. Draft Comprehensive Content
Write the full content for each section in one continuous operation. Avoid placeholders like `[insert here]` unless data is genuinely missing and must be flagged for follow-up.

- **Subjective (S):**
  - Chief Complaint (CC)
  - History of Present Illness (HPI)
  - Past Medical History (PMH)
  - Family/Social History (FH/SH)
  - Review of Systems (ROS)
- **Objective (O):**
  - Vitals (BP, HR, Temp, Resp, O2 Sat, Weight/Height)
  - Physical Exam (by system)
  - Diagnostic Results (Labs, Imaging)
- **Assessment (A):**
  - Primary Diagnosis
  - Differential Diagnoses
  - Problem List
- **Plan (P):**
  - Management/Treatment
  - Medications
  - Follow-up Instructions
  - Patient Education

### 3. Write to File
Save the complete note to a single file (e.g., `soap_note_<patient_id>_<date>.md` or `.txt`). Ensure the file is saved in one write operation to maintain consistency.

### 4. Review for Completeness
Verify that all four headers exist and contain substantive content.

## Template Example

```markdown
# SOAP Note - [Patient Name] - [Date]

## Subjective
**Chief Complaint:** [Reason for visit]
**HPI:** [Detailed history]
**PMH:** [Conditions, surgeries]
**Social/Family History:** [Relevant details]

## Objective
**Vitals:** [List values]
**Physical Exam:** [Findings by system]
**Labs/Imaging:** [Results]

## Assessment
**Diagnoses:**
1. [Primary Diagnosis]
2. [Differential]

## Plan
**Management:** [Steps taken]
**Medications:** [Prescriptions]
**Follow-up:** [Timeline]
**Education:** [Instructions given]
```

## Best Practices

- **Privacy:** Ensure no real PHI (Protected Health Information) is exposed in public logs if not authorized.
- **Clarity:** Use medical terminology appropriately but keep patient instructions clear.
- **Efficiency:** Aim to generate the full document in one iteration to reduce overhead.

## Troubleshooting

- **Missing Data:** If specific clinical data is missing, explicitly state "Information not provided" in the relevant section rather than skipping the section.
- **File Size:** If the note is exceptionally long, ensure the file write command supports the content length.