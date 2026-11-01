---
name: panel-breaker-backoff
description: Unified guide for authoring dashboard panel components in vanilla TypeScript — covers the Panel base class architecture, localStorage state persistence, exponential-backoff retry, and CircuitBreaker integration. Shows how to wrap any existing fetchX() service call with CircuitBreaker.execute() as a one-liner and clarifies when each resilience layer is appropriate.
---

# Resilient Panel — Unified Pattern

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
│   │   │   └── newBadge (.panel-new-badge) [optional]
│   │   ├── statusBadge (.panel-data-badge) [optional]
│   │   └── countEl (.panel-count) [optional]
│   ├── content: HTMLElement (.panel-content)
│   └── resizeHandle (.panel-resize-handle)
```

### Resilience layers (choose what you need)

| Layer | File | When to use |
|---|---|---|
| **isFetching guard** | `Panel` base class | Always — prevents concurrent duplicate requests |
| **Exponential-backoff retry** | `Panel.fetchWithRetry()` | When you own the fetch call and want per-request retry before surfacing an error |
| **CircuitBreaker** | `src/utils/circuit-breaker.ts` | When a service may fail repeatedly; stops hammering the API and serves stale cache instead |
| **localStorage persistence** | `Panel.saveState()` / `loadState()` | When you want collapsed/size state to survive page reloads |

> **Wrapping an existing service call with CircuitBreaker is a one-liner:**
> ```typescript
> // Before
> const data = await fetchStockQuotes();
> // After — adds caching + cooldown with zero refactor
> const data = await breaker.execute(() => fetchStockQuotes(), []);
> ```

---

## File 1 — `src/components/Panel.ts`

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

  // Retry state (used by fetchWithRetry)
  protected retryAttempts = 0;
  protected retryDelay = 1000; // ms; doubles each attempt
  private maxRetries = 3;

  constructor(options: PanelOptions) {
    this.panelId = options.id;
    this.element = document.createElement('div');
    this.element.className = `panel ${options.className ?? ''}`;
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

  // ── Public API ───────────────────────────────────────────────────────

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
      this.content.querySelector('[data-panel-retry]')
        ?.addEventListener('click', onRetry);
    }
  }

  public setContent(html: string): void { this.content.innerHTML = html; }

  public setCount(count: number): void {
    if (this.countEl) this.countEl.textContent = count.toString();
  }

  public show(): void { this.element.classList.remove('hidden'); }
  public hide(): void { this.element.classList.add('hidden'); }

  public destroy(): void { this.element.remove(); }

  // ── Protected helpers ────────────────────────────────────────────────

  protected setFetching(v: boolean): void { this._fetching = v; }
  protected get isFetching(): boolean { return this._fetching; }

  /**
   * Fetch `url` with exponential-backoff retry (up to maxRetries attempts).
   * Reset `this.retryAttempts` and `this.retryDelay` before calling if you
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

  /** Persist expanded/collapsed state and dimensions to localStorage. */
  public saveState(): void {
    localStorage.setItem(`panelState_${this.panelId}`, JSON.stringify({
      isExpanded: !this.element.classList.contains('collapsed'),
      width: this.element.style.width,
      height: this.element.style.height,
    }));
  }

  /** Restore state saved by saveState(). */
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
    this.loadState();       // restore collapsed/size from localStorage
    this.fetchData();
    this.refreshTimer = setInterval(() => this.fetchData(), 60_000);

    // Optional: manual refresh button in header
    const btn = document.createElement('button');
    btn.className = 'panel-refresh-btn';
    btn.textContent = 'Refresh';
    btn.addEventListener('click', () => this.fetchData());
    this.header.appendChild(btn);
  }

  private async fetchData(): Promise<void> {
    if (this.isFetching) return;
    this.setFetching(true);
    try {
      // One-liner: wrap existing service call with circuit breaker
      const quotes = await breaker.execute(() => fetchStockQuotes(), []);
      this.render(quotes);
      this.setCount(quotes.length);
      this.saveState();
    } catch (err) {
      this.showError(
        `Failed to load stock data: ${(err as Error).message}`,
        () => this.fetchData()
      );
    } finally {
      this.setFetching(false);
    }
  }

  private render(quotes: StockQuote[]): void {
    const rows = quotes.map(q => `
      <div class="stock-row">
        <span class="stock-symbol">${q.symbol}</span>
        <span class="stock-name">${q.name}</span>
        <span class="stock-price">${q.price != null ? '$' + q.price.toFixed(2) : '--'}</span>
        <span class="stock-change ${(q.change ?? 0) >= 0 ? 'positive' : 'negative'}">
          ${q.change != null ? (q.change >= 0 ? '+' : '') + q.change.toFixed(2) + '%' : '--'}
        </span>
      </div>`).join('');
    this.setContent(`<div class="stock-list">${rows}</div>`);
  }

  public override destroy(): void {
    if (this.refreshTimer) clearInterval(this.refreshTimer);
    super.destroy();
  }
}
```

### Option B — Direct fetch with `fetchWithRetry` (no existing service layer)

Use this when there is no project service and you want per-request exponential backoff:

