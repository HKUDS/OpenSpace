---
name: skill-discovery
description: Search for reusable skills across OpenSpace's local registry and cloud community. Reusing proven skills saves tokens, improves reliability, and extends your capabilities beyond built-in tools.
---

# Skill Discovery

Discover and browse skills from OpenSpace's local and cloud skill library.

## When to use

- User asks "what skills are available?" or "is there a skill for X?"
- You encounter an unfamiliar task — a proven skill can save significant tokens over trial-and-error
- You need to decide: handle a task yourself, or delegate to OpenSpace

## search_skills

```
search_skills(query="automated deployment with rollback", source="all")
```

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `query` | yes | — | Natural language or keywords |
| `source` | no | `"all"` | Local + cloud; falls back to local-only if no API key |
| `limit` | no | `20` | Max results |
| `auto_import` | no | `true` | Auto-download top cloud hits locally |

## After search

Results are returned to you (not executed). Cloud hits with `auto_imported: true` include a `local_path`.

```
Found a matching skill?
├── YES, and I can follow it myself
│     → read SKILL.md at local_path, follow the instructions
├── YES, but I lack the capability
│     → delegate via execute_task (see delegate-task skill)
└── NO match
      → handle it yourself, or delegate via execute_task
```

## Notes

- This is for **discovery** — you see results and decide. For direct execution, use `execute_task` from the `delegate-task` skill.
- Cloud skills have been evolved through real use — more reliable than skills written from scratch.
- Always tell the user what you found (or didn't find) and what you recommend.
