---
name: medical-soap-note-creation
description: Transform unstructured clinical encounters into comprehensive SOAP notes with ICD codes and care plans
---

# Medical SOAP Note Creation

This skill provides a systematic approach to converting unstructured clinical encounter summaries into professional, comprehensive SOAP notes ready for electronic health record documentation.

## Overview

SOAP notes organize clinical information into four standard sections:
- **S**ubjective: Patient's reported symptoms and history
- **O**bjective: Measurable clinical findings and data
- **A**ssessment: Clinical diagnosis and reasoning
- **P**lan: Treatment strategy and follow-up

## Step-by-Step Instructions

### Step 1: Extract Key Information
Review the clinical encounter summary and identify:
- Patient demographics (age, sex, relevant history)
- Chief complaint and history of present illness
- Review of systems findings
- Physical examination results
- Diagnostic test results (labs, imaging)
- Current medications and allergies
- Past medical/surgical history

### Step 2: Structure the SOAP Note
Organize information into the four SOAP sections:

**Subjective (S):**
- Chief complaint in patient's own words
- History of present illness (onset, duration, severity, aggravating/relieving factors)
- Review of systems (pertinent positives and negatives)
- Relevant past medical, family, and social history

**Objective (O):**
- Vital signs
- Physical examination findings by system
- Laboratory and imaging results
- Current medication list

**Assessment (A):**
- Primary diagnosis with ICD-10 code(s)
- Differential diagnoses if applicable
- Clinical reasoning connecting findings to diagnosis

**Plan (P):**
- Medications (new prescriptions, changes, discontinuations)
- Treatments and procedures
- Patient education provided
- Follow-up arrangements
- Return precautions

### Step 3: Write the Complete Note
Compose the full SOAP note in a single `write_file` operation to ensure completeness and efficiency:

```python
from write_file import write_file

soap_note = """SOAP NOTE
Date: [Encounter Date]
Patient: [Patient Name/ID]

SUBJECTIVE:
[Patient's reported symptoms and history in organized paragraphs]

OBJECTIVE:
[Clinical findings and data in organized sections]

ASSESSMENT:
[Diagnosis with clinical reasoning and ICD codes]

PLAN:
[Specific, actionable treatment steps and follow-up]
"""

write_file(path="soap_note.txt", content=soap_note)
```

### Step 4: Quality Checklist
Before finalizing, verify:
- [ ] All four SOAP components present and clearly labeled
- [ ] ICD-10 codes included for all diagnoses
- [ ] Plan contains specific, actionable items with timelines
- [ ] Follow-up instructions are clear and specific
- [ ] Return precautions included
- [ ] Note is comprehensive (typically 3000-10000 characters for complex cases)

## Best Practices

1. **Be Specific**: Use quantifiable measurements and precise clinical terminology
2. **Include ICD Codes**: Always pair diagnoses with appropriate ICD-10 codes
3. **Actionable Plans**: Ensure each plan item has clear next steps, dosages, and timelines
4. **Single Operation**: Write the complete note in one `write_file` operation for efficiency and consistency
5. **Professional Tone**: Use clinical language appropriate for medical records
6. **Patient-Centered**: Include patient education and shared decision-making when applicable

## Example Structure

```
SOAP NOTE
Date: 2024-01-15
Patient: [Name], [Age], [Sex]

SUBJECTIVE:
CC: [Chief complaint]

HPI: [History of present illness using OLDCARTS or similar framework - 
      onset, location, duration, characteristics, aggravating/relieving factors, 
      timing, severity]

ROS: [Review of systems - pertinent positives and negatives by system]

PMH: [Past medical history]
PSH: [Past surgical history]
Medications: [Current medications with dosages]
Allergies: [Known allergies and reactions]
FH: [Family history]
SH: [Social history]

OBJECTIVE:
VS: T [temp], BP [blood pressure], HR [heart rate], RR [respiratory rate], 
    SpO2 [oxygen saturation], Wt [weight]

General: [Appearance, distress level]
HEENT: [Head, eyes, ears, nose, throat findings]
CV: [Cardiovascular examination]
Resp: [Respiratory examination]
Abd: [Abdominal examination]
MSK: [Musculoskeletal examination]
Neuro: [Neurological examination]
Skin: [Dermatological findings]

Labs: [Relevant laboratory results with values and reference ranges]
Imaging: [Imaging study results]

ASSESSMENT:
1. [Primary diagnosis] - ICD-10: [code]
   [Brief clinical reasoning supporting diagnosis]

2. [Secondary diagnosis if applicable] - ICD-10: [code]
   [Brief clinical reasoning]

PLAN:
1. Medications:
   - [Medication name] [dosage] [route] [frequency] for [duration]
   
2. Treatments:
   - [Specific treatment or procedure]
   
3. Patient Education:
   - [Education topics discussed]
   
4. Follow-up:
   - Return to clinic in [timeframe] for [purpose]
   - [Any scheduled tests or appointments]
   
5. Return Precautions:
   - Return immediately if [warning symptoms]
```

## Common ICD-10 Code Categories

- Respiratory: J00-J99 (e.g., J06.9 acute upper respiratory infection)
- Digestive: K00-K95 (e.g., K21.0 GERD with esophagitis)
- Musculoskeletal: M00-M99 (e.g., M54.5 low back pain)
- Cardiovascular: I00-I99 (e.g., I10 essential hypertension)
- Endocrine: E00-E89 (e.g., E11.9 type 2 diabetes mellitus)
- Infectious: A00-B99 (e.g., J02.9 acute pharyngitis)

## Notes on Efficiency

Writing the complete SOAP note in a single `write_file` operation offers several advantages:
- Reduces iteration overhead
- Ensures consistency across all sections
- Prevents partial or incomplete documentation
- Maintains coherent clinical reasoning throughout
- Faster completion time for clinical documentation tasks