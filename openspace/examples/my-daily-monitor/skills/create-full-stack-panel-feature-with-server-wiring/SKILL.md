---
name: create-full-stack-panel-feature-with-server-wiring
description: Multi-layer workflow for creating complete panel features by analyzing existing patterns and generating coordinated server routes, services, UI components, registration, styling files, AND explicitly wiring new handlers into the server's dispatch table (supports both Express middleware and manual http.createServer switch/case patterns).
---

# Create Full-Stack Panel Feature (with Server Wiring)

This skill guides you through creating a complete, pattern-consistent panel feature across all application layers: backend API routes, service logic, frontend components, registration, styling — **and crucially, wiring the new route handler into the server's dispatch table**, whether that uses Express or a manual `http.createServer` pattern.

## When to Use

- Adding a new panel type to a dashboard or admin interface
- Creating a feature that spans multiple architectural layers
- Ensuring consistency with existing codebase patterns across the stack
- Projects that use a manual `http.createServer` dispatch loop instead of Express router mounting

## Workflow Steps

### 1. Identify Pattern Files

Locate existing examples for each layer you need to implement:

```bash
# Find existing server routes
find . -name "*route*.js" -o -name "*routes*.js" -o -name "*route*.ts" -o -name "*routes*.ts" | grep -E "(panel|dashboard|server)"

# Find service files
find . -name "*service*.js" -o -name "*service*.ts" | grep -E "(panel|dashboard|server)"

# Find UI components
find . -name "*.jsx" -o -name "*.tsx" -o -name "*.ts" | grep -i panel

# Find the server entry point (critical for wiring step)
find . -name "index.ts" -o -name "index.js" -o -name "server.ts" -o -name "server.js" | grep -v node_modules | head -10

# Find registration/config files
find . -name "*config*.js" -o -name "*registry*.js" -o -name "*config*.ts" -o -name "*registry*.ts"

# Find styling files
find . -name "*.css" -o -name "*.scss" | grep -i panel
```

### 2. Read and Analyze Patterns

Read 1-2 representative examples from **each layer** to understand:

- Naming conventions (e.g., `MetricsPanel`, `metrics-service.ts`)
- Code structure and organization
- Import/export patterns
- Registration mechanisms
- API endpoint patterns
- Error handling approaches
- Styling conventions

**Also read the server entry point** (`server/index.ts`, `src/server.ts`, etc.) to determine which dispatch pattern is used:

```bash
# Read the server entry point to identify routing mechanism
cat server/index.ts   # or server/index.js, src/server.ts, etc.
```

**Example analysis checklist:**

```
Server Route Pattern:
- ✓ Endpoint naming: /api/panels/{type}
- ✓ Authentication middleware
- ✓ Response format: { success, data, error }

Service Pattern:
- ✓ Export structure: class vs functions
- ✓ Data transformation logic
- ✓ Error propagation

Component Pattern:
- ✓ Props interface
- ✓ State management (hooks/class)
- ✓ Data fetching approach
- ✓ Loading/error states

Registration Pattern:
- ✓ Registry file location
- ✓ Registration format (array/object)
- ✓ Required metadata fields

Server Dispatch Pattern:
- ✓ Express app.use() / router.use()  → Express routing
- ✓ switch/case on url/pathname       → Manual dispatch table
- ✓ if/else if chain on url           → Manual dispatch chain
- ✓ handler map object { '/path': fn }→ Handler map pattern
```

### 3. Plan Your Feature Files

Based on patterns, list the files you need to create **and** the existing files you need to update:

**Typical full-stack panel structure:**
1. **Server route handler** (e.g., `server/routes/system-health-route.ts`)
2. **Service layer** (e.g., `server/services/system-health-service.ts`)
3. **UI component** (e.g., `client/panels/SystemHealthPanel.ts`)
4. **Panel registration** (e.g., update `client/config/panel-registry.ts`)
5. **Styling** (e.g., `client/styles/system-health-panel.css`)
6. **Type definitions** (if TypeScript, e.g., `types/system-health.ts`)
7. **⚠️ Server wiring** (update `server/index.ts` to import and dispatch to the new handler)

### 3b. Detect Project Framework

**Before selecting templates in Step 4, determine the frontend framework in use:**

```bash
# Check for React
grep -s "react" package.json | head -5

# Check for TypeScript without React (vanilla TS)
ls client/components/*.ts 2>/dev/null | head -3
ls client/panels/*.ts 2>/dev/null | head -3

# Check for a Panel base class (common in vanilla TS dashboards)
grep -r "class.*Panel" client/ --include="*.ts" -l | head -3
grep -r "extends Panel" client/ --include="*.ts" -l | head -3
```

