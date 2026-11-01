---
name: soap-note-creation-b354d8
description: Create structured medical SOAP notes by writing comprehensive content to a file in one iteration
---

# SOAP Note Creation

This skill provides a reusable pattern for creating structured medical documentation (SOAP notes) by writing all required sections comprehensively in a single file write operation.

## When to Use

- Creating clinical documentation for patient visits
- Generate structured medical notes requiring standard SOAP format
- Tasks requiring Subjective, Objective, Assessment, and Plan sections

## Core Pattern

**Write the complete SOAP note directly to a file in one iteration** rather than building it incrementally. Include all four standard sections with comprehensive content.

## SOAP Note Structure

### 1. Subjective (S)
Document patient-reported information:
- **Chief Complaint (CC)**: Primary reason for visit in patient's own words
- **History of Present Illness (HPI)**: Detailed narrative of current symptoms (onset, duration, severity, modifying factors)
- **Past Medical History (PMH)**: Chronic conditions, surgeries, hospitalizations
- **Medications**: Current prescriptions, OTC drugs, supplements
- **Allergies**: Drug, food, environmental allergies with reactions
- **Family History**: Relevant hereditary conditions in family members
- **Social History**: Occupation, lifestyle, substance use, living situation

### 2. Objective (O)
Document observable, measurable findings:
- **Vital Signs**: BP, HR, RR, Temp, SpO2, height, weight, BMI
- **General Appearance**: Overall presentation, distress level
- **Physical Exam by System**:
  - HEENT (Head, Eyes, Ears, Nose, Throat)
  - Cardiovascular
  - Respiratory
  - Gastrointestinal
  - Neurological
  - Musculoskeletal
  - Skin
  - Psychiatric (if applicable)
- **Diagnostic Results**: Labs, imaging, tests (if available)

### 3. Assessment (A)
Document clinical reasoning:
- **Primary Diagnosis**: Main working diagnosis with ICD code if applicable
- **Differential Diagnoses**: Alternative diagnoses considered
- **Clinical Reasoning**: Why the primary diagnosis is most likely
- **Problem List**: Numbered or bulleted active issues

### 4. Plan (P)
Document management strategy:
- **Treatment Plan**: Medications, therapies, procedures
- **Follow-up**: Timing and purpose of next visit
- **Patient Education**: Counseling provided, instructions given
- **Referrals**: Specialist consultations if needed
- **Order Set**: Labs, imaging, tests to be obtained

## Implementation Template

```markdown
# SOAP Note - [Patient Name/ID]
**Date:** [Date of Visit]
**Provider:** [Provider Name]

## Subjective

### Chief Complaint
[Patient's stated reason for visit]

### History of Present Illness
[Detailed narrative of symptoms using OLDCARTS or similar framework]

### Past Medical History
[List of relevant conditions]

### Medications
[List with dosages]

### Allergies
[List with reactions]

### Family History
[Relevant family medical conditions]

### Social History
[Occupation, habits, lifestyle factors]

## Objective

### Vital Signs
- BP: [value]
- HR: [value]
- RR: [value]
- Temp: [value]
- SpO2: [value]
- Height: [value]
- Weight: [value]
- BMI: [value]

### Physical Examination
**General:** [Appearance, distress level]
**HEENT:** [Findings]
**Cardiovascular:** [Findings]
**Respiratory:** [Findings]
**Gastrointestinal:** [Findings]
**Neurological:** [Findings]
**Musculoskeletal:** [Findings]
**Skin:** [Findings]

### Diagnostic Results
[List any available lab/imaging results]

## Assessment

1. **[Primary Diagnosis]** - [ICD-10 code if applicable]
   - [Brief justification]

2. **[Differential Diagnosis]** - [Why less likely]

### Problem List
1. [Active problem 1]
2. [Active problem 2]

## Plan

### Treatment
- [Medication/dosage/frequency]
- [Non-pharmacologic interventions]

### Follow-up
- [Timeline and purpose]

### Patient Education
- [Topics discussed]
- [Instructions provided]

### Orders/Referrals
- [Labs/imaging ordered]
- [Specialist referrals]
```

## Best Practices

1. **Write comprehensively in one pass** - Gather all information first, then write the complete note
2. **Use clear section headers** - Make each SOAP component easily identifiable
3. **Include specific details** - Avoid vague statements; use measurable data
4. **Maintain professional tone** - Use appropriate medical terminology
5. **Ensure logical flow** - Assessment should follow from Objective findings; Plan should address Assessment
6. **Document negative findings** - Note relevant systems reviewed that were normal
7. **Include patient understanding** - Document that patient understood the plan

## Example Usage

When tasked with creating a SOAP note:

1. Gather all available patient information from the task description
2. Organize information into SOAP categories mentally or in notes
3. Write the complete file with all four sections in one `write_file` operation
4. Ensure no required section is missing before completing the task

## File Format

- Use markdown (.md) or plain text (.txt) for clarity
- Include appropriate headers for each section
- Use bullet points and numbered lists for readability
- Keep file size comprehensive (typically 3000-10000+ bytes for complete notes)