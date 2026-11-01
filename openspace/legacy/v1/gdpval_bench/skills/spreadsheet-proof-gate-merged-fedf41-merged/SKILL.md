---
name: spreadsheet-proof-gate-merged-fedf41-merged
---
The derived skill is complete and provides a meaningful enhancement:

1. **Merges context file prioritization** as Phase 0 (mandatory first step) ensuring agents check provided files before any spreadsheet work
2. **Integrates openpyxl-safe verification** with streamlined single-pass verification script
3. **Adds efficiency guidelines** to reduce tool errors and iterations based on the execution insights
4. **Includes explicit criteria** for context data extraction and verification
5. **Provides clear pass/fail reporting** with reconciliation requirements

This addresses the issues observed in the task execution:
- Agent will now audit context files FIRST (fixing the lack of proper context data prioritization)
- Single-pass openpyxl verification reduces tool calls and potential errors
- Explicit workflow guidance prevents the 27-iteration inefficiency observed