---
name: panel-component-with-push-sidebar
description: Dashboard panel components (class-based, self-fetching) and push-update sidebar modules (functional, externally-driven) using vanilla TypeScript DOM API, with retry logic, localStorage persistence, and guarded CSS injection.
---

# Panel & Push-Update Sidebar Patterns

Two complementary patterns for building dashboard UI in **vanilla TypeScript** (no framework, no JSX):

| Pattern | Use when… |
|---|---|
| **Panel class** (extends `Panel`) | The component fetches its own data on a timer |
| **Push-update sidebar module** | Data arrives from outside (caller pushes it in) |

---

## Part 1 — Panel Class Pattern

### Architecture Overview

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

### Base Panel Class

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

  // --- Retry state (reset before each logical fetch sequence) ---
  private retryAttempts = 0;
  private maxRetries = 3;
  private retryDelay = 1000; // ms; doubles on each attempt

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

  // ----------------------------------------------------------------
  // Public API
  // ----------------------------------------------------------------

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

  // ----------------------------------------------------------------
  // Protected API — available in subclasses
  // ----------------------------------------------------------------

  protected setFetching(v: boolean): void { this._fetching = v; }
  protected get isFetching(): boolean { return this._fetching; }

  /**
   * Fetch a URL with exponential-backoff retry.
   * Call resetRetry() before each new fetch sequence.
   */
  protected async fetchWithRetry(url: string): Promise<unknown> {
    while (this.retryAttempts < this.maxRetries) {
      try {
        const res = await fetch(url);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return await res.json();
      } catch (err: unknown) {
        this.retryAttempts++;
        if (this.retryAttempts >= this.maxRetries) {
          const msg = err instanceof Error ? err.message : String(err);
          throw new Error(`Failed after ${this.maxRetries} attempts: ${msg}`);
        }
        await new Promise(r => setTimeout(r, this.retryDelay));
        this.retryDelay *= 2;
      }
    }
  }

  /** Reset retry counters before a fresh fetch sequence. */
  protected resetRetry(): void {
    this.retryAttempts = 0;
    this.retryDelay = 1000;
  }

  // ----------------------------------------------------------------
  // State persistence (localStorage)
  // ----------------------------------------------------------------

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

### Protected API Reference

| Member / Method | Type | Description |
|---|---|---|
| `element` | `HTMLElement` | Outer container div (`.panel`) |
| `header` | `HTMLElement` | Header bar — append extra controls here |
| `content` | `HTMLElement` | Scrollable content area |
| `countEl` | `HTMLElement \| null` | Count badge, or `null` if `showCount` not set |
| `panelId` | `string` | The `id` from `PanelOptions` |
| `isFetching` | `boolean` getter | `true` while an async fetch is in progress |
| `setFetching(v)` | `void` | Set/clear the fetching guard |
| `showLoading(msg?)` | `void` | Replace content with a spinner |
| `showError(msg?, onRetry?)` | `void` | Replace content with error + optional retry button |
| `setContent(html)` | `void` | Set raw HTML into the content area |
| `setCount(n)` | `void` | Update count badge (no-op if `countEl` is null) |
| `fetchWithRetry(url)` | `Promise<unknown>` | Fetch with exponential-backoff retry (3 attempts) |
| `resetRetry()` | `void` | Reset retry counters before a new fetch sequence |
| `saveState()` | `void` | Persist expanded/size state to localStorage |
| `loadState()` | `void` | Restore state from localStorage |

