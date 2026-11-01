---
name: panel-base-advanced
description: Unified guide for authoring dashboard panel components in vanilla TypeScript — covers the authoritative Panel base class (grid-span persistence, dual resize handles, radar loading animation, exponential-backoff error countdown, data/new badge helpers, full destroy() cleanup), localStorage state via loadMap/saveMap, exponential-backoff retry, and CircuitBreaker integration. Shows how to wrap any existing fetchX() service call with CircuitBreaker.execute() as a one-liner and clarifies when each resilience layer is appropriate.
---

# Resilient Panel — Unified Pattern (v2)

Create dashboard panel components using **vanilla TypeScript** (no framework, no JSX).
Each panel is a class extending a `Panel` base class and optionally wraps its data source with a `CircuitBreaker`.

---

## Architecture Overview

```
Panel (base class)
├── element: HTMLElement (.panel)
│   ├── header: HTMLElement (.panel-header)
│   │   ├── headerLeft (.panel-header-left)
│   │   │   ├── title (.panel-title)
│   │   │   └── newBadge (.panel-new-badge) [optional, via setNewBadge()]
│   │   ├── statusBadge (.panel-data-badge) [optional, via setDataBadge()]
│   │   └── countEl (.panel-count) [optional]
│   ├── content: HTMLElement (.panel-content)
│   ├── resizeHandleRow (.panel-resize-handle-row)   [bottom edge]
│   └── resizeHandleCol (.panel-resize-handle-col)   [right edge]
```

### Resilience layers (choose what you need)

| Layer | File | When to use |
|---|---|---|
| **isFetching guard** | `Panel` base class | Always — prevents concurrent duplicate requests |
| **Exponential-backoff retry** | `Panel.fetchWithRetry()` | When you own the fetch call and want per-request retry before surfacing an error |
| **Error countdown in showError()** | `Panel` base class | Automatic retry countdown rendered inside the error state UI |
| **CircuitBreaker** | `src/utils/circuit-breaker.ts` | When a service may fail repeatedly; stops hammering the API and serves stale cache instead |
| **localStorage persistence** | `Panel.saveMap()` / `loadMap()` | When you want grid-span (not pixel size) state to survive page reloads |

> **Wrapping an existing service call with CircuitBreaker is a one-liner:**
> ```typescript
> // Before
> const data = await fetchStockQuotes();
> // After — adds caching + cooldown with zero refactor
> const data = await breaker.execute(() => fetchStockQuotes(), []);
> ```

---

## File 1 — `src/components/Panel.ts`

