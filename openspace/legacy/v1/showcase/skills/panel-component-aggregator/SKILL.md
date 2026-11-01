---
name: panel-component-aggregator
description: Dashboard panel component pattern for aggregator/summary panels that accept pushed partial data updates via updateData(), defer rendering until first data arrives, and incrementally re-render metrics without re-fetching — ideal for panels that synthesize data already fetched by other panels.
---

# Aggregator Panel Component Pattern

Create **aggregator** dashboard panel components using **vanilla TypeScript** (no framework, no JSX).
An aggregator panel does **not** fetch data itself. Instead, it receives partial data pushes from sibling panels (or a coordinator) via `updateData(partial)`, defers its first render until enough data has arrived, and re-renders incrementally on each subsequent push.

This skill is self-contained. It covers the base `Panel` class, the `AggregatorPanel` generic base class, and a concrete example (`SummaryPanel`).

---

## When to Use This Pattern

| Use case | Fetching Panel | Aggregator Panel |
|---|---|---|
| Panel owns its own API endpoint | yes | no |
| Panel summarises data from multiple sources | no | yes |
| Data arrives asynchronously from siblings | no | yes |
| Re-renders on each push without extra network calls | no | yes |

---

## Architecture Overview

```
Panel (base class)
└── AggregatorPanel<TData> (generic aggregator base)
    ├── partialData: Partial<TData>         — accumulated pushed fields
    ├── receivedKeys: Set<keyof TData>      — tracks which keys have arrived
    ├── requiredKeys: (keyof TData)[]       — keys needed before first render
    ├── lastUpdated: Map<keyof TData, number> — staleness timestamps
    └── SummaryPanel (concrete example)
```

Data flow:

```
StockPanel ──pushes──► coordinator.push('stocks', quotes)
EmailPanel ──pushes──► coordinator.push('emails', messages)
                               │
                    AggregatorPanel.updateData({ stocks, emails })
                               │
                    ┌──────────▼──────────┐
                    │  enough data yet?   │
                    │  no  → showPending  │
                    │  yes → render()     │
                    └─────────────────────┘
```

---

