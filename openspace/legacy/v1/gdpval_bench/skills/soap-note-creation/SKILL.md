---
name: soap-note-creation
description: Create structured medical SOAP notes with all four components in a single comprehensive file write
---

# SOAP Note Creation

This skill captures the pattern of creating complete, structured medical documentation (SOAP notes) by writing all required sections to a file in one iteration.

## When to Use

Use this skill when you need to create medical meeting notes, patient visit documentation, or clinical encounter records that follow the standard SOAP format.

## Core Technique

Write the **entire SOAP note in one comprehensive file write** rather than building it incrementally. This ensures completeness and consistency across all sections.

## Required Sections

Every SOAP note must include these four components:

### 1. Subjective (S)
- **Chief Complaint (CC)**: Patient's primary reason for visit
- **History of Present Illness (HPI)**: Detailed narrative of current symptoms
- **Past Medical History (PMH)**: Relevant medical history, medications, allergies
- **Family/Social History**: Pertinent family and social context

### 2. Objective (O)
- **Vital Signs**: BP, HR, RR, Temp, SpO2, weight, height
- **Physical Examination**: System-by-system findings (HEENT, Cardiovascular, Respiratory, Abdomen, Neurological, etc.)
- **Diagnostic Data**: Lab results, imaging findings if available

### 3. Assessment (A)
- **Primary Diagnosis**: Main clinical diagnosis
- **Differential Diagnoses**: Alternative considerations
- **Clinical Reasoning**: Brief justification for assessment

### 4. Plan (P)
- **Management**: Treatments, medications, interventions
- **Follow-up**: Timing and conditions for return
- **Patient Education**: Instructions, lifestyle modifications, warning signs

## Execution Pattern

```
1. Gather all patient information and clinical data
2. Structure content into the four SOAP sections
3. Write the complete note to file in ONE operation
4. Verify all four sections are present and complete
```

## Example Structure

```markdown
# SOAP Note - [Patient Name/ID]
## Date: [Visit Date]

## SUBJECTIVE
### Chief Complaint
[Patient's reason for visit in their own words]

### History of Present Illness
[Detailed symptom narrative using OLDCARTS or similar framework]

### Past Medical History
[Relevant conditions, surgeries, medications, allergies]

### Family/Social History
[Relevant family history and social context]

## OBJECTIVE
### Vital Signs
- BP: [value]
- HR: [value]
- Temp: [value]
- [etc.]

### Physical Examination
- **HEENT**: [findings]
- **Cardiovascular**: [findings]
- **Respiratory**: [findings]
- **Abdomen**: [findings]
- **Neurological**: [findings]
- [etc.]

### Diagnostic Data
[Labs, imaging, other test results]

## ASSESSMENT
### Primary Diagnosis
[Diagnosis with ICD code if applicable]

### Differential Diagnoses
1. [Alternative 1]
2. [Alternative 2]

### Clinical Reasoning
[Brief explanation linking findings to diagnosis]

## PLAN
### Management
- [Medications with dose/frequency]
- [Procedures/interventions]
- [Referrals if needed]

### Follow-up
[Timeline and conditions for return visit]

### Patient Education
[Instructions, lifestyle mods, warning signs to watch]
```

## Best Practices

1. **Completeness First**: Include all four sections before writing—do not build incrementally
2. **Specificity**: Use concrete values (vitals, dates, doses) rather than placeholders
3. **Clinical Accuracy**: Ensure assessment logically follows from subjective and objective data
4. **Actionable Plan**: Plan items should be specific and implementable
5. **File Size**: Comprehensive notes typically range 3000-8000 bytes depending on complexity

## Common Mistakes to Avoid

- Writing sections separately across multiple iterations
- Leaving sections incomplete or with placeholder text
- Omitting differential diagnoses in Assessment
- Creating vague, non-actionable Plan items
- Missing vital signs or key physical exam findings