> **Authoritative implementation.** Use this verbatim; do not revert to
> the older `saveState()`/`loadState()` width/height approach.

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
  protected headerLeft: HTMLElement;
  protected countEl: HTMLElement | null = null;
  protected statusBadge: HTMLElement | null = null;
  protected newBadge: HTMLElement | null = null;
  protected panelId: string;

  private _fetching = false;
  private _countdownInterval: ReturnType<typeof setInterval> | null = null;
  private _resizeListeners: Array<{ target: EventTarget; type: string; fn: EventListenerOrEventListenerObject }> = [];

  // Retry state (used by fetchWithRetry)
  protected retryAttempts = 0;
  protected retryDelay = 1000; // ms; doubles each attempt
  private maxRetries = 3;

  constructor(options: PanelOptions) {
    this.panelId = options.id;

    this.element = document.createElement('div');
    this.element.className = `panel${options.className ? ' ' + options.className : ''}`;
    this.element.dataset.panel = options.id;

    // Header
    this.header = document.createElement('div');
    this.header.className = 'panel-header';

    this.headerLeft = document.createElement('div');
    this.headerLeft.className = 'panel-header-left';

    const title = document.createElement('span');
    title.className = 'panel-title';
    title.textContent = options.title;
    this.headerLeft.appendChild(title);
    this.header.appendChild(this.headerLeft);

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

    // Dual resize handles
    const resizeHandleRow = document.createElement('div');
    resizeHandleRow.className = 'panel-resize-handle-row';

    const resizeHandleCol = document.createElement('div');
    resizeHandleCol.className = 'panel-resize-handle-col';

    this.element.appendChild(this.header);
    this.element.appendChild(this.content);
    this.element.appendChild(resizeHandleRow);
    this.element.appendChild(resizeHandleCol);

    this._initResizeHandles(resizeHandleRow, resizeHandleCol);
    this.showLoading();
  }

  // Public API

  public getElement(): HTMLElement { return this.element; }

  /** Radar-ring loading animation with optional message. */
  public showLoading(message = 'Loading...'): void {
    this._clearCountdown();
    this.content.innerHTML = `
      <div class="panel-loading">
        <div class="panel-loading-radar">
          <div class="radar-ring radar-ring-1"></div>
          <div class="radar-ring radar-ring-2"></div>
          <div class="radar-ring radar-ring-3"></div>
        </div>
        <div class="panel-loading-text">${message}</div>
      </div>`;
  }

  /**
   * Show an error state.
   * If autoRetryMs is provided, renders an exponential-backoff countdown
   * inside the error UI and calls onRetry automatically when it reaches 0.
   */
  public showError(
    message = 'Failed to load',
    onRetry?: () => void,
    autoRetryMs?: number
  ): void {
    this._clearCountdown();
    const countdownId = `panel-error-countdown-${this.panelId}`;
    this.content.innerHTML = `
      <div class="panel-error-state">
        <div class="panel-error-msg">${message}</div>
        ${autoRetryMs != null
          ? `<div class="panel-error-countdown" id="${countdownId}">Retrying in ${Math.ceil(autoRetryMs / 1000)}s...</div>`
          : ''}
        ${onRetry ? '<button class="panel-retry-btn" data-panel-retry>Retry</button>' : ''}
      </div>`;

    if (onRetry) {
      this.content.querySelector('[data-panel-retry]')
        ?.addEventListener('click', () => { this._clearCountdown(); onRetry(); });
    }

    if (autoRetryMs != null && onRetry) {
      let remaining = autoRetryMs;
      const getEl = () => this.content.querySelector<HTMLElement>(`#${countdownId}`);
      this._countdownInterval = setInterval(() => {
        remaining -= 1000;
        if (remaining <= 0) {
          this._clearCountdown();
          onRetry();
        } else {
          const node = getEl();
          if (node) node.textContent = `Retrying in ${Math.ceil(remaining / 1000)}s...`;
        }
      }, 1000);
    }
  }

  public setContent(html: string): void {
    this._clearCountdown();
    this.content.innerHTML = html;
  }

  public setCount(count: number): void {
    if (this.countEl) this.countEl.textContent = count.toString();
  }

  /**
   * Show (or update) the data-status badge in the panel header.
   * Pass a variant CSS modifier such as 'live', 'stale', or 'error'.
   */
  public setDataBadge(text: string, variant = 'live'): void {
    if (!this.statusBadge) {
      this.statusBadge = document.createElement('span');
      this.statusBadge.className = 'panel-data-badge';
      this.header.appendChild(this.statusBadge);
    }
    this.statusBadge.textContent = text;
    this.statusBadge.dataset.variant = variant;
  }

  public clearDataBadge(): void {
    this.statusBadge?.remove();
    this.statusBadge = null;
  }

  /** Show the "NEW" badge next to the panel title (idempotent). */
  public setNewBadge(text = 'NEW'): void {
    if (!this.newBadge) {
      this.newBadge = document.createElement('span');
      this.newBadge.className = 'panel-new-badge';
      this.headerLeft.appendChild(this.newBadge);
    }
    this.newBadge.textContent = text;
  }

  public clearNewBadge(): void {
    this.newBadge?.remove();
    this.newBadge = null;
  }

  public show(): void { this.element.classList.remove('hidden'); }
  public hide(): void { this.element.classList.add('hidden'); }

  /** Full teardown: removes element, clears countdown timer, removes all resize listeners. */
  public destroy(): void {
    this._clearCountdown();
    for (const { target, type, fn } of this._resizeListeners) {
      target.removeEventListener(type, fn);
    }
    this._resizeListeners = [];
    this.element.remove();
  }

  // Protected helpers

  protected setFetching(v: boolean): void { this._fetching = v; }
  protected get isFetching(): boolean { return this._fetching; }

  /**
   * Fetch url with exponential-backoff retry (up to maxRetries attempts).
   * Reset this.retryAttempts and this.retryDelay before calling if you
   * want a fresh retry sequence (e.g. from a user-initiated Retry button).
   */
  protected async fetchWithRetry(url: string): Promise<unknown> {
    while (this.retryAttempts < this.maxRetries) {
      try {
        const response = await fetch(url);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return await response.json();
      } catch (error) {
        this.retryAttempts++;
        if (this.retryAttempts >= this.maxRetries) {
          throw new Error(
            `Failed after ${this.maxRetries} attempts: ${(error as Error).message}`
          );
        }
        await new Promise(resolve => setTimeout(resolve, this.retryDelay));
        this.retryDelay *= 2;
      }
    }
  }

  // Grid-span persistence
  //
  // Panels live inside a CSS grid. Their size is expressed as span classes
  // (e.g. "col-span-2", "row-span-3"), NOT inline pixel widths/heights.
  // Use saveMap / loadMap to persist this.

  /**
   * Persist an arbitrary key->value map to localStorage under this panel's key.
   * Typical use: save current grid-span class numbers.
   *
   *   this.saveMap({ colSpan: '2', rowSpan: '1', collapsed: 'false' });
   */
  public saveMap(data: Record<string, string>): void {
    localStorage.setItem(`panelState_${this.panelId}`, JSON.stringify(data));
  }

  /**
   * Load the map saved by saveMap(), or null if nothing was stored.
   *
   *   const state = this.loadMap();
   *   if (state) {
   *     this.element.classList.add(`col-span-${state.colSpan}`);
   *     this.element.classList.add(`row-span-${state.rowSpan}`);
   *     if (state.collapsed === 'true') this.element.classList.add('collapsed');
   *   }
   */
  public loadMap(): Record<string, string> | null {
    const raw = localStorage.getItem(`panelState_${this.panelId}`);
    if (!raw) return null;
    try {
      return JSON.parse(raw) as Record<string, string>;
    } catch {
      return null;
    }
  }

  // Private internals

  private _clearCountdown(): void {
    if (this._countdownInterval !== null) {
      clearInterval(this._countdownInterval);
      this._countdownInterval = null;
    }
  }

  /**
   * Wire up the two resize handles.
   * Row handle drags vertically and updates the row-span class.
   * Col handle drags horizontally and updates the col-span class.
   * All listeners are tracked in _resizeListeners for clean destroy().
   */
  private _initResizeHandles(
    rowHandle: HTMLElement,
    colHandle: HTMLElement
  ): void {
    const track = (
      target: EventTarget,
      type: string,
      fn: EventListenerOrEventListenerObject
    ) => {
      target.addEventListener(type, fn);
      this._resizeListeners.push({ target, type, fn });
    };

    // Row resize (bottom handle)
    let rowDragging = false;
    let rowStartY = 0;
    let rowStartSpan = 1;

    const onRowMouseDown = (e: Event) => {
      const me = e as MouseEvent;
      rowDragging = true;
      rowStartY = me.clientY;
      const m = (this.element.className.match(/row-span-(\d+)/) ?? [])[1];
      rowStartSpan = m ? parseInt(m, 10) : 1;
      me.preventDefault();
    };
    const onRowMouseMove = (e: Event) => {
      if (!rowDragging) return;
      const me = e as MouseEvent;
      const delta = Math.round((me.clientY - rowStartY) / 60);
      const newSpan = Math.max(1, Math.min(6, rowStartSpan + delta));
      this.element.className = this.element.className
        .replace(/row-span-\d+/, `row-span-${newSpan}`)
        .trim();
      if (!/row-span-\d+/.test(this.element.className)) {
        this.element.classList.add(`row-span-${newSpan}`);
      }
    };
    const onRowMouseUp = () => { rowDragging = false; };

    track(rowHandle, 'mousedown', onRowMouseDown);
    track(document,  'mousemove', onRowMouseMove);
    track(document,  'mouseup',   onRowMouseUp);

    // Col resize (right handle)
    let colDragging = false;
    let colStartX = 0;
    let colStartSpan = 1;

    const onColMouseDown = (e: Event) => {
      const me = e as MouseEvent;
      colDragging = true;
      colStartX = me.clientX;
      const m = (this.element.className.match(/col-span-(\d+)/) ?? [])[1];
      colStartSpan = m ? parseInt(m, 10) : 1;
      me.preventDefault();
    };
    const onColMouseMove = (e: Event) => {
      if (!colDragging) return;
      const me = e as MouseEvent;
      const delta = Math.round((me.clientX - colStartX) / 120);
      const newSpan = Math.max(1, Math.min(12, colStartSpan + delta));
      this.element.className = this.element.className
        .replace(/col-span-\d+/, `col-span-${newSpan}`)
        .trim();
      if (!/col-span-\d+/.test(this.element.className)) {
        this.element.classList.add(`col-span-${newSpan}`);
      }
    };
    const onColMouseUp = () => { colDragging = false; };

    track(colHandle, 'mousedown', onColMouseDown);
    track(document,  'mousemove', onColMouseMove);
    track(document,  'mouseup',   onColMouseUp);
  }
}
```

---

## File 2 — `src/utils/circuit-breaker.ts`

```typescript
interface CircuitState {
  failures: number;
  cooldownUntil: number;
}