```typescript
import { Panel } from './Panel';

interface NewsArticle {
  id: string;
  title: string;
  url: string;
}

export class NewsPanel extends Panel {
  private refreshTimer: ReturnType<typeof setInterval> | null = null;

  constructor() {
    super({ id: 'news', title: 'Live News', showCount: true });
    this.fetchData();
    this.refreshTimer = setInterval(() => this.fetchData(), 120_000);
  }

  private async fetchData(): Promise<void> {
    if (this.isFetching) return;
    this.setFetching(true);
    try {
      // fetchWithRetry handles up to 3 attempts with exponential backoff
      const articles = await this.fetchWithRetry('/api/news') as NewsArticle[];
      this.render(articles);
      this.setCount(articles.length);
    } catch (err) {
      this.showError(
        `News unavailable: ${(err as Error).message}`,
        () => {
          // Reset retry counters before user-initiated retry
          this.retryAttempts = 0;
          this.retryDelay = 1000;
          this.fetchData();
        }
      );
    } finally {
      this.setFetching(false);
    }
  }

  private render(articles: NewsArticle[]): void {
    const items = articles.map(a =>
      `<div class="news-item"><a href="${a.url}">${a.title}</a></div>`
    ).join('');
    this.setContent(`<div class="news-list">${items}</div>`);
  }

  public override destroy(): void {
    if (this.refreshTimer) clearInterval(this.refreshTimer);
    super.destroy();
  }
}
```

### Option C — Plain fetch, no resilience layer

For non-critical panels or mock/dev data where retries add no value:

```typescript
private async fetchData(): Promise<void> {
  if (this.isFetching) return;
  this.setFetching(true);
  try {
    const resp = await fetch('/api/config');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    this.render(data);
  } catch (err) {
    this.showError('Could not load config', () => this.fetchData());
  } finally {
    this.setFetching(false);
  }
}
```

---

## Decision Guide — Which resilience layer?

```
Does the project have an existing fetchX() service function?
  YES -> wrap it: breaker.execute(() => fetchX(), defaultValue)     <- Option A
  NO  -> does the endpoint fail intermittently under normal load?
           YES -> use fetchWithRetry() for per-request retry         <- Option B
           NO  -> plain fetch() with showError/retry is enough       <- Option C

Is the endpoint unreliable or rate-limited for extended periods?
  YES -> add CircuitBreaker (prevents hammering during outages)
  NO  -> fetchWithRetry or plain fetch is sufficient

Do you need panel state (size, collapsed) to survive page reloads?
  YES -> call this.loadState() in constructor (after super())
         call this.saveState() after a successful render
```

---

## Protected API Reference

All members below are accessible from subclasses without importing or inspecting `Panel.ts` directly:

| Member / Method | Type | Description |
|---|---|---|
| `element` | `HTMLElement` | Outer container div (`.panel`) |
| `header` | `HTMLElement` | Header bar div — append extra controls here |
| `content` | `HTMLElement` | Content area div (`.panel-content`) |
| `countEl` | `HTMLElement \| null` | Count badge, or `null` if `showCount` not set |
| `panelId` | `string` | The `id` from `PanelOptions` |
| `retryAttempts` | `number` | Current retry count for `fetchWithRetry` |
| `retryDelay` | `number` | Current delay (ms) for `fetchWithRetry`; reset to 1000 before retry |
| `isFetching` | `boolean` (getter) | `true` while an async fetch is in progress |
| `setFetching(v)` | `void` | Set the fetching guard flag |
| `showLoading(msg?)` | `void` | Replace content with a loading spinner |
| `showError(msg?, onRetry?)` | `void` | Replace content with an error state and optional retry button |
| `setContent(html)` | `void` | Set raw HTML into the content area |
| `setCount(n)` | `void` | Update the count badge (no-op if `countEl` is null) |
| `fetchWithRetry(url)` | `Promise<unknown>` | Fetch with exponential-backoff retry (3 attempts) |
| `saveState()` | `void` | Persist expanded/size state to localStorage |
| `loadState()` | `void` | Restore state from localStorage |

> **Example — appending a button to the header in a subclass:**
> ```typescript
> constructor() {
>   super({ id: 'insights', title: 'Insights', className: 'panel-wide' });
>   const btn = document.createElement('button');
>   btn.className = 'panel-refresh-btn';
>   btn.textContent = 'Refresh';
>   btn.addEventListener('click', () => this.generate());
>   this.header.appendChild(btn); // 'header' is protected — safe to use here
> }
> ```

---

## Key Patterns (checklist)

1. **Constructor**: call `super()` with `PanelOptions`, optionally call `loadState()`, then trigger initial data fetch.
2. **fetchData()**: async, use `isFetching` guard, wrap service calls with `breaker.execute()` when available.
3. **render()**: build HTML strings, call `this.setContent(html)`, then `this.saveState()`.
4. **Error recovery**: `showError(message, () => this.fetchData())` — retry button is wired automatically.
5. **destroy()**: clear all `setInterval` / `setTimeout` handles, then call `super.destroy()`.
6. **Sparklines**: import `miniSparkline` from `src/utils/sparkline.ts`, embed return value directly in HTML template strings.
7. **Header controls**: `this.header.appendChild(el)` — `header` is `protected`, safe to use in subclasses.

---

## localStorage Utilities (optional helpers)

See `examples/localStorageUtils.ts` for typed wrappers when you need to persist additional per-panel data beyond the built-in size/collapsed state (e.g. user filter selections, last-viewed item).
