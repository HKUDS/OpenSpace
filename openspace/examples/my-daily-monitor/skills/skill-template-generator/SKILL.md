---
name: skill-template-generator
description: Generate properly-formatted SKILL.md files from extracted architectural patterns. Turns raw pattern descriptions into reusable skills that OpenSpace can discover, select, and evolve.
---

# Skill Template Generator

Given a structured pattern description (from `codebase-pattern-analyzer` or manual analysis), generate a valid SKILL.md file that OpenSpace can register, select for tasks, and evolve over time.

## When to Use

- After extracting a pattern from a reference codebase, you need to package it as a reusable skill
- You want to create a new skill that teaches an agent how to perform a specific kind of task
- You are converting informal documentation or code examples into the SKILL.md format

## SKILL.md Format

Every skill is a directory containing at minimum a `SKILL.md` file:

```
my-skill-name/
├── SKILL.md            # Required — the skill definition
├── scripts/            # Optional — helper scripts
├── references/         # Optional — reference data files
└── assets/             # Optional — images, templates
```

### Frontmatter (Required)

The file MUST start with YAML frontmatter containing exactly two fields:

```yaml
---
name: my-skill-name
description: One-sentence description of what this skill teaches. Must be specific enough for LLM skill selection to match it to relevant tasks.
---
```

**Rules:**
- `name` must match the directory name (kebab-case, lowercase)
- `description` should be 15-40 words, include key terms an LLM would search for
- No other frontmatter fields — everything else goes in the markdown body

### Body Structure

The markdown body follows this template:

```markdown
# [Skill Title]

[1-2 sentence summary of what this skill enables]

## When to Use

- [Trigger condition 1]
- [Trigger condition 2]
- [Trigger condition 3]

## [Core Content Sections]

[Step-by-step instructions, code templates, API references, etc.]

## Key Patterns

1. [Convention or best practice 1]
2. [Convention or best practice 2]
...
```

## Step 1: Determine Skill Category

Match the pattern to one of three OpenSpace categories:

| Category | Use When | Example |
|----------|----------|---------|
| `tool_guide` | The skill teaches how to use a specific tool or technique | "How to analyze a codebase", "How to use Finnhub API" |
| `workflow` | The skill prescribes an end-to-end procedure with ordered steps | "Create a panel component", "Set up API proxy endpoint" |
| `reference` | The skill provides knowledge that informs decisions | "WorldMonitor architecture index", "News API comparison" |

The category affects how OpenSpace judges the skill during execution analysis:
- **workflow**: Was the agent able to follow the prescribed steps?
- **tool_guide**: Did the agent use the described tool/approach?
- **reference**: Did the knowledge influence agent decisions?

## Step 2: Write the Description

The `description` field is the **most critical line** — it determines whether OpenSpace selects this skill for a given task.

**Good descriptions** (specific, searchable):
- "Create a dashboard panel component using vanilla TypeScript DOM API, following the worldmonitor Panel architecture."
- "Integrate with Finnhub Stock API for real-time and historical stock market data."
- "CSS grid layout system for a responsive panel dashboard with dark theme."

**Bad descriptions** (vague, generic):
- "A useful skill for building things."
- "TypeScript patterns."
- "How to make a panel."

**Formula**: `[Action verb] + [specific subject] + [key technology/approach] + [context/project reference]`

## Step 3: Write Actionable Instructions

The body must be concrete enough that an AI agent can follow it without guessing.

### For Workflow Skills

Include:
1. **File paths** — exact paths where files should be created (`src/components/MyPanel.ts`)
2. **Code templates** — complete, runnable code blocks (not pseudocode)
3. **Import statements** — every import the code needs
4. **Interface definitions** — typed data shapes
5. **Integration points** — how this connects to other parts of the project

Use `{baseDir}` for paths relative to the skill directory:
```
Read the reference file at {baseDir}/references/example.json
```

### For Tool Guide Skills

Include:
1. **API endpoints** — full URLs with parameter documentation
2. **Auth mechanism** — how to authenticate (query param, header, etc.)
3. **Response shapes** — JSON examples with field descriptions
4. **Rate limits** — free tier limitations
5. **Error handling** — common errors and how to handle them

### For Reference Skills

Include:
1. **Structured index** — tables, lists, or maps of the reference material
2. **Key file paths** — where to find specific things in the reference codebase
3. **Architecture decisions** — WHY certain patterns were chosen
4. **Comparison tables** — alternatives and tradeoffs

## Step 4: Add Dependency Hints

If the skill depends on patterns from other skills, mention them explicitly:

```markdown
## Prerequisites

This skill builds on:
- `panel-component` — for the Panel base class
- `data-service` — for the circuit breaker pattern
- `api-proxy-endpoint` — for server-side API key isolation
```

This helps OpenSpace understand skill composition when DERIVING new skills.

## Step 5: Validate the Skill

Before saving, verify:

1. **Frontmatter is valid YAML** — no tabs, proper indentation, quotes around special chars
2. **Name matches directory** — `name: foo-bar` lives in `foo-bar/SKILL.md`
3. **Code blocks are complete** — every snippet can be copy-pasted and run
4. **No broken references** — all file paths and URLs are valid
5. **Description is specific** — would an LLM pick this skill for the right task?

## Example: Converting a Pattern to a Skill

**Input** (from codebase-pattern-analyzer):

```
Pattern: Circuit Breaker Data Service
Source: worldmonitor/src/services/*.ts
Category: service
Structure: Module-level CircuitBreaker instance, async fetch functions, typed interfaces
Key code: createCircuitBreaker({ name, cacheTtlMs }), breaker.execute(fn, default)
```

**Output** (`data-service/SKILL.md`):

```markdown
---
name: data-service
description: Create data fetching services with circuit breaker pattern for API resilience. Services handle fetch, cache, retry, and expose typed data to panel components.
---

# Data Service Pattern

Each panel's data comes from a dedicated service module in `src/services/`. ...

## Circuit Breaker

[Full implementation code]

## Service Module Pattern

[Template with typed interfaces, breaker usage, export functions]

## Key Patterns

1. One circuit breaker per API endpoint
2. Export typed interfaces for data shapes
3. Wrap fetch calls in breaker.execute(fn, defaultValue)
...
```

## Evolution Hooks

Skills generated by this workflow are designed to evolve:

- **FIX**: When an API changes or code pattern breaks, OpenSpace updates the skill in-place
- **DERIVED**: When a new panel needs a similar service, OpenSpace derives a specialized version
- **CAPTURED**: When an agent discovers a novel pattern during execution, OpenSpace captures it as a new skill

The better the initial skill quality, the better the evolved descendants.