**Decision rule:**
- If `react` or `react-dom` is present in `package.json` → use the **React/JSX template** (Step 4C-React).
- If components are `.ts` files extending a `Panel` base class → use the **Vanilla TS template** (Step 4C-Vanilla).
- When in doubt, read one or two existing component files to confirm before proceeding.

### 3c. Detect Server Dispatch Pattern

**Read the server entry point and classify it:**

```bash
# Look for Express usage
grep -n "express\|app\.use\|router\." server/index.ts 2>/dev/null || \
grep -n "express\|app\.use\|router\." server/index.js 2>/dev/null || \
grep -n "express\|app\.use\|router\." src/server.ts 2>/dev/null

# Look for manual http.createServer dispatch
grep -n "createServer\|switch.*url\|switch.*pathname\|req\.url\|req\.pathname" server/index.ts 2>/dev/null || \
grep -n "createServer\|switch.*url\|switch.*pathname\|req\.url\|req\.pathname" server/index.js 2>/dev/null
```

**Decision rule:**
- `app.use('/api/feature', featureRouter)` style → **Express pattern** (Step 5A)
- `switch(pathname) { case '/api/feature': ... }` style → **Manual switch/case pattern** (Step 5B)
- `if (url.startsWith('/api/feature'))` style → **Manual if/else chain** (Step 5C)
- `{ '/api/feature': handlerFn }` map style → **Handler map pattern** (Step 5D)

### 4. Create Files in Order

Generate files in dependency order (backend → frontend → registration):

> ⚠️ **Unicode / multi-byte character warning**
> `write_file` may fail with `'unknown error'` (or silently produce a truncated file) when
> the content contains multi-byte Unicode characters such as emoji (e.g. document/chart icons)
> or box-drawing characters (e.g. ─ │ ╔).
> **Prevention:** prefer ASCII-safe equivalents in generated source code — e.g. `[OK]`
> instead of a checkmark emoji, `->` instead of an arrow, plain hyphens/pipes instead of
> box-drawing chars.
> **Recovery:** if a `write_file` call returns an error or the resulting file is empty or
> truncated, fall back to `run_shell` with a heredoc:
>
> ```bash
> # Recovery path -- write file content via shell heredoc (avoids write_file Unicode bug)
> cat > path/to/file.ts << 'HEREDOC'
> // file content here -- ensure no raw emoji or box-drawing chars are present
> HEREDOC
> ```
>
> Verify the file was written correctly with `wc -l path/to/file.ts` or
> `head -5 path/to/file.ts` after every write that previously failed.


#### A. Server Route Handler

**Express style:**
```javascript
// server/routes/{feature}-route.js
const express = require('express');
const router = express.Router();
const featureService = require('../services/{feature}-service');

router.get('/api/panels/{feature}', async (req, res) => {
  try {
    const data = await featureService.getData();
    res.json({ success: true, data });
  } catch (error) {
    res.status(500).json({ success: false, error: error.message });
  }
});

module.exports = router;
```

**Manual dispatch style (TypeScript):**
```typescript
// server/routes/{feature}-route.ts
import { IncomingMessage, ServerResponse } from 'http';
import { featureService } from '../services/{feature}-service';

export async function handle{Feature}Request(
  req: IncomingMessage,
  res: ServerResponse,
  action?: string
): Promise<void> {
  try {
    const data = await featureService.getData(action);
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ success: true, data }));
  } catch (err) {
    res.writeHead(500, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ success: false, error: err instanceof Error ? err.message : String(err) }));
  }
}
```

#### B. Service Layer

```typescript
// server/services/{feature}-service.ts
export interface {Feature}Data {
  // Define your data shape
}

export async function getData(action?: string): Promise<{Feature}Data> {
  // Implementation following existing service patterns
  // - Data fetching
  // - Business logic
  // - Data transformation
  return {} as {Feature}Data;
}
```

#### C-React. UI Component (React/JSX projects)

*Use this template only when React is confirmed in Step 3b.*

```javascript
// client/components/{Feature}Panel.jsx
import React, { useState, useEffect } from 'react';
import './styles/{feature}-panel.css';

const FeaturePanel = () => {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    fetch('/api/panels/{feature}')
      .then(res => res.json())
      .then(result => {
        setData(result.data);
        setLoading(false);
      })
      .catch(err => {
        setError(err.message);
        setLoading(false);
      });
  }, []);

  if (loading) return <div>Loading...</div>;
  if (error) return <div>Error: {error}</div>;

  return (
    <div className="{feature}-panel">
      {/* Render data following UI patterns */}
    </div>
  );
};

export default FeaturePanel;
```

