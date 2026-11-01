---
name: full-stack-panel-authoring
description: Single cohesive workflow for creating complete dashboard panel features in vanilla TypeScript — covers project-structure exploration, server dispatch pattern detection and wiring (Express or manual http.createServer), Panel base class component authoring with CircuitBreaker resilience and localStorage persistence, panel registration in index.ts, and safe file writing (including emoji workarounds).
---

# Unified Full-Stack Panel Authoring

One guide for the complete workflow: backend route → service → server wiring → Panel component with resilience → registration. Eliminates the need to consult separate skills for what is always a single cohesive task.

---

## When to Use

- Adding a new panel type to a vanilla-TypeScript dashboard
- Creating a feature that spans all layers: server route, service, component, registration
- Ensuring pattern-consistency across the stack
- Projects using a manual `http.createServer` dispatch loop **or** Express

---

## Quick Reference — File Checklist

| # | File | Action |
|---|------|--------|
| 1 | `server/routes/{feature}-route.ts` | Create |
| 2 | `src/services/{feature}.ts` | Create |
| 3 | `src/components/{Feature}Panel.ts` | Create |
| 4 | `src/components/index.ts` | Update — add export |
| 5 | `server/index.ts` | Update — import + dispatch case |
| 6 | `src/utils/circuit-breaker.ts` | Create if absent |
| 7 | `src/utils/sparkline.ts` | Create if absent |

---

## Step 1 — Explore Project Structure

Run these commands before writing a single line of code. Reading existing patterns prevents style drift and reveals the exact dispatch mechanism.

```bash
# Locate server entry point (critical for wiring)
find . -name "index.ts" -o -name "index.js" -o -name "server.ts" -o -name "server.js" \
  | grep -v node_modules | head -10

# Find existing route handlers to use as pattern files
find server/routes -name "*.ts" -o -name "*.js" 2>/dev/null | head -10

# Find existing service files
find src/services -name "*.ts" -o -name "*.js" 2>/dev/null | head -10

# Find existing Panel components
find src/components -name "*Panel.ts" 2>/dev/null | head -10

# Find the Panel base class
find src -name "Panel.ts" 2>/dev/null | head -5

# Find component registration index
find src/components -name "index.ts" 2>/dev/null | head -3

# Check for CircuitBreaker utility
find src/utils -name "circuit-breaker.ts" 2>/dev/null

# Check framework (should NOT find React in vanilla TS projects)
grep -s '"react"' package.json | head -3
```

**Read 1-2 examples from each layer before generating any code:**

```bash
cat server/routes/<existing>-route.ts
cat src/services/<existing>.ts
cat src/components/<existing>Panel.ts
cat src/components/index.ts
cat server/index.ts          # most important: reveals dispatch pattern
```

---

## Step 2 — Detect Server Dispatch Pattern

Read `server/index.ts` (or `server/index.js`, `src/server.ts`) and classify:

```bash
# Express signals
grep -n "express\|app\.use\|router\." server/index.ts 2>/dev/null

# Manual http.createServer signals
grep -n "createServer\|switch.*url\|switch.*pathname\|req\.url\|case '/" server/index.ts 2>/dev/null
```

| Pattern in file | Wiring method (Step 5) |
|----------------|------------------------|
| `app.use('/api/x', xRouter)` | **5A - Express** |
| `switch(pathname) { case '/api/x':` | **5B - Manual switch/case** |
| `if (url.startsWith('/api/x'))` | **5C - Manual if/else chain** |
| `const handlers = { '/api/x': fn }` | **5D - Handler map** |

NOTE: Misidentifying the dispatch pattern is the most common cause of silent 404s.
If unsure, read the full server entry point before proceeding.

---

## Step 3 — Create the Server Route Handler

Use the **manual dispatch** template for `http.createServer` projects (most common in this codebase). Use the Express template only when Express is confirmed.

### Manual dispatch (TypeScript) — preferred template

```typescript
// server/routes/{feature}-route.ts
import { IncomingMessage, ServerResponse } from 'http';
import { fetch{Feature}Data } from '../../src/services/{feature}';

export async function handle{Feature}Request(
  req: IncomingMessage,
  res: ServerResponse,
  action?: string
): Promise<void> {
  try {
    const data = await fetch{Feature}Data(action);
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ success: true, data }));
  } catch (err) {
    res.writeHead(500, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      success: false,
      error: err instanceof Error ? err.message : String(err)
    }));
  }
}
```