interface CacheEntry<T> {
  data: T;
  timestamp: number;
}

export interface CircuitBreakerOptions {
  name: string;
  maxFailures?: number;   // default 2
  cooldownMs?: number;    // default 5 min
  cacheTtlMs?: number;    // default 10 min
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
    if (this.state.cooldownUntil > 0) {
      this.state = { failures: 0, cooldownUntil: 0 };
    }
    return false;
  }

  getCached(): T | null {
    if (this.cache && Date.now() - this.cache.timestamp < this.cacheTtlMs) {
      return this.cache.data;
    }
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
      console.warn(`[${this.name}] Circuit open — cooldown ${this.cooldownMs / 1000}s`);
    }
  }

  /**
   * Execute fn. On success, caches result. On failure, records failure and
   * returns defaultValue. While on cooldown or cache is fresh, skips fn
   * entirely and returns cached/default value.
   */
  async execute<R extends T>(fn: () => Promise<R>, defaultValue: R): Promise<R> {
    if (this.isOnCooldown()) {
      const cached = this.getCached();
      return (cached as R) ?? defaultValue;
    }
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

export function createCircuitBreaker<T>(
  options: CircuitBreakerOptions
): CircuitBreaker<T> {
  return new CircuitBreaker<T>(options);
}
```

---

## File 3 — `src/utils/sparkline.ts`

```typescript
/**
 * Returns an inline SVG sparkline string, or '' if data is too short.
 * Embed the return value directly in HTML template strings.
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

## Key differences from the old Panel pattern

| Feature | Old pattern | New (authoritative) pattern |
|---|---|---|
| Persistence | `saveState()`/`loadState()` with inline `width`/`height` strings | `saveMap()`/`loadMap()` with arbitrary key/value map; store grid-span class numbers |
| Resize | Single `.panel-resize-handle` | Dual handles: `.panel-resize-handle-row` (vertical) + `.panel-resize-handle-col` (horizontal) |
| Loading UI | Spinner div | Radar-ring animation (3 nested `.radar-ring` divs) |
| Error UI | Static message + optional retry button | Message + optional countdown timer (`autoRetryMs`) + retry button; countdown clears on manual retry |
| Badges | None | `setDataBadge(text, variant)` / `clearDataBadge()` and `setNewBadge(text)` / `clearNewBadge()` |
| `destroy()` | `element.remove()` only | Clears countdown interval + removes all resize `document` listeners + `element.remove()` |

---

## Creating a Concrete Panel

### Option A — Wrapping an existing project service with CircuitBreaker (preferred)

Use this when the project already has a `fetchStockQuotes()` (or similar) function.
The circuit breaker is a **one-liner wrapper** — no refactoring of the service needed.

```typescript
import { Panel } from './Panel';
import { createCircuitBreaker } from '../utils/circuit-breaker';
import { fetchStockQuotes } from '../services/stocks'; // existing project service

interface StockQuote {
  symbol: string;
  name: string;
  price: number | null;
  change: number | null;
  sparkline?: number[];
}

// Module-level breaker — shared across re-renders, persists cache between fetches
const breaker = createCircuitBreaker<StockQuote[]>({
  name: 'StockMarket',
  cacheTtlMs: 60_000,
  cooldownMs: 5 * 60_000,
});

export class StockPanel extends Panel {
  private refreshTimer: ReturnType<typeof setInterval> | null = null;

  constructor() {
    super({ id: 'stocks', title: 'Stock Market', showCount: true });

    // Restore grid-span state from localStorage
    const state = this.loadMap();
    if (state) {
      if (state.colSpan) this.element.classList.add(`col-span-${state.colSpan}`);
      if (state.rowSpan) this.element.classList.add(`row-span-${state.rowSpan}`);
      if (state.collapsed === 'true') this.element.classList.add('collapsed');
    }

    this.fetchData();
    this.refreshTimer = setInterval(() => this.fetchData(), 60_000);

    const btn = document.createElement('button');
    btn.className = 'panel-refresh-btn';
    btn.textContent = 'Refresh';
    btn.addEventListener('click', () => this.fetchData());
    this.header.appendChild(btn);

    this.setNewBadge('NEW');
  }

  private async fetchData(): Promise<void> {
    if (this.isFetching) return;
    this.setFetching(true);
    this.showLoading();
    try {
      const quotes = await breaker.execute(() => fetchStockQuotes(), []);
      this.setContent(this.renderQuotes(quotes));
      this.setCount(quotes.length);
      this.setDataBadge('LIVE', 'live');
      const colM = this.element.className.match(/col-span-(\d+)/);
      const rowM = this.element.className.match(/row-span-(\d+)/);
      this.saveMap({
        colSpan: colM ? colM[1] : '1',
        rowSpan: rowM ? rowM[1] : '1',
      });
    } catch (err) {
      this.setDataBadge('ERROR', 'error');
      this.showError('Failed to load stock data', () => this.fetchData(), 30_000);
    } finally {
      this.setFetching(false);
    }
  }

  private renderQuotes(quotes: StockQuote[]): string {
    if (!quotes.length) return '<p class="panel-empty">No data</p>';
    return quotes.map(q => `
      <div class="stock-row">
        <span class="stock-symbol">${q.symbol}</span>
        <span class="stock-name">${q.name}</span>
        <span class="stock-price">${q.price ?? '—'}</span>
        <span class="stock-change ${(q.change ?? 0) >= 0 ? 'positive' : 'negative'}">
          ${q.change != null ? (q.change >= 0 ? '+' : '') + q.change.toFixed(2) + '%' : '—'}
        </span>
      </div>`).join('');
  }

  public override destroy(): void {
    if (this.refreshTimer) clearInterval(this.refreshTimer);
    super.destroy();
  }
}
```

### Option B — Direct fetch with `fetchWithRetry` (no external service)

Use this when there is no existing service function and you want full control over
the HTTP call with per-request retry baked in.

```typescript
export class NewsPanel extends Panel {
  constructor() {
    super({ id: 'news', title: 'Latest News' });
    const state = this.loadMap();
    if (state?.collapsed === 'true') this.element.classList.add('collapsed');
    this.load();
  }

  private async load(): Promise<void> {
    if (this.isFetching) return;
    this.setFetching(true);
    this.retryAttempts = 0;
    this.retryDelay    = 1000;
    try {
      const data = await this.fetchWithRetry('/api/news') as NewsItem[];
      this.setContent(this.renderNews(data));
      this.setDataBadge('LIVE', 'live');
    } catch (err) {
      this.setDataBadge('ERROR', 'error');
      this.showError('Could not load news', () => this.load(), 15_000);
    } finally {
      this.setFetching(false);
    }
  }

  private renderNews(items: NewsItem[]): string {
    return items.map(i => `<div class="news-item">${i.headline}</div>`).join('');
  }
}
```

---

## CSS reference for new features

```css
/* Radar loading animation */
.panel-loading-radar {
  position: relative;
  width: 40px;
  height: 40px;
  margin: 0 auto 8px;
}
.radar-ring {
  position: absolute;
  inset: 0;
  border-radius: 50%;
  border: 2px solid var(--accent, #4af);
  opacity: 0;
  animation: radar-pulse 1.8s ease-out infinite;
}
.radar-ring-2 { animation-delay: 0.6s; }
.radar-ring-3 { animation-delay: 1.2s; }
@keyframes radar-pulse {
  0%   { transform: scale(0.3); opacity: 0.8; }
  100% { transform: scale(1.4); opacity: 0; }
}

/* Data badge variants */
.panel-data-badge[data-variant="live"]  { background: var(--green, #2a2); color: #fff; }
.panel-data-badge[data-variant="stale"] { background: var(--yellow, #aa2); color: #fff; }
.panel-data-badge[data-variant="error"] { background: var(--red, #a22);   color: #fff; }

/* Dual resize handles */
.panel-resize-handle-row {
  position: absolute;
  bottom: 0;
  left: 0;
  right: 0;
  height: 6px;
  cursor: row-resize;
}
.panel-resize-handle-col {
  position: absolute;
  top: 0;
  right: 0;
  bottom: 0;
  width: 6px;
  cursor: col-resize;
}

/* Error countdown */
.panel-error-countdown {
  font-size: 0.75rem;
  opacity: 0.7;
  margin-top: 4px;
}
```
