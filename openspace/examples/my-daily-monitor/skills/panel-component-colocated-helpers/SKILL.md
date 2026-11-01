---
name: panel-component-colocated-helpers
description: Enhanced dashboard panel component with robust error handling, retry logic, localStorage persistence, and co-located inline helper functions (e.g. timeAgo, statusIcon) for self-contained, import-light panel implementations.
---

# Panel Component Pattern

Create dashboard panel components using **vanilla TypeScript** (no framework, no JSX). Each panel is a class extending a `Panel` base class.

## Architecture Overview

### Enhanced Features
- **Retry Logic**: Automatically retries failed data fetches with exponential backoff.
- **State Persistence**: Saves panel state (expanded/collapsed, size) to localStorage.
- **Detailed Error Handling**: Provides more granular error messages and recovery options.
- **Co-located Helpers**: Panel-specific pure utility functions live in the same file as the panel class, keeping implementations self-contained.

```
Panel (base class)
├── element: HTMLElement (outer container, .panel)
│   ├── header: HTMLElement (.panel-header)
│   │   ├── headerLeft (.panel-header-left)
│   │   │   ├── title (.panel-title)
│   │   │   └── newBadge (.panel-new-badge) [optional]
│   │   ├── statusBadge (.panel-data-badge) [optional]
│   │   └── countEl (.panel-count) [optional]
│   ├── content: HTMLElement (.panel-content)
│   └── resizeHandle (.panel-resize-handle)
```

## Base Panel Class (Simplified for this project)

```typescript
export class Panel {
  // ... existing code ...

  private retryAttempts = 0;
  private maxRetries = 3;
  private retryDelay = 1000; // starts at 1s, doubles each retry

  protected async fetchWithRetry(url: string): Promise<any> {
    while (this.retryAttempts < this.maxRetries) {
      try {
        const response = await fetch(url);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return await response.json();
      } catch (error) {
        this.retryAttempts++;
        if (this.retryAttempts >= this.maxRetries) {
          throw new Error(`Failed after ${this.maxRetries} attempts: ${error.message}`);
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
      height: this.element.style.height
    }));
  }

  public loadState(): void {
    const savedState = localStorage.getItem(`panelState_${this.panelId}`);
    if (savedState) {
      const { isExpanded, width, height } = JSON.parse(savedState);
      if (!isExpanded) this.element.classList.add('collapsed');
      if (width) this.element.style.width = width;
      if (height) this.element.style.height = height;
    }
  }
}
```

Create `src/components/Panel.ts`:

```typescript
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

  constructor(options: PanelOptions) {
    this.panelId = options.id;
    this.element = document.createElement('div');
    this.element.className = `panel ${options.className || ''}`;
    this.element.dataset.panel = options.id;

    // Header
    this.header = document.createElement('div');
    this.header.className = 'panel-header';

    const headerLeft = document.createElement('div');
    headerLeft.className = 'panel-header-left';

    const title = document.createElement('span');
    title.className = 'panel-title';
    title.textContent = options.title;
    headerLeft.appendChild(title);
    this.header.appendChild(headerLeft);

    // Count badge (optional)
    if (options.showCount) {
      this.countEl = document.createElement('span');
      this.countEl.className = 'panel-count';
      this.countEl.textContent = '0';
      this.header.appendChild(this.countEl);
    }

    // Content area
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

  public setContent(html: string): void {
    this.content.innerHTML = html;
  }

  public setCount(count: number): void {
    if (this.countEl) this.countEl.textContent = count.toString();
  }

  public show(): void { this.element.classList.remove('hidden'); }
  public hide(): void { this.element.classList.add('hidden'); }

  protected setFetching(v: boolean): void { this._fetching = v; }
  protected get isFetching(): boolean { return this._fetching; }

  public destroy(): void {
    this.element.remove();
  }
}
```

## Creating a Concrete Panel (Example: StockPanel)

```typescript
export class StockPanel extends Panel {
  // ... existing code ...

  private async fetchData(): Promise<void> {
    if (this.isFetching) return;
    this.setFetching(true);
    try {
      const quotes = await this.fetchWithRetry('/api/stocks');
      this.render(quotes);
      this.setCount(quotes.length);
      this.saveState();
    } catch (err) {
      this.showError(`Failed to load stock data: ${err.message}`, () => {
        this.retryAttempts = 0;
        this.retryDelay = 1000;
        this.fetchData();
      });
    } finally {
      this.setFetching(false);
    }
  }
}
```