### Express template (only when confirmed)

```typescript
// server/routes/{feature}-route.ts
import { Router } from 'express';
import { fetch{Feature}Data } from '../../src/services/{feature}';

const router = Router();

router.get('/', async (req, res) => {
  try {
    const data = await fetch{Feature}Data(req.query.action as string | undefined);
    res.json({ success: true, data });
  } catch (err) {
    res.status(500).json({ success: false, error: (err as Error).message });
  }
});

export default router;
```

---

## Step 4 — Create the Service Layer

The service owns data fetching and transformation. The CircuitBreaker (Step 6) wraps the service call at the **component** level — keep the service itself simple.

```typescript
// src/services/{feature}.ts

export interface {Feature}Item {
  id: string;
  // define your data shape matching the API response
}

/**
 * Fetch {feature} data from the server API.
 * @param action  Optional sub-action / filter parameter.
 */
export async function fetch{Feature}Data(action?: string): Promise<{Feature}Item[]> {
  const url = `/api/panels/{feature}${action ? `?action=${encodeURIComponent(action)}` : ''}`;
  const res = await fetch(url, {
    headers: { Authorization: `Bearer ${localStorage.getItem('token') ?? ''}` }
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const { data } = await res.json() as { data: {Feature}Item[] };
  return data;
}
```

---

## Step 5 — Wire the Route Handler into the Server

**This step is the most frequently missed and causes features to silently return 404.**

After creating the route file, open `server/index.ts` and add **both** an import and a dispatch entry.

### 5A - Express

```typescript
// server/index.ts  -- excerpt
import {feature}Router from './routes/{feature}-route';

// Add alongside other app.use() calls:
app.use('/api/panels/{feature}', {feature}Router);
```

### 5B - Manual switch/case (most common pattern)

```typescript
// server/index.ts  -- excerpt showing the additions

// 1. Add import near the top with other route imports
import { handle{Feature}Request } from './routes/{feature}-route';

// 2. Inside the switch / if-else dispatch block, add a new case
// Find the block that looks like:
//   switch (pathname) {
//     case '/api/panels/health': ...
//     case '/api/panels/email':  ...
// Add:
    case '/api/panels/{feature}':
      await handle{Feature}Request(req, res, action);
      break;
```

### 5C - Manual if/else chain

```typescript
// server/index.ts  -- excerpt
import { handle{Feature}Request } from './routes/{feature}-route';

// Add a new else-if branch:
} else if (url.startsWith('/api/panels/{feature}')) {
  await handle{Feature}Request(req, res, action);
}
```

### 5D - Handler map

```typescript
// server/index.ts  -- excerpt
import { handle{Feature}Request } from './routes/{feature}-route';

const handlers: Record<string, HandlerFn> = {
  // ... existing entries ...
  '/api/panels/{feature}': handle{Feature}Request,
};
```

**Verification:** After wiring, start the server and `curl /api/panels/{feature}` — you should receive JSON, not a 404.

---

## Step 6 — Create the Panel Component

### Architecture overview

```
Panel (base class)
+-- element: HTMLElement (.panel)
    +-- header (.panel-header)
    |   +-- headerLeft (.panel-header-left)
    |   |   +-- title (.panel-title)
    |   +-- countEl (.panel-count)  [optional]
    +-- content (.panel-content)
```

### Resilience layers

| Layer | When to use |
|---|---|
| `isFetching` guard | Always -- prevents duplicate concurrent requests |
| `fetchWithRetry()` | When you own the raw fetch and want per-request retry |
| `CircuitBreaker.execute()` | When a service may fail repeatedly; serves stale cache during cooldown |
| `saveState()` / `loadState()` | When collapsed/size state should survive page reloads |

### Complete Panel component template

