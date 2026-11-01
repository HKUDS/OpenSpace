# Skills

Place custom skills here. Each skill is a subdirectory containing a `SKILL.md`:

```
skills/
├── my-skill/
│   └── SKILL.md
└── another-skill/
    ├── SKILL.md
    └── helper.sh       (optional auxiliary files)
```

`SKILL.md` must start with YAML frontmatter containing `name` and `description`. The markdown body is the agent instruction, loaded only when selected for a task.

## Discovery & Loading

This directory is the **lowest-priority** skill source, always scanned at startup:

1. `OPENSPACE_HOST_SKILL_DIRS` env (highest priority)
2. `config_grounding.json → skills.skill_dirs`
3. **This directory** (lowest priority)

On first discovery, each skill gets a `.skill_id` sidecar file (`{name}__imp_{uuid[:8]}`) for persistent tracking across restarts. Cloud-downloaded and evolution-captured skills may also land here when no other skill directory is configured.

Loose files like this README are safely ignored — only subdirectories with `SKILL.md` are scanned.

## Safety

All skills pass `check_skill_safety` before loading. Skills with dangerous patterns (prompt injection, credential exfiltration, etc.) are **blocked automatically** and logged as warnings.