Each panel extends `Panel` and manages its own data fetching + rendering:

```typescript
import { Panel } from './Panel';

interface StockQuote {
  symbol: string;
  name: string;
  price: number | null;
  change: number | null;
  sparkline?: number[];
}

export class StockPanel extends Panel {
  private refreshTimer: ReturnType<typeof setInterval> | null = null;

  constructor() {
    super({ id: 'stocks', title: 'Stock Market', showCount: true });
    this.fetchData();
    this.refreshTimer = setInterval(() => this.fetchData(), 60_000);
  }

  private async fetchData(): Promise<void> {
    if (this.isFetching) return;
    this.setFetching(true);
    try {
      const quotes = await fetchStockQuotes(); // from data-service
      this.render(quotes);
      this.setCount(quotes.length);
    } catch (err) {
      this.showError('Failed to load stock data', () => this.fetchData());
    } finally {
      this.setFetching(false);
    }
  }

  private render(quotes: StockQuote[]): void {
    const rows = quotes.map(q => `
      <div class="stock-row">
        <span class="stock-symbol">${q.symbol}</span>
        <span class="stock-name">${q.name}</span>
        <span class="stock-price">${q.price != null ? '$' + q.price.toFixed(2) : '—'}</span>
        <span class="stock-change ${(q.change ?? 0) >= 0 ? 'positive' : 'negative'}">
          ${q.change != null ? (q.change >= 0 ? '+' : '') + q.change.toFixed(2) + '%' : '—'}
        </span>
      </div>
    `).join('');

    this.setContent(`<div class="stock-list">${rows}</div>`);
  }

  public override destroy(): void {
    if (this.refreshTimer) clearInterval(this.refreshTimer);
    super.destroy();
  }
}
```

## Key Patterns

1. **Constructor** calls `super()` with panel config, then triggers initial data fetch
2. **fetchData()** is async, uses `isFetching` guard, shows error on failure with retry
3. **render()** builds HTML strings and calls `this.setContent(html)`
4. **destroy()** cleans up timers and event listeners
5. Use `showLoading()` during initial load (auto-called in constructor)
6. Use `showError(msg, retryFn)` on failure
7. Sparkline SVGs use inline `<svg>` with `<polyline>` — see sparkline utility
8. **Co-located helpers**: Define panel-specific pure utility functions (e.g. `timeAgo`, `statusIcon`) as module-level functions in the same `.ts` file as the panel class. Do **not** place them in `src/utils` unless they are reused by three or more panels.

## Co-located Helper Pattern

Panel-specific formatting and mapping utilities live directly above the class
definition in the same file. This keeps the panel self-contained, avoids
polluting `src/utils`, and makes the file easier to read and test in isolation.

### When to co-locate

| Situation | Decision |
|-----------|----------|
| Helper used only in this panel | ✅ Co-locate in panel file |
| Helper used in 2 panels | ✅ Co-locate in the more "owning" panel; import from there |
| Helper used in 3+ panels | ❌ Move to `src/utils/<helper>.ts` |
| Helper depends on DOM or Panel API | ✅ Always co-locate (private method or module function) |

### timeAgo — relative timestamp helper

```typescript
// co-located at the top of, e.g., src/components/CodeStatusPanel.ts

/** Returns a human-readable relative time string, e.g. "3 minutes ago". */
function timeAgo(dateStr: string): string {
  const diff = Date.now() - new Date(dateStr).getTime();
  const minutes = Math.floor(diff / 60_000);
  if (minutes < 1)  return 'just now';
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? '' : 's'} ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24)   return `${hours} hour${hours === 1 ? '' : 's'} ago`;
  const days = Math.floor(hours / 24);
  return `${days} day${days === 1 ? '' : 's'} ago`;
}
```

Usage inside `render()`:

```typescript
private render(runs: WorkflowRun[]): void {
  const rows = runs.map(r => `
    <div class="run-row">
      <span class="run-name">${r.name}</span>
      <span class="run-time">${timeAgo(r.updatedAt)}</span>
    </div>
  `).join('');
  this.setContent(`<div class="run-list">${rows}</div>`);
}
```

### statusIcon — enum-to-icon mapping helper

Maps a string enum (e.g. CI status) to an icon character or emoji. Define
the map as a `const` outside the function so it is allocated once.

