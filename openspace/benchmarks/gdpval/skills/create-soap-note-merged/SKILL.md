---
name: comprehensive-soap-note
description: Generate complete medical SOAP notes with all four sections in a single comprehensive file write operation
---

# Comprehensive SOAP Note Creation

This skill defines the workflow for creating structured medical documentation (SOAP notes) by writing all required sections to a file in one comprehensive operation. This ensures completeness, consistency, and efficiency in clinical documentation.

## When to Use

Use this skill when you need to create:
- Medical visit documentation
- Patient encounter records
- Clinical meeting notes
- Healthcare follow-up documentation

that follow the standard SOAP (Subjective, Objective, Assessment, Plan) format.

## Objective

Produce a complete medical visit record containing all four SOAP sections without fragmenting the output across multiple files or incomplete drafts. The entire note should be written in a single operation to maintain consistency.

## Prerequisites

Before creating the SOAP note, gather:
- **Patient demographics**: age, gender, patient ID
- **Visit details**: date, provider name, reason for visit
- **Clinical data**: vitals, symptoms, exam findings, medical history
- **Diagnostic results**: labs, imaging (if available)

## Core Technique

Write the **entire SOAP note in one comprehensive file write** rather than building it incrementally. This ensures:
- All sections are complete before writing
- Consistency across the document
- Reduced overhead from multiple write operations
- No risk of partial or fragmented notes

## Required Sections

Every SOAP note must include these four components:

### 1. Subjective (S)
Patient-reported information including:
- **Chief Complaint (CC)**: Patient's primary reason for visit (in their own words when possible)
- **History of Present Illness (HPI)**: Detailed narrative of current symptoms (use OLDCARTS or similar framework: Onset, Location, Duration, Characteristics, Aggravating/Relieving factors, Timing, Severity)
- **Past Medical History (PMH)**: Relevant conditions, surgeries, medications, allergies
- **Family History (FH)**: Pertinent family medical history
- **Social History (SH)**: Relevant social context (occupation, lifestyle, habits)
- **Review of Systems (ROS)**: Systematic symptom review if applicable

### 2. Objective (O)
Clinician-observed and measured data:
- **Vital Signs**: BP, HR, RR, Temp, SpO2, weight, height (always include concrete values)
- **Physical Examination**: System-by-system findings (HEENT, Cardiovascular, Respiratory, Abdomen, Neurological, Musculoskeletal, Skin, etc.)
- **Diagnostic Data**: Lab results, imaging findings, EKG, other test results if available

### 3. Assessment (A)
Clinical synthesis and diagnosis:
- **Primary Diagnosis**: Main clinical diagnosis (include ICD code if applicable)
- **Differential Diagnoses**: Alternative considerations (at least 1-2 alternatives)
- **Clinical Reasoning**: Brief justification linking subjective and objective findings to the assessment
- **Problem List**: Current active problems if applicable

### 4. Plan (P)
Actionable next steps:
- **Management/Treatment**: Specific interventions, procedures, therapies
- **Medications**: Prescriptions with dose, frequency, route, duration
- **Follow-up**: Timeline and conditions for return visit
- **Patient Education**: Instructions, lifestyle modifications, warning signs to watch
- **Referrals**: Specialist referrals if needed

## Execution Pattern

```
1. Gather all patient information and clinical data
2. Structure content into the four SOAP sections (do not skip any section)
3. Write the complete note to file in ONE operation
4. Verify all four sections are present and contain substantive content
```

## Template Example

```markdown
# SOAP Note - [Patient Name/ID]
## Date: [Visit Date]
## Provider: [Provider Name]

## SUBJECTIVE
### Chief Complaint
[Patient's reason for visit in their own words]

### History of Present Illness
[Detailed symptom narrative using OLDCARTS framework]

### Past Medical History
[Relevant conditions, surgeries, medications, allergies]

### Family History
[Relevant family medical history]

### Social History
[Occupation, lifestyle, habits, social context]

## OBJECTIVE
### Vital Signs
- BP: [value] mmHg
- HR: [value] bpm
- RR: [value] breaths/min
- Temp: [value] °F/°C
- SpO2: [value]%
- Weight: [value] kg/lbs
- Height: [value] cm/ft

### Physical Examination
- **HEENT**: [findings]
- **Cardiovascular**: [findings]
- **Respiratory**: [findings]
- **Abdomen**: [findings]
- **Neurological**: [findings]
- **Musculoskeletal**: [findings]
- **Skin**: [findings]

### Diagnostic Data
[Labs, imaging, other test results with dates and values]

## ASSESSMENT
### Primary Diagnosis
[Diagnosis with ICD code if applicable]

### Differential Diagnoses
1. [Alternative diagnosis 1]
2. [Alternative diagnosis 2]

### Clinical Reasoning
[Brief explanation linking findings to diagnosis]

## PLAN
### Management
- [Medications with dose/frequency/duration]
- [Procedures/interventions]
- [Referrals if needed]

### Follow-up
[Timeline and conditions for return visit]

### Patient Education
[Instructions, lifestyle modifications, warning signs to watch]
```

## File Naming

Save the complete note using a consistent naming convention:
- `soap_note_<patient_id>_<date>.md` or
- `soap_note_<patient_id>_<date>.txt`

## Best Practices

1. **Completeness First**: Include all four sections before writing—do not build incrementally
2. **Specificity**: Use concrete values (vitals, dates, doses) rather than placeholders
3. **Clinical Accuracy**: Ensure assessment logically follows from subjective and objective data
4. **Actionable Plan**: Plan items should be specific and implementable
5. **No Section Omission**: If data is genuinely missing, explicitly state "Information not provided" or "Not applicable" rather than skipping the section
6. **Privacy**: Ensure no real PHI (Protected Health Information) is exposed in public logs if not authorized
7. **Clarity**: Use medical terminology appropriately but keep patient instructions clear and understandable
8. **Single Write Operation**: Aim to generate the full document in one iteration to reduce overhead and maintain consistency

## Common Mistakes to Avoid

- Writing sections separately across multiple iterations or files
- Leaving sections incomplete or with placeholder text like `[insert here]`
- Omitting differential diagnoses in the Assessment section
- Creating vague, non-actionable Plan items
- Missing vital signs or key physical exam findings in Objective
- Skipping sections entirely when data is sparse (instead mark as "not applicable")

## Troubleshooting

### Missing Data
If specific clinical data is missing, explicitly state "Information not provided" or "Deferred" in the relevant section rather than skipping the section entirely.

### File Size Issues
Comprehensive SOAP notes typically range 3,000-10,000+ characters depending on complexity. If the note is exceptionally long:
- Ensure the file write command supports the content length
- Consider using markdown format for better structure
- Verify the complete content was written (check file size after write)

### Incomplete Sections
After writing, verify all four headers (Subjective, Objective, Assessment, Plan) exist and contain substantive content. If any section is incomplete, rewrite the entire note in one operation.

## Quality Checklist

Before considering the SOAP note complete, verify:
- [ ] All four SOAP sections are present
- [ ] Subjective includes CC, HPI, and relevant history
- [ ] Objective includes vitals with concrete values
- [ ] Objective includes physical exam findings by system
- [ ] Assessment includes primary diagnosis AND at least one differential
- [ ] Plan includes specific, actionable items (medications, follow-up, education)
- [ ] No placeholder text like `[insert here]` unless genuinely unavailable
- [ ] File was written in a single operation
- [ ] File naming follows the convention

## Example File Size Reference

- Minimal SOAP note: ~2,000-3,000 characters
- Standard SOAP note: ~4,000-7,000 characters
- Complex SOAP note: ~8,000-12,000+ characters