```typescript
// src/components/{Feature}Panel.ts
import { Panel } from './Panel';
import { createCircuitBreaker } from '../utils/circuit-breaker';
import { fetch{Feature}Data, {Feature}Item } from '../services/{feature}';

// Module-level breaker -- persists cache and failure state between re-renders
const breaker = createCircuitBreaker<{Feature}Item[]>({
  name: '{Feature}',
  cacheTtlMs: 60_000,      // serve cache for 1 min
  cooldownMs: 5 * 60_000,  // open circuit for 5 min after maxFailures
  maxFailures: 2,
});

export class {Feature}Panel extends Panel {
  private refreshTimer: ReturnType<typeof setInterval> | null = null;

  constructor() {
    super({ id: '{feature}', title: '{Feature Display Name}', showCount: true });
    this.loadState();                                  // restore collapsed/size
    this.fetchData();
    this.refreshTimer = setInterval(() => this.fetchData(), 60_000);
  }

  private async fetchData(): Promise<void> {
    if (this.isFetching) return;
    this.setFetching(true);
    try {
      // One-liner: wraps existing service call with caching + circuit-break
      const items = await breaker.execute(() => fetch{Feature}Data(), []);
      this.setCount(items.length);
      this.render(items);
    } catch (err) {
      this.showError(
        err instanceof Error ? err.message : 'Failed to load',
        () => {
          this.retryAttempts = 0;
          this.retryDelay = 1000;
          this.fetchData();
        }
      );
    } finally {
      this.setFetching(false);
    }
  }

  private render(items: {Feature}Item[]): void {
    if (items.length === 0) {
      this.setContent('<div class="panel-empty">No items found.</div>');
      return;
    }

    const container = document.createElement('div');
    container.className = '{feature}-panel';

    for (const item of items) {
      const row = document.createElement('div');
      row.className = '{feature}-panel__row';
      // Build DOM from item fields -- no JSX, no innerHTML with untrusted data:
      const label = document.createElement('span');
      label.className = '{feature}-panel__label';
      label.textContent = item.id;          // replace with real fields
      row.appendChild(label);
      container.appendChild(row);
    }

    this.content.innerHTML = '';
    this.content.appendChild(container);
    this.saveState();
  }

  destroy(): void {
    if (this.refreshTimer !== null) clearInterval(this.refreshTimer);
    super.destroy();
  }
}
```

**Key rules for vanilla TS panels (no React):**
- Extend `Panel` base class; use `setContent()` / `showError()` / `setCount()` / `showLoading()` instead of React state.
- Build DOM via `document.createElement` -- not JSX, not `innerHTML` with untrusted strings.
- Lifecycle: `constructor` -> `fetchData()` -> `render()` -> `destroy()`. No `useEffect`.
- File extension is `.ts`, not `.jsx`/`.tsx`.

---

## Step 7 — Register the Component in index.ts

Export the new panel from the components barrel file so the dashboard can discover it.

```typescript
// src/components/index.ts  -- add alongside existing exports
export { {Feature}Panel } from './{Feature}Panel';
```

Then instantiate and mount in the dashboard bootstrap (follow the existing pattern):

```typescript
// src/index.ts or src/dashboard.ts  -- excerpt
import { {Feature}Panel } from './components';

const {feature}Panel = new {Feature}Panel();
document.getElementById('{feature}-slot')?.appendChild({feature}Panel.getElement());
```

---

## Step 8 — Add Utility Files (if absent)

Only create these if `find src/utils -name "circuit-breaker.ts"` returns nothing.

### `src/utils/circuit-breaker.ts`

```typescript
interface CircuitState { failures: number; cooldownUntil: number; }
interface CacheEntry<T> { data: T; timestamp: number; }

export interface CircuitBreakerOptions {
  name: string;
  maxFailures?: number;  // default 2
  cooldownMs?: number;   // default 5 min
  cacheTtlMs?: number;   // default 10 min
}

export class CircuitBreaker<T> {
  private state: CircuitState = { failures: 0, cooldownUntil: 0 };
  private cache: CacheEntry<T> | null = null;
  private readonly name: string;
  private readonly maxFailures: number;
  private readonly cooldownMs: number;
  private readonly cacheTtlMs: number;

  constructor(options: CircuitBreakerOptions) {
    this.name        = options.name;
    this.maxFailures = options.maxFailures ?? 2;
    this.cooldownMs  = options.cooldownMs  ?? 5 * 60 * 1000;
    this.cacheTtlMs  = options.cacheTtlMs  ?? 10 * 60 * 1000;
  }

  isOnCooldown(): boolean {
    if (Date.now() < this.state.cooldownUntil) return true;
    if (this.state.cooldownUntil > 0) this.state = { failures: 0, cooldownUntil: 0 };
    return false;
  }

  getCached(): T | null {
    if (this.cache && Date.now() - this.cache.timestamp < this.cacheTtlMs) return this.cache.data;
    return null;
  }

  recordSuccess(data: T): void {
    this.state = { failures: 0, cooldownUntil: 0 };
    this.cache = { data, timestamp: Date.now() };
  }

  recordFailure(): void {
    this.state.failures++;
    if (this.state.failures >= this.maxFailures) {
      this.state.cooldownUntil = Date.now() + this.cooldownMs;
      console.warn(`[${this.name}] Circuit open -- cooldown ${this.cooldownMs / 1000}s`);
    }
  }

  async execute<R extends T>(fn: () => Promise<R>, defaultValue: R): Promise<R> {
    if (this.isOnCooldown()) return (this.getCached() as R) ?? defaultValue;
    const cached = this.getCached();
    if (cached !== null) return cached as R;
    try {
      const result = await fn();
      this.recordSuccess(result);
      return result;
    } catch (e) {
      console.error(`[${this.name}] Request failed:`, e);
      this.recordFailure();
      return defaultValue;
    }
  }
}

export function createCircuitBreaker<T>(options: CircuitBreakerOptions): CircuitBreaker<T> {
  return new CircuitBreaker<T>(options);
}
```