```typescript
// co-located at the top of, e.g., src/components/CodeStatusPanel.ts

const STATUS_ICON: Record<string, string> = {
  success:     '✅',
  failure:     '❌',
  cancelled:   '⛔',
  skipped:     '⏭️',
  in_progress: '🔄',
  queued:      '⏳',
};

/** Returns an icon string for a workflow run conclusion/status value. */
function statusIcon(status: string | null): string {
  if (!status) return '❓';
  return STATUS_ICON[status] ?? '❓';
}
```

Usage inside `render()`:

```typescript
const rows = runs.map(r => `
  <div class="run-row ${r.conclusion ?? r.status}">
    <span class="run-status">${statusIcon(r.conclusion ?? r.status)}</span>
    <span class="run-name">${r.name}</span>
    <span class="run-time">${timeAgo(r.updatedAt)}</span>
  </div>
`).join('');
```

### Full example: CodeStatusPanel with co-located helpers

```typescript
import { Panel } from './Panel';
import { fetchWorkflowRuns, WorkflowRun } from '../services/code-status';

// ── Co-located helpers ────────────────────────────────────────────────────────

const STATUS_ICON: Record<string, string> = {
  success:     '✅',
  failure:     '❌',
  cancelled:   '⛔',
  skipped:     '⏭️',
  in_progress: '🔄',
  queued:      '⏳',
};

function statusIcon(status: string | null): string {
  if (!status) return '❓';
  return STATUS_ICON[status] ?? '❓';
}

function timeAgo(dateStr: string): string {
  const diff = Date.now() - new Date(dateStr).getTime();
  const minutes = Math.floor(diff / 60_000);
  if (minutes < 1)  return 'just now';
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? '' : 's'} ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24)   return `${hours} hour${hours === 1 ? '' : 's'} ago`;
  const days = Math.floor(hours / 24);
  return `${days} day${days === 1 ? '' : 's'} ago`;
}

// ── Panel class ───────────────────────────────────────────────────────────────

export class CodeStatusPanel extends Panel {
  private refreshTimer: ReturnType<typeof setInterval> | null = null;

  constructor() {
    super({ id: 'codeStatus', title: 'CI / CD Status', showCount: true });
    this.fetchData();
    this.refreshTimer = setInterval(() => this.fetchData(), 60_000);
  }

  private async fetchData(): Promise<void> {
    if (this.isFetching) return;
    this.setFetching(true);
    try {
      const runs = await fetchWorkflowRuns();
      this.render(runs);
      this.setCount(runs.length);
      this.saveState();
    } catch (err) {
      this.showError(`Failed to load CI status: ${(err as Error).message}`, () => this.fetchData());
    } finally {
      this.setFetching(false);
    }
  }

  private render(runs: WorkflowRun[]): void {
    if (!runs.length) {
      this.setContent('<p class="panel-empty">No recent runs.</p>');
      return;
    }
    const rows = runs.map(r => `
      <div class="run-row ${r.conclusion ?? r.status}">
        <span class="run-icon">${statusIcon(r.conclusion ?? r.status)}</span>
        <span class="run-name">${r.name}</span>
        <span class="run-branch">${r.headBranch}</span>
        <span class="run-time">${timeAgo(r.updatedAt)}</span>
      </div>
    `).join('');
    this.setContent(`<div class="run-list">${rows}</div>`);
  }

  public override destroy(): void {
    if (this.refreshTimer) clearInterval(this.refreshTimer);
    super.destroy();
  }
}
```

### Naming conventions for co-located helpers

- Use **camelCase** function names (`timeAgo`, `statusIcon`, `formatBytes`).
- Keep them **pure** (no side effects, no DOM access) so they are trivially unit-testable.
- Place them **above** the class definition, below imports.
- Prefix module-level constants that back a helper with the helper name in SCREAMING_SNAKE (`STATUS_ICON` for `statusIcon`).
- Do **not** export them unless another file already needs them — keep the API surface minimal.

## Sparkline Utility

```typescript
export function miniSparkline(data: number[] | undefined, change: number | null, w = 50, h = 16): string {
  if (!data || data.length < 2) return '';
  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;
  const color = change != null && change >= 0 ? 'var(--green)' : 'var(--red)';
  const points = data.map((v, i) => {
    const x = (i / (data.length - 1)) * w;
    const y = h - ((v - min) / range) * (h - 2) - 1;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><polyline points="${points}" fill="none" stroke="${color}" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
}
```