#### C-Vanilla. UI Component (Vanilla TypeScript / Panel base-class projects)

*Use this template when components extend a Panel base class (no React).*

```typescript
// client/panels/{Feature}Panel.ts
import { Panel } from '../core/Panel';

export class FeaturePanel extends Panel {
  private intervalId: number | null = null;

  constructor(id: string) {
    super(id);
    this.setTitle('Feature Display Name');
  }

  async onLoad(): Promise<void> {
    if (this.isFetching) return;
    this.isFetching = true;
    try {
      const res = await fetch('/api/panels/{feature}', {
        headers: { Authorization: `Bearer ${localStorage.getItem('token') ?? ''}` }
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const { data } = await res.json();
      this.render(data);
    } catch (err) {
      this.showError(err instanceof Error ? err.message : String(err), () => this.onLoad());
    } finally {
      this.isFetching = false;
    }
  }

  private render(data: unknown): void {
    const container = document.createElement('div');
    container.className = '{feature}-panel';
    // Build DOM nodes from data instead of JSX:
    // const item = document.createElement('p');
    // item.textContent = String(data);
    // container.appendChild(item);
    this.setContent(container);
  }

  // Call this to start polling (optional)
  startPolling(intervalMs = 60_000): void {
    this.onLoad();
    this.intervalId = window.setInterval(() => this.onLoad(), intervalMs);
  }

  destroy(): void {
    if (this.intervalId !== null) clearInterval(this.intervalId);
    super.destroy();
  }
}
```

**Key differences from React template:**
- Extends `Panel` base class; uses `setContent()` / `showError()` / `setTitle()` / `setCount()` instead of React state.
- DOM construction via `document.createElement` / `innerHTML` instead of JSX.
- No `useState` / `useEffect`; lifecycle is `onLoad()` + `destroy()`.
- File extension is `.ts`, not `.jsx`/`.tsx`.
- Registration passes a factory function or class constructor, not a JSX component.

#### D. Panel Registration

**React projects** — import the component directly:

```javascript
// client/config/panel-registry.js (update existing)
import FeaturePanel from '../components/FeaturePanel';

export const panels = [
  // ... existing panels
  {
    id: '{feature}',
    name: 'Feature Display Name',
    component: FeaturePanel,
    icon: 'icon-name',
    category: 'appropriate-category'
  }
];
```

**Vanilla TS projects** — register via factory:

```typescript
// client/config/panel-registry.ts (update existing)
import { FeaturePanel } from '../panels/FeaturePanel';

export const panels = [
  // ... existing panels
  {
    id: '{feature}',
    name: 'Feature Display Name',
    factory: (id: string) => new FeaturePanel(id),
    icon: 'icon-name',
    category: 'appropriate-category'
  }
];
```

#### E. Styling

```css
/* client/styles/{feature}-panel.css */
.{feature}-panel {
  /* Follow existing panel styling conventions */
  padding: 1rem;
  border-radius: 4px;
}

.{feature}-panel__header {
  /* Consistent header styling */
}
```

### 5. Wire the Route Handler into the Server

**This step is frequently missed and causes features to silently return 404.** After creating the route handler, you must register it with the server's request dispatcher.

First, re-read the server entry point to confirm the exact dispatch pattern:

```bash
cat server/index.ts    # adjust path as needed
```

---

#### 5A. Express Pattern

Add a `require`/`import` and an `app.use()` call alongside the existing routes:

```typescript
// server/index.ts  — Express style
import featureRouter from './routes/{feature}-route';

// Place with other app.use() route registrations
app.use('/api/panels/{feature}', featureRouter);
```

**Checklist:**
- [ ] Import is at the top of the file with other route imports
- [ ] `app.use()` call is placed before any catch-all `404` handler
- [ ] Path prefix matches what the client fetches

---

#### 5B. Manual switch/case Pattern

This is the pattern used when the server is built with `http.createServer` and routes are dispatched via a `switch` on the URL pathname. Read the existing cases to match the exact style, then add a new `case`:

```typescript
// server/index.ts — manual switch/case dispatch
import { handle{Feature}Request } from './routes/{feature}-route';

// Inside the createServer callback, find the switch block:
const url = new URL(req.url ?? '/', `http://${req.headers.host}`);
const pathname = url.pathname;
const action = url.searchParams.get('action') ?? undefined;