## Base Panel Class

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
    this.element.className = `panel ${options.className || ''}`.trim();
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
  }

  public getElement(): HTMLElement { return this.element; }

  public showLoading(message = 'Loading...'): void {
    this.content.innerHTML = `
      <div class="panel-loading">
        <div class="panel-loading-spinner"></div>
        <div class="panel-loading-text">${message}</div>
      </div>`;
  }

  /** Show a "waiting for data" placeholder — softer than showLoading */
  public showPending(message = 'Waiting for data\u2026'): void {
    this.content.innerHTML = `
      <div class="panel-pending">
        <div class="panel-pending-text">${message}</div>
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

  protected setFetching(v: boolean): void { this._fetching = v; }
  protected get isFetching(): boolean { return this._fetching; }

  public destroy(): void { this.element.remove(); }
}
```

---

## AggregatorPanel Base Class

Create `src/components/AggregatorPanel.ts`:

```typescript
import { Panel, PanelOptions } from './Panel';

export interface AggregatorPanelOptions<TData> extends PanelOptions {
  /**
   * Keys that MUST be present before the first render is attempted.
   * Until all required keys have been received at least once, the panel
   * shows a pending placeholder.
   */
  requiredKeys: (keyof TData)[];

  /**
   * How old (ms) a field can be before it is considered stale and a
   * staleness indicator is shown in the header. Defaults to 5 minutes.
   */
  staleThresholdMs?: number;
}

/**
 * Base class for panels that aggregate data pushed from other panels
 * rather than fetching independently.
 *
 * Usage:
 *   1. Extend AggregatorPanel<YourDataShape>.
 *   2. Implement render(data: YourDataShape): void.
 *   3. Call updateData(partial) whenever a sibling pushes new data.
 */
export abstract class AggregatorPanel<TData extends Record<string, unknown>>
  extends Panel {

  private partialData: Partial<TData> = {};
  private receivedKeys = new Set<keyof TData>();
  private readonly requiredKeys: (keyof TData)[];
  private readonly staleThresholdMs: number;
  private lastUpdated = new Map<keyof TData, number>();
  private hasRenderedOnce = false;

  constructor(options: AggregatorPanelOptions<TData>) {
    super(options);
    this.requiredKeys = options.requiredKeys;
    this.staleThresholdMs = options.staleThresholdMs ?? 5 * 60 * 1000;
    // Show pending placeholder immediately — no spinner, no fetch
    this.showPending(this.buildPendingMessage());
  }

  // ── Public API ──────────────────────────────────────────────────────────

  /**
   * Accept a partial data push. Merges incoming fields into accumulated
   * state, timestamps each updated key, and either triggers the first
   * render (if required keys are now satisfied) or re-renders incrementally.
   */
  public updateData(partial: Partial<TData>): void {
    for (const key of Object.keys(partial) as (keyof TData)[]) {
      this.partialData[key] = partial[key];
      this.receivedKeys.add(key);
      this.lastUpdated.set(key, Date.now());
    }

    if (!this.hasRequiredData()) {
      this.showPending(this.buildPendingMessage());
      return;
    }

    // All required keys present — safe to cast
    try {
      this.render(this.partialData as TData);
      this.hasRenderedOnce = true;
      this.updateStalenessIndicators();
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      this.showError(`Render error: ${message}`, () => {
        this.updateData({});
      });
    }
  }

  /** Returns true if every requiredKey has been received at least once. */
  public hasRequiredData(): boolean {
    return this.requiredKeys.every(k => this.receivedKeys.has(k));
  }

  /** Returns the set of required keys that have not yet been received. */
  public getMissingKeys(): (keyof TData)[] {
    return this.requiredKeys.filter(k => !this.receivedKeys.has(k));
  }

  /** Returns true if a specific key's data is older than staleThresholdMs. */
  public isStale(key: keyof TData): boolean {
    const ts = this.lastUpdated.get(key);
    if (ts == null) return false;
    return Date.now() - ts > this.staleThresholdMs;
  }

  /**
   * Force-clears accumulated data and reverts to pending state.
   * Useful when the data source resets (e.g. user logs out).
   */
  public reset(): void {
    this.partialData = {};
    this.receivedKeys.clear();
    this.lastUpdated.clear();
    this.hasRenderedOnce = false;
    this.showPending(this.buildPendingMessage());
  }

  // ── Protected helpers ───────────────────────────────────────────────────

  /**
   * Subclasses MUST implement this. Called every time updateData() receives
   * a push after required keys are satisfied. `data` is guaranteed to
   * contain all requiredKeys.
   */
  protected abstract render(data: TData): void;

  /**
   * Returns the accumulated partial snapshot. Useful inside render() for
   * optional (non-required) keys.
   */
  protected getSnapshot(): Partial<TData> {
    return { ...this.partialData };
  }

  /**
   * True on the first render call, false on subsequent updates.
   * Use to decide between full DOM replacement vs. in-place patch.
   */
  protected get isFirstRender(): boolean {
    return !this.hasRenderedOnce;
  }

  // ── Private helpers ─────────────────────────────────────────────────────

  private buildPendingMessage(): string {
    const missing = this.getMissingKeys() as string[];
    if (missing.length === 0) return 'Processing\u2026';
    return `Waiting for: ${missing.join(', ')}`;
  }

  private updateStalenessIndicators(): void {
    const staleKeys = this.requiredKeys.filter(k => this.isStale(k));
    const indicator = this.element.querySelector('.panel-stale-indicator');
    if (staleKeys.length > 0) {
      const msg = `Stale data: ${(staleKeys as string[]).join(', ')}`;
      if (indicator) {
        indicator.textContent = msg;
      } else {
        const el = document.createElement('div');
        el.className = 'panel-stale-indicator';
        el.textContent = msg;
        this.header.appendChild(el);
      }
    } else {
      indicator?.remove();
    }
  }
}
```

---

## Concrete Example: SummaryPanel

Create `src/components/SummaryPanel.ts`:

```typescript
import { AggregatorPanel } from './AggregatorPanel';
import { StockQuote } from '../services/stock-service';
import { EmailMessage } from '../services/email-service';
import { CalendarEvent } from '../services/calendar-service';

interface SummaryData {
  stocks: StockQuote[];
  emails: EmailMessage[];
  events: CalendarEvent[];
}

export class SummaryPanel extends AggregatorPanel<SummaryData> {
  constructor() {
    super({
      id: 'summary',
      title: 'Daily Summary',
      className: 'panel-wide',
      showCount: true,
      // Render as soon as stocks + emails arrive; events are optional bonus
      requiredKeys: ['stocks', 'emails'],
      staleThresholdMs: 3 * 60 * 1000,
    });
  }

  protected render(data: SummaryData): void {
    const snapshot = this.getSnapshot();

    const unread  = data.emails.filter(e => !e.read).length;
    const gainers = data.stocks.filter(q => (q.change ?? 0) > 0).length;
    const losers  = data.stocks.filter(q => (q.change ?? 0) < 0).length;

    // Optional key — may not have arrived yet
    const upcomingCount = snapshot.events
      ? snapshot.events.filter(ev => new Date(ev.start) > new Date()).length
      : null;

    const eventsHtml = upcomingCount != null
      ? `<div class="summary-metric" data-metric="events">
           <span class="summary-metric-label">Upcoming events</span>
           <span class="summary-metric-value">${upcomingCount}</span>
         </div>`
      : '';

    if (this.isFirstRender) {
      // Full DOM build on first render
      this.setContent(`
        <div class="summary-metrics">
          <div class="summary-metric" data-metric="unread">
            <span class="summary-metric-label">Unread emails</span>
            <span class="summary-metric-value">${unread}</span>
          </div>
          <div class="summary-metric" data-metric="gainers">
            <span class="summary-metric-label">Stocks up</span>
            <span class="summary-metric-value positive">${gainers}</span>
          </div>
          <div class="summary-metric" data-metric="losers">
            <span class="summary-metric-label">Stocks down</span>
            <span class="summary-metric-value negative">${losers}</span>
          </div>
          ${eventsHtml}
        </div>
      `);
    } else {
      // Incremental patch — avoid full DOM thrash on subsequent pushes
      this.patchMetric('unread', unread);
      this.patchMetric('gainers', gainers);
      this.patchMetric('losers', losers);
      if (upcomingCount != null) this.patchMetric('events', upcomingCount);
    }

    this.setCount(data.emails.length + data.stocks.length);
  }

  /** Update a single metric value in-place */
  private patchMetric(name: string, value: number): void {
    const el = this.content.querySelector(
      `[data-metric="${name}"] .summary-metric-value`
    );
    if (el) el.textContent = String(value);
  }
}
```

---

## Coordinator / Push Pattern

Create `src/services/panel-coordinator.ts`:

```typescript
import { SummaryPanel } from '../components/SummaryPanel';

/**
 * Lightweight pub/sub coordinator. Fetching panels call coordinator.push()
 * after each successful render; aggregator panels receive those updates.
 */
export class PanelCoordinator {
  private summaryPanel: SummaryPanel;

  constructor(summaryPanel: SummaryPanel) {
    this.summaryPanel = summaryPanel;
  }

  push<K extends 'stocks' | 'emails' | 'events'>(
    key: K,
    value: unknown
  ): void {
    this.summaryPanel.updateData({ [key]: value } as any);
  }
}
```

In your fetching panels, call `coordinator.push()` after each successful render:

```typescript
// Inside StockPanel.fetchData(), after this.render(quotes):
coordinator.push('stocks', quotes);

// Inside EmailPanel.fetchData(), after this.render(messages):
coordinator.push('emails', messages);
```

---

## Key Patterns

1. **Constructor** calls `super()` with `requiredKeys` — the minimum fields needed before the first render.
2. **updateData(partial)** is the only public ingestion point. It merges, timestamps, and either shows a pending placeholder or triggers `render()`.
3. **render(data)** receives a fully-typed snapshot; use `this.getSnapshot()` for optional keys.
4. **isFirstRender** lets you choose between a full DOM replace (first time) vs. incremental in-place patch (subsequent pushes) to avoid unnecessary reflow.
5. **Staleness indicators** are automatically injected into the header when a key's last-update timestamp exceeds `staleThresholdMs`.
6. **reset()** clears all state and reverts to pending — call it on session logout or data source disconnect.
7. Aggregator panels never call `fetch()` or `showLoading()` — they call `showPending()` until required data arrives.
8. The coordinator is a thin pub/sub shim; replace with EventEmitter, RxJS Subject, or your app's store if available.

---

## Aggregator vs Fetching Panel Checklist

| Concern | Fetching Panel | Aggregator Panel |
|---|---|---|
| Network call | `fetchWithRetry(url)` | none |
| Initial state | `showLoading()` in constructor | `showPending()` in constructor |
| Data ingestion | internal async fetch | `updateData(partial)` |
| First render gate | none | `hasRequiredData()` |
| Re-render strategy | full replace | incremental patch preferred |
| Staleness | timer-based refetch | automatic header indicator |
| `destroy()` | clear interval + super | super only (no timers to clear) |

---

## TypeScript Strict-Mode Notes

- `TData extends Record<string, unknown>` ensures key iteration is safe.
- `Object.keys(partial) as (keyof TData)[]` is safe because `partial` is `Partial<TData>`.
- The `as TData` cast in `this.render(this.partialData as TData)` is guarded by `hasRequiredData()` — a runtime guarantee backing the compile-time assertion.
- If your project enables `exactOptionalPropertyTypes`, declare optional aggregated sources as `key?: T | undefined` in `TData`.