### Creating a Concrete Panel (Example: StockPanel)

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
    this.loadState();          // restore saved size/collapsed state
    this.fetchData();
    this.refreshTimer = setInterval(() => this.fetchData(), 60_000);
  }

  private async fetchData(): Promise<void> {
    if (this.isFetching) return;
    this.setFetching(true);
    this.resetRetry();         // start a fresh retry sequence
    try {
      const quotes = await this.fetchWithRetry('/api/stocks') as StockQuote[];
      this.render(quotes);
      this.setCount(quotes.length);
      this.saveState();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Unknown error';
      this.showError(`Failed to load stock data: ${msg}`, () => this.fetchData());
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
        ${miniSparkline(q.sparkline, q.change)}
      </div>`).join('');
    this.setContent(`<div class="stock-list">${rows}</div>`);
  }

  public override destroy(): void {
    if (this.refreshTimer) clearInterval(this.refreshTimer);
    super.destroy();
  }
}
```

### Key Patterns for Self-Fetching Panels

1. **Constructor** → `super()` → `loadState()` → initial `fetchData()` → start refresh timer
2. **fetchData()** → `isFetching` guard → `resetRetry()` → `fetchWithRetry()` → `render()` + `saveState()`
3. **render()** builds HTML strings → `this.setContent(html)`
4. **destroy()** clears timers, calls `super.destroy()`
5. Use `showLoading()` during initial load (auto-called in constructor)
6. Use `showError(msg, retryFn)` on failure; retryFn must call `fetchData()` (which calls `resetRetry()`)

---

## Part 2 — Push-Update Sidebar Pattern

Use this pattern when the component **does not fetch data itself** — instead it receives data pushed by an external caller (e.g. a WebSocket handler, a store subscription, or a parent orchestrator).

### When to use this pattern vs. Panel class

```
Self-fetching?  → Panel class (Part 1)
Data pushed in? → Push-update sidebar module (Part 2)
```

### Module Structure

A push-update sidebar is a **plain TypeScript module** (not a class) with:

| Export | Purpose |
|---|---|
| `createXSidebar(): HTMLElement` | Build and return the root element (idempotent singleton) |
| `updateX(data: XData): void` | Re-render only the sections that changed |
| `interface XData` | The data shape the caller must provide |

Internally the module uses:
- A **module-level singleton** `let sidebarEl: HTMLElement | null = null`
- A **`SectionRefs` interface** caching live DOM node references to avoid repeated `querySelector` calls
- A **guarded `injectStyles()`** function that inserts a `<style>` tag exactly once

### Skeleton

```typescript
// src/components/FooSidebar.ts

// ── Types ────────────────────────────────────────────────────────────────────

export interface FooData {
  title: string;
  items: { id: string; label: string; value: number }[];
  lastUpdated: Date;
}

// ── DOM node cache ────────────────────────────────────────────────────────────

interface SectionRefs {
  root:      HTMLElement;
  titleEl:   HTMLElement;
  listEl:    HTMLElement;
  footerEl:  HTMLElement;
}

// ── Singleton state ───────────────────────────────────────────────────────────

let sidebarEl: HTMLElement | null = null;
let refs: SectionRefs | null = null;
let stylesInjected = false;

// ── CSS injection ─────────────────────────────────────────────────────────────

function injectStyles(): void {
  if (stylesInjected) return;            // guard: run exactly once
  stylesInjected = true;
  const style = document.createElement('style');
  style.dataset.owner = 'foo-sidebar';   // easy to find in DevTools
  style.textContent = `
    .foo-sidebar { /* … */ }
    .foo-sidebar__title { font-weight: 600; }
    .foo-sidebar__list  { list-style: none; padding: 0; }
    .foo-sidebar__footer { font-size: 0.75rem; color: var(--muted); }
  `;
  document.head.appendChild(style);
}

// ── Builder ───────────────────────────────────────────────────────────────────

/**
 * Create (or return the existing) sidebar element.
 * Idempotent — safe to call multiple times; always returns the same node.
 */
export function createFooSidebar(): HTMLElement {
  if (sidebarEl) return sidebarEl;       // singleton guard

  injectStyles();

  const root = document.createElement('aside');
  root.className = 'foo-sidebar';

  const titleEl = document.createElement('h2');
  titleEl.className = 'foo-sidebar__title';
  root.appendChild(titleEl);

  const listEl = document.createElement('ul');
  listEl.className = 'foo-sidebar__list';
  root.appendChild(listEl);

  const footerEl = document.createElement('div');
  footerEl.className = 'foo-sidebar__footer';
  root.appendChild(footerEl);

  // Cache references — avoids querySelector on every update
  refs = { root, titleEl, listEl, footerEl };
  sidebarEl = root;
  return root;
}

// ── Updater ───────────────────────────────────────────────────────────────────

/**
 * Push new data into the sidebar.
 * Calls createFooSidebar() defensively if refs is not yet initialised.
 */
export function updateFoo(data: FooData): void {
  if (!refs) createFooSidebar();
  const r = refs!;

  r.titleEl.textContent = data.title;

  r.listEl.innerHTML = data.items
    .map(item => `
      <li class="foo-sidebar__item" data-id="${item.id}">
        <span class="foo-item-label">${item.label}</span>
        <span class="foo-item-value">${item.value}</span>
      </li>`)
    .join('');

  r.footerEl.textContent =
    `Updated ${data.lastUpdated.toLocaleTimeString()}`;
}
```

### Real-World Example: TodayFocusSidebar

A sidebar that summarises the user's day — greeting, meeting countdown, inbox counts, stock alerts, CI failures, and an AI briefing with truncate/expand toggle. Data is pushed in from an external orchestrator.

```typescript
// src/components/TodayFocusSidebar.ts

export interface FocusData {
  userName: string;
  nextMeeting: { title: string; startsInMinutes: number } | null;
  inboxCounts: { email: number; slack: number; github: number };
  stockAlerts: { symbol: string; changePercent: number }[]; // pre-filtered >= 2%
  ciFailures: { repo: string; branch: string }[];
  aiBriefing: string; // may be long — sidebar truncates with toggle
}

interface SectionRefs {
  root:       HTMLElement;
  greetingEl: HTMLElement;
  meetingEl:  HTMLElement;
  inboxEl:    HTMLElement;
  stocksEl:   HTMLElement;
  ciEl:       HTMLElement;
  briefingEl: HTMLElement;
}

let sidebarEl: HTMLElement | null = null;
let refs: SectionRefs | null = null;
let stylesInjected = false;

// ── Helpers ───────────────────────────────────────────────────────────────────

function greeting(name: string): string {
  const h = new Date().getHours();
  const salutation = h < 12 ? 'Good morning' : h < 17 ? 'Good afternoon' : 'Good evening';
  return `${salutation}, ${name}`;
}

function formatCountdown(minutes: number): string {
  if (minutes <= 0) return 'Now';
  if (minutes < 60) return `in ${minutes}m`;
  return `in ${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

// ── CSS ───────────────────────────────────────────────────────────────────────

function injectStyles(): void {
  if (stylesInjected) return;
  stylesInjected = true;
  const s = document.createElement('style');
  s.dataset.owner = 'today-focus-sidebar';
  s.textContent = `
    .tfs { display:flex; flex-direction:column; gap:12px; padding:16px;
           background:var(--bg-surface, #1e1e2e); color:var(--fg, #cdd6f4);
           border-radius:8px; font-family:inherit; }
    .tfs__greeting  { font-size:1.1rem; font-weight:600; }
    .tfs__meeting   { font-size:0.85rem; color:var(--yellow, #f9e2af); }
    .tfs__section   { font-size:0.85rem; }
    .tfs__label     { font-size:0.7rem; text-transform:uppercase;
                      letter-spacing:.06em; color:var(--muted, #6c7086);
                      margin-bottom:4px; }
    .tfs__briefing  { font-size:0.82rem; line-height:1.5; }
    .tfs__toggle    { background:none; border:none; color:var(--blue, #89b4fa);
                      cursor:pointer; font-size:0.8rem; padding:2px 0; }
    .tfs__alert-pos { color:var(--green, #a6e3a1); }
    .tfs__alert-neg { color:var(--red,   #f38ba8); }
    .tfs__ci-fail   { color:var(--red,   #f38ba8); }
  `;
  document.head.appendChild(s);
}

// ── Builder ───────────────────────────────────────────────────────────────────

export function createTodayFocusSidebar(): HTMLElement {
  if (sidebarEl) return sidebarEl;
  injectStyles();

  const make = (tag: string, cls: string): HTMLElement => {
    const el = document.createElement(tag);
    el.className = cls;
    return el as HTMLElement;
  };

  const root       = make('aside', 'tfs');
  const greetingEl = make('div',   'tfs__greeting');
  const meetingEl  = make('div',   'tfs__meeting');
  const inboxEl    = make('div',   'tfs__section');
  const stocksEl   = make('div',   'tfs__section');
  const ciEl       = make('div',   'tfs__section');
  const briefingEl = make('div',   'tfs__section');

  root.append(greetingEl, meetingEl, inboxEl, stocksEl, ciEl, briefingEl);
  refs = { root, greetingEl, meetingEl, inboxEl, stocksEl, ciEl, briefingEl };
  sidebarEl = root;
  return root;
}

// ── Updater ───────────────────────────────────────────────────────────────────

const BRIEFING_LIMIT = 200;

export function updateTodayFocus(data: FocusData): void {
  if (!refs) createTodayFocusSidebar();
  const r = refs!;

  // Greeting
  r.greetingEl.textContent = greeting(data.userName);

  // Next meeting
  if (data.nextMeeting) {
    r.meetingEl.textContent =
      `Next: ${data.nextMeeting.title} — ${formatCountdown(data.nextMeeting.startsInMinutes)}`;
    r.meetingEl.hidden = false;
  } else {
    r.meetingEl.hidden = true;
  }

  // Inbox counts
  const { email, slack, github } = data.inboxCounts;
  r.inboxEl.innerHTML = `
    <div class="tfs__label">Inbox</div>
    Email: ${email} &nbsp; Slack: ${slack} &nbsp; GitHub: ${github}`;

  // Stock alerts (caller should pre-filter >= 2%, but we guard here too)
  const alerts = data.stockAlerts.filter(s => Math.abs(s.changePercent) >= 2);
  r.stocksEl.innerHTML = alerts.length === 0
    ? ''
    : `<div class="tfs__label">Stock Alerts</div>` +
      alerts.map(s => {
        const cls  = s.changePercent >= 0 ? 'tfs__alert-pos' : 'tfs__alert-neg';
        const sign = s.changePercent >= 0 ? '+' : '';
        return `<span class="${cls}">${s.symbol} ${sign}${s.changePercent.toFixed(1)}%</span>`;
      }).join(' &nbsp; ');

  // CI failures
  r.ciEl.innerHTML = data.ciFailures.length === 0
    ? ''
    : `<div class="tfs__label">CI Failures</div>` +
      data.ciFailures
        .map(f => `<div class="tfs__ci-fail">${f.repo} / ${f.branch}</div>`)
        .join('');

  // AI briefing with truncate/expand toggle
  const text = data.aiBriefing;
  if (text.length <= BRIEFING_LIMIT) {
    r.briefingEl.innerHTML =
      `<div class="tfs__label">Briefing</div><div class="tfs__briefing">${text}</div>`;
  } else {
    const short = text.slice(0, BRIEFING_LIMIT) + '…';
    r.briefingEl.innerHTML = `
      <div class="tfs__label">Briefing</div>
      <div class="tfs__briefing"
           data-full="${encodeURIComponent(text)}"
           data-short="${encodeURIComponent(short)}"
           data-expanded="false">${short}</div>
      <button class="tfs__toggle" data-briefing-toggle>Show more</button>`;
    r.briefingEl.querySelector('[data-briefing-toggle]')
      ?.addEventListener('click', function (this: HTMLButtonElement) {
        const div = r.briefingEl.querySelector<HTMLElement>('[data-full]')!;
        const expanded = div.dataset.expanded === 'true';
        div.textContent = decodeURIComponent(
          expanded ? div.dataset.short! : div.dataset.full!);
        div.dataset.expanded = String(!expanded);
        this.textContent = expanded ? 'Show more' : 'Show less';
      });
  }
}
```

### Push-Update Checklist

- [ ] Define `interface XData` (exported — callers need it)
- [ ] Define `interface SectionRefs` (internal — not exported)
- [ ] Declare module-level `let sidebarEl`, `let refs`, `let stylesInjected`
- [ ] `injectStyles()` inserts one `<style>` tag, guarded by `stylesInjected`; set `style.dataset.owner` for DevTools visibility
- [ ] `createXSidebar()` is idempotent: returns existing `sidebarEl` if already built
- [ ] `createXSidebar()` populates `refs` with live node references
- [ ] `updateX(data)` calls `createXSidebar()` defensively if `refs` is null
- [ ] `updateX(data)` mutates only nodes in `refs` — never calls `document.querySelector`
- [ ] Export `createXSidebar`, `updateX`, and `XData` interface; keep `SectionRefs` module-private

### Barrel File Wiring

After creating a push-update sidebar, register it in both barrel files:

```typescript
// src/components/index.ts
export { createTodayFocusSidebar, updateTodayFocus } from './TodayFocusSidebar';
export type { FocusData } from './TodayFocusSidebar';

// src/index.ts
export * from './components';
```

---

## Part 3 — Sparkline Utility

Used by both patterns for inline SVG sparklines:

```typescript
// src/utils/sparkline.ts
export function miniSparkline(
  data: number[] | undefined,
  change: number | null,
  w = 50,
  h = 16,
): string {
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
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">` +
    `<polyline points="${points}" fill="none" stroke="${color}" ` +
    `stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
}
```

---

## Quick-Reference Decision Tree

```
Need a new dashboard component?
│
├─ Will it fetch its own data (polling / one-shot)?
│   └─ YES → extend Panel (Part 1)
│             • constructor → super() → loadState() → fetchData() → setInterval
│             • fetchData() → resetRetry() → fetchWithRetry() → render() → saveState()
│             • destroy() → clearInterval → super.destroy()
│
└─ Will data be pushed from outside?
    └─ YES → functional push-update module (Part 2)
              • createXSidebar() — idempotent, builds DOM, fills SectionRefs
              • updateX(data)    — mutates only SectionRefs nodes
              • injectStyles()   — guarded, runs once
              • export createX, updateX, XData; keep SectionRefs private
```
