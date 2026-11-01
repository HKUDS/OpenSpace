---
name: remember
description: Review auto-memory entries and propose promotions to OPENSPACE.md, OPENSPACE.local.md, or retained auto-memory topics; detect duplicates, stale facts, conflicts, and ambiguous entries before making changes.
---

# Memory Review

## Goal
Review the user's memory landscape and produce a clear report of proposed changes, grouped by action type. Do not apply changes until the user approves them.

## Steps

### 1. Gather all memory layers
Read `OPENSPACE.md` and `OPENSPACE.local.md` from the project root if they exist. Review auto-memory from the memory section and `MEMORY.md`; use `memory_read` for topic files when needed. Note team memory only as unavailable unless this deployment explicitly has a team memory backend.

Success criteria: you have the contents of all available memory layers and can compare them.

### 2. Classify each auto-memory entry
For each substantive auto-memory entry, determine the best destination:

| Destination | What belongs there | Examples |
|---|---|---|
| `OPENSPACE.md` | Project conventions and instructions for OpenSpace that all contributors should follow | use `uv` not raw `pip`; API routes use kebab-case; test command is `pytest`; prefer functional style |
| `OPENSPACE.local.md` | Personal instructions specific to this user or machine, not applicable to other contributors | concise responses; explain tradeoffs; do not auto-commit; run tests before committing |
| Auto-memory topic | Durable cross-session user, feedback, project, or reference memory that does not belong in static project instructions | user role, durable feedback, external reference pointers, uncertain but useful context |
| Stay put | Working notes, temporary context, or entries that do not clearly fit elsewhere | session-specific observations, uncertain patterns |

Important distinctions:
- `OPENSPACE.md` and `OPENSPACE.local.md` contain instructions for the agent, not arbitrary external-tool preferences.
- Workflow practices such as PR conventions, merge strategy, and branch naming can be personal or team-wide; ask the user when unclear.
- When unsure, ask rather than guess.

Success criteria: each entry has a proposed destination or is flagged as ambiguous.

### 3. Identify cleanup opportunities
Scan across all layers for:

- Duplicates: auto-memory entries already captured in `OPENSPACE.md` or `OPENSPACE.local.md`.
- Outdated entries: static instructions contradicted by newer auto-memory.
- Conflicts: contradictions between any two layers; propose a resolution and note which evidence is newer.
- Overloaded index lines: `MEMORY.md` entries that contain content that belongs in a topic file.

Success criteria: all cross-layer issues are identified.

### 4. Present the report
Output a structured report grouped by action type:

1. Promotions: entries to move, with destination and rationale.
2. Cleanup: duplicates, outdated entries, conflicts, and index cleanup.
3. Ambiguous: entries where you need the user's input on destination.
4. No action needed: brief note on entries that should stay put.

If auto-memory is empty, say so and offer to review `OPENSPACE.md` for cleanup.

## Rules
- Present all proposals before making changes.
- Do not modify files without explicit user approval.
- Do not create new files unless the approved target does not exist yet.
- Ask about ambiguous entries; do not guess.