### `src/utils/sparkline.ts` (optional — for numeric trend data)

```typescript
/**
 * Returns an inline SVG sparkline string, or '' if data is too short.
 * Embed directly in HTML template strings.
 */
export function miniSparkline(
  data: number[] | undefined,
  change: number | null,
  w = 50,
  h = 16
): string {
  if (!data || data.length < 2) return '';
  const min   = Math.min(...data);
  const max   = Math.max(...data);
  const range = max - min || 1;
  const color = change != null && change >= 0 ? 'var(--green)' : 'var(--red)';
  const points = data.map((v, i) => {
    const x = (i / (data.length - 1)) * w;
    const y = h - ((v - min) / range) * (h - 2) - 1;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">` +
    `<polyline points="${points}" fill="none" stroke="${color}" ` +
    `stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
}
```

---

## Step 9 — Panel Base Class (if absent)

Only create `src/components/Panel.ts` if no existing base class is found.

```typescript
// src/components/Panel.ts
export interface PanelOptions {
  id: string;
  title: string;
  showCount?: boolean;
  className?: string;
}

export class Panel {
  protected element: HTMLElement;
  protected content: HTMLElement;
  protected header: HTMLElement;
  protected countEl: HTMLElement | null = null;
  protected panelId: string;
  private _fetching = false;

  // Retry state (used by fetchWithRetry)
  protected retryAttempts = 0;
  protected retryDelay = 1000;
  private maxRetries = 3;

  constructor(options: PanelOptions) {
    this.panelId = options.id;
    this.element = document.createElement('div');
    this.element.className = `panel ${options.className ?? ''}`;
    this.element.dataset.panel = options.id;

    this.header = document.createElement('div');
    this.header.className = 'panel-header';

    const headerLeft = document.createElement('div');
    headerLeft.className = 'panel-header-left';
    const title = document.createElement('span');
    title.className = 'panel-title';
    title.textContent = options.title;
    headerLeft.appendChild(title);
    this.header.appendChild(headerLeft);

    if (options.showCount) {
      this.countEl = document.createElement('span');
      this.countEl.className = 'panel-count';
      this.countEl.textContent = '0';
      this.header.appendChild(this.countEl);
    }

    this.content = document.createElement('div');
    this.content.className = 'panel-content';
    this.content.id = `${options.id}Content`;

    this.element.appendChild(this.header);
    this.element.appendChild(this.content);
    this.showLoading();
  }

  public getElement(): HTMLElement { return this.element; }

  public showLoading(message = 'Loading...'): void {
    this.content.innerHTML = `
      <div class="panel-loading">
        <div class="panel-loading-spinner"></div>
        <div class="panel-loading-text">${message}</div>
      </div>`;
  }

  public showError(message = 'Failed to load', onRetry?: () => void): void {
    this.content.innerHTML = `
      <div class="panel-error-state">
        <div class="panel-error-msg">${message}</div>
        ${onRetry ? '<button class="panel-retry-btn" data-panel-retry>Retry</button>' : ''}
      </div>`;
    if (onRetry) {
      this.content.querySelector('[data-panel-retry]')?.addEventListener('click', onRetry);
    }
  }

  public setContent(html: string): void { this.content.innerHTML = html; }

  public setCount(count: number): void {
    if (this.countEl) this.countEl.textContent = count.toString();
  }

  public show(): void { this.element.classList.remove('hidden'); }
  public hide(): void { this.element.classList.add('hidden'); }
  public destroy(): void { this.element.remove(); }

  protected setFetching(v: boolean): void { this._fetching = v; }
  protected get isFetching(): boolean { return this._fetching; }

  protected async fetchWithRetry(url: string): Promise<unknown> {
    while (this.retryAttempts < this.maxRetries) {
      try {
        const response = await fetch(url);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return await response.json();
      } catch (error) {
        this.retryAttempts++;
        if (this.retryAttempts >= this.maxRetries) {
          throw new Error(`Failed after ${this.maxRetries} attempts: ${(error as Error).message}`);
        }
        await new Promise(resolve => setTimeout(resolve, this.retryDelay));
        this.retryDelay *= 2;
      }
    }
  }

  public saveState(): void {
    localStorage.setItem(`panelState_${this.panelId}`, JSON.stringify({
      isExpanded: !this.element.classList.contains('collapsed'),
      width: this.element.style.width,
      height: this.element.style.height,
    }));
  }

  public loadState(): void {
    const raw = localStorage.getItem(`panelState_${this.panelId}`);
    if (!raw) return;
    const { isExpanded, width, height } = JSON.parse(raw) as {
      isExpanded: boolean; width: string; height: string;
    };
    if (!isExpanded) this.element.classList.add('collapsed');
    if (width)  this.element.style.width  = width;
    if (height) this.element.style.height = height;
  }
}
```