switch (pathname) {
  // ... existing cases ...

  case '/api/panels/{feature}':
    await handle{Feature}Request(req, res, action);
    break;

  // ... keep the default/404 case last ...
  default:
    res.writeHead(404, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'Not found' }));
}
```

**Checklist:**
- [ ] Import is at the top of the file with other handler imports
- [ ] New `case` is placed **before** the `default` case
- [ ] `break` (or `return`) statement is present after the handler call
- [ ] The path string exactly matches the client's `fetch('/api/panels/{feature}')` call
- [ ] `action` / query-param forwarding matches the handler's signature

---

#### 5C. Manual if/else Chain Pattern

If the server uses an `if/else if` chain instead of `switch`:

```typescript
// server/index.ts — if/else chain
import { handle{Feature}Request } from './routes/{feature}-route';

// Inside the createServer callback:
if (pathname === '/api/panels/existing-a') {
  await handleExistingA(req, res);
} else if (pathname === '/api/panels/existing-b') {
  await handleExistingB(req, res);
// ↓ Add your new branch before the final else/404 block
} else if (pathname === '/api/panels/{feature}') {
  await handle{Feature}Request(req, res, action);
} else {
  res.writeHead(404, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({ error: 'Not found' }));
}
```

**Checklist:**
- [ ] New `else if` branch is before the final `else` (404) block
- [ ] Import is at the top of the file

---

#### 5D. Handler Map Pattern

If the server uses a map object to look up handlers:

```typescript
// server/index.ts — handler map
import { handle{Feature}Request } from './routes/{feature}-route';

const handlers: Record<string, RequestHandler> = {
  '/api/panels/existing-a': handleExistingA,
  '/api/panels/existing-b': handleExistingB,
  // ↓ Add your new entry
  '/api/panels/{feature}': handle{Feature}Request,
};
```

**Checklist:**
- [ ] New entry key matches client fetch URL exactly
- [ ] Import is at the top of the file

---

#### 5E. Verify Wiring

After updating the server entry point, confirm the wiring is complete:

```bash
# Confirm the import exists
grep -n "handle{Feature}Request\|{feature}-route" server/index.ts

# Confirm the dispatch entry exists
grep -n "{feature}" server/index.ts

# Confirm no syntax errors (TypeScript projects)
npx tsc --noEmit 2>&1 | head -20

# Quick smoke test: start the server and curl the endpoint
# curl http://localhost:PORT/api/panels/{feature}
```

### 6. Verify Pattern Consistency

After creating all files and wiring the server, check:

- [ ] Naming follows project conventions across all layers
- [ ] Import/export statements are consistent
- [ ] Error handling matches existing patterns
- [ ] API response format is uniform
- [ ] Component structure mirrors other panels
- [ ] Registration metadata is complete
- [ ] CSS class naming follows BEM or project convention
- [ ] Server entry point updated (new route is reachable)

### 7. Test Integration Points

Verify that all pieces connect:

```bash
# Server route handler file exists
ls server/routes/{feature}-route.*

# Server entry point imports and dispatches the new handler
grep -n "{feature}" server/index.ts   # or server/index.js

# Component is imported in registry
grep -r "import.*{Feature}Panel" client/config/

# Styles are imported
grep -r "import.*{feature}-panel.css" client/
```

## Key Principles

1. **Pattern before implementation**: Always read existing code first — especially the server entry point
2. **Maintain consistency**: Match naming, structure, and style exactly
3. **Complete the stack**: Don't leave layers incomplete — the server wiring step is as important as creating the handler file
4. **Follow the chain**: Backend service → Route handler → **Server wiring** → Frontend component → Registration → Styling
5. **Verify integration**: Ensure all pieces connect properly; a missing `case` in a switch block causes silent 404s

## Common Pitfalls

- ❌ Creating a route handler file but forgetting to add it to the server's dispatch table → **silent 404 on every request**
- ❌ Creating component before verifying API endpoint works
- ❌ Inconsistent naming across layers (camelCase vs kebab-case)
- ❌ Forgetting to register the panel in the frontend config
- ❌ Missing error handling in any layer
- ❌ Skipping styling, leading to broken UI
- ❌ Placing a new `case` after the `default` case in a switch (unreachable code)
- ❌ Path string mismatch between server case and client fetch URL

## Variations

**For simple features**: May omit service layer if route logic is trivial

**For complex features**: May need additional files:
- Database migrations/models
- Redux actions/reducers (if using Redux)
- Test files for each layer
- Documentation files

**For TypeScript projects**: Add `.d.ts` or `.ts` type definition files

**For Express projects**: Step 5A covers the wiring; it is usually straightforward (`app.use()`).

**For manual http.createServer projects**: Steps 5B–5D are critical. Always read the server entry point before assuming the pattern — projects vary between switch/case, if/else chains, and handler maps.
