---
name: codebase-pattern-analyzer
description: Analyze a reference codebase to discover and extract reusable architectural patterns. Produces structured pattern descriptions that can be turned into standalone skills. This is a meta-skill that bootstraps the skill evolution chain.
---

# Codebase Pattern Analyzer

Systematically analyze a reference codebase and extract reusable architectural patterns into structured descriptions. These descriptions become the raw material for new skills (via the `skill-template-generator` skill).

## When to Use

- You need to understand how a large codebase is structured before replicating it
- You want to extract a specific pattern (component architecture, service layer, API proxy, etc.) from a reference project
- You are bootstrapping a new project that should follow the conventions of an existing one

## Step 1: Map the Directory Structure

Read the top-level directory and identify the major code zones:

```
list_dir <project_root>
list_dir <project_root>/src
```

Classify each directory into one of these roles:

| Role | Typical Dirs | What to Look For |
|------|-------------|------------------|
| **Components** | `src/components/` | UI classes, base class inheritance, DOM manipulation |
| **Services** | `src/services/` | Data fetching, API calls, circuit breakers, caching |
| **Config** | `src/config/` | Constants, panel definitions, API endpoints, feature flags |
| **Styles** | `src/styles/` | CSS custom properties, theme variables, grid layout |
| **Utils** | `src/utils/` | Helper functions, formatting, DOM utilities |
| **API Layer** | `api/`, `server/` | Serverless functions, route handlers, CORS, proxy logic |
| **Entry Point** | `src/main.ts` | Bootstrap, panel instantiation, scheduler setup |

## Step 2: Identify the Component Pattern

Find the base component class (usually the most imported file in `components/`):

```
read_file <project_root>/src/components/Panel.ts
```

Extract these structural elements:

1. **Constructor options interface** — what config each component accepts (id, title, className, etc.)
2. **DOM structure** — how the element tree is built (header, content area, resize handles)
3. **Lifecycle methods** — constructor, destroy, show/hide, refresh
4. **State management** — loading/error/content states, fetching guards
5. **Data binding** — how data flows from service → render → DOM

Record the pattern as:
```
COMPONENT PATTERN:
  Base class: [name]
  Options: [list of constructor params]
  DOM tree: [element hierarchy]
  States: [loading, error, content]
  Lifecycle: [init → fetch → render → destroy]
  Key methods: [getElement, showLoading, showError, setContent, refresh, destroy]
```

## Step 3: Identify the Service Layer Pattern

Examine 2-3 service files to find the common structure:

```
read_file <project_root>/src/services/news.ts
read_file <project_root>/src/services/stock-market.ts
```

Look for:

1. **Circuit breaker / resilience** — retry logic, cooldown, cached fallback
2. **Interface definitions** — typed data shapes returned by each service
3. **Fetch pattern** — how HTTP calls are made (direct fetch, proxy, batching)
4. **Caching strategy** — TTLs, stale-while-revalidate, in-memory cache
5. **Error handling** — what happens on failure, default return values

Record as:
```
SERVICE PATTERN:
  Resilience: [circuit breaker with N failures → cooldown]
  Cache: [in-memory, TTL=Xms]
  Fetch: [browser fetch → /api/... proxy → external API]
  Error: [catch → recordFailure → return default]
  Exports: [async functions, not class instances]
```

## Step 4: Identify the API Proxy Pattern

Read the server-side proxy layer:

```
list_dir <project_root>/api/
read_file <project_root>/api/_cors.js
read_file <project_root>/api/stocks.ts    # or similar endpoint
```

Extract:

1. **File-per-endpoint structure** — one file per API domain
2. **CORS handling** — origin allowlist, preflight response
3. **API key isolation** — `process.env.*` usage, never exposed to frontend
4. **Cache headers** — `Cache-Control`, `s-maxage`, `stale-while-revalidate`
5. **Error wrapping** — structured JSON errors, never raw upstream responses
6. **Input validation** — query parameter checks before upstream call

## Step 5: Identify the Styling System

```
read_file <project_root>/src/styles/main.css   # first 100 lines for CSS variables
```

Extract:

1. **CSS custom properties** — color tokens, font stacks, spacing
2. **Theme structure** — dark/light mode switch mechanism
3. **Grid layout** — `grid-template-columns`, `auto-fill`, `minmax()` values
4. **Component styles** — panel base styles, header, content, scrollbar
5. **Semantic colors** — positive/negative, severity levels, status indicators
6. **Typography** — font family, sizes, letter-spacing, text-transform

## Step 6: Identify the Scheduling / Refresh Pattern

```
read_file <project_root>/src/app/refresh-scheduler.ts
```

Extract:

1. **Registration API** — how panels register their refresh functions
2. **Interval management** — per-panel configurable intervals
3. **Visibility awareness** — pause when tab hidden, flush stale on return
4. **In-flight guards** — prevent duplicate concurrent refreshes
5. **Stagger logic** — avoid API burst when resuming

## Step 7: Synthesize Into Pattern Descriptions

For each pattern discovered, produce a structured description:

```markdown
## Pattern: [Name]

**Source file(s)**: [paths in reference codebase]
**Category**: component | service | api | style | scheduler | utility

### Structure
[Key structural elements — interfaces, classes, functions]

### Key Code
[Minimal representative code snippet, 20-40 lines]

### Conventions
[Naming, file organization, import paths]

### Dependencies
[Other patterns this depends on]

### Adaptation Notes
[What must change when applying to a different project]
```

## Output

The final output should be a list of pattern descriptions, one per architectural concern. These become the input to the `skill-template-generator` skill, which turns each pattern into a standalone SKILL.md.

## Tips

- **Read broadly first, deeply second** — scan directory listings before reading individual files
- **Follow imports** — if a component imports from `../services/`, read that service next
- **Count instances** — if 10+ components extend the same base class, that's a core pattern
- **Note what's NOT used** — no framework (React, Vue) means vanilla DOM; no ORM means raw fetch
- **Compare 2-3 examples** of the same pattern to distinguish the template from the instance-specific parts