---

## Step 10 — Safe File Writing

**Known tool issue:** `write_file` (and some shell tools) fail silently or with "unknown error" when file content contains multi-byte Unicode characters such as emoji (e.g., document, chart, checkmark emoji).

### Rules for safe content

1. **Never use emoji in generated source code.** Replace with plain-text equivalents:
   - Document emoji -> `[doc]` or `(file)`
   - Chart emoji -> `[chart]`
   - Checkmark emoji -> `[ok]` or `(done)`
   - Warning emoji -> `[warn]` or `WARNING:`

2. **Use ASCII substitutes in comments** instead of Unicode box-drawing characters.

3. **Test-write suspect content** to a throwaway file first if unsure:
   ```bash
   echo "test content" > /tmp/write_test.txt && cat /tmp/write_test.txt
   ```

4. **Recovery pattern** -- if a write_file call fails:
   - Strip all non-ASCII characters from the content.
   - Retry the write.
   - If still failing, split the file into smaller chunks and concatenate with `cat`.

---

## Validation Checklist

Before considering the feature complete, verify each layer:

```
Server route
  [ ] File created at server/routes/{feature}-route.ts
  [ ] Exports handle{Feature}Request function
  [ ] Returns { success: true, data } on success
  [ ] Returns { success: false, error } on failure with correct HTTP status

Service
  [ ] File created at src/services/{feature}.ts
  [ ] Exports fetch{Feature}Data function
  [ ] Throws on non-ok HTTP status (so CircuitBreaker can record failures)

Server wiring
  [ ] Import added to server/index.ts
  [ ] Dispatch case/route added (matching the detected pattern)
  [ ] curl /api/panels/{feature} returns JSON (not 404)

Panel component
  [ ] Extends Panel base class
  [ ] Uses isFetching guard
  [ ] CircuitBreaker wraps the service call
  [ ] showError() called with retry callback on failure
  [ ] destroy() clears interval and calls super.destroy()

Registration
  [ ] Export added to src/components/index.ts
  [ ] Panel instantiated and mounted in dashboard bootstrap

File safety
  [ ] No emoji in any generated source file
  [ ] All write_file calls succeeded (check file size > 0)
```

---

## Common Mistakes

| Mistake | Symptom | Fix |
|---------|---------|-----|
| Skipping Step 5 (server wiring) | Feature returns 404 | Add import + dispatch entry to server/index.ts |
| Wrong dispatch pattern | 404 or runtime error | Re-read server/index.ts; use grep from Step 2 |
| Emoji in source files | write_file silent failure | Replace all emoji with ASCII equivalents |
| React template in vanilla TS project | Compile errors | Confirm no React in package.json; use Panel base class template |
| Creating Panel.ts when one exists | Overwrites base class | Check with `find src -name "Panel.ts"` first |
| Not calling `super.destroy()` | Memory leak | Always chain super.destroy() in subclass destroy() |
| CircuitBreaker declared inside class | Cache lost between fetches | Declare `const breaker = ...` at module level outside the class |
