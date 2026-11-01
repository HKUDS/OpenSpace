---
name: panel-component-xss-safe
description: Create a dashboard panel component using vanilla TypeScript DOM API, following the worldmonitor Panel architecture. Panels have a header with title/count, scrollable content area, loading/error states, and resize handles. Includes XSS-safe rendering pattern with esc() helper for safely interpolating untrusted external API data into innerHTML.
---

# Panel Component Pattern (XSS-Safe)

Create dashboard panel components using **vanilla TypeScript** (no framework, no JSX). Each panel is a class extending a `Panel` base class.

> **Security note:** Panels that consume external API data (calendar events, news feeds, stock names, user-supplied content, etc.) MUST escape all untrusted values before injecting them into `innerHTML`. Use the `esc()` helper documented below.

## Architecture Overview

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

## XSS-Safe Rendering: the `esc()` Helper

**Always use `esc()` when interpolating untrusted data into an HTML string.**  
Untrusted data includes anything from external APIs: event titles, locations, URLs, names, descriptions, symbols, etc.

Create `src/utils/esc.ts`:

```typescript
/**
 * Escapes a value for safe interpolation into an innerHTML string.
 * Converts &, <, >, ", and ' to their HTML entity equivalents.
 *
 * Usage:
 *   this.setContent(`<div class="title">${esc(item.title)}</div>`);
 *
 * Do NOT use for:
 *   - href/src attributes with user-controlled URLs — validate scheme instead
 *   - CSS values — use a separate sanitizer
 */
export function esc(value: unknown): string {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
```

### When to use `esc()`

| Data source | Safe? | Action |
|---|---|---|
| Hardcoded string literal in source | ✅ Safe | No escaping needed |
| Enum / controlled constant | ✅ Safe | No escaping needed |
| External API string field (title, name, body…) | ❌ Unsafe | **Use `esc()`** |
| User input / localStorage value | ❌ Unsafe | **Use `esc()`** |
| Number/boolean (rendered as text) | ✅ Safe | `String(n)` is fine |
| URL from external API | ⚠️ Unsafe | Validate scheme + `esc()` |

### URL Safety

For URLs from external sources, validate the scheme before injecting:

```typescript
/** Returns the URL only if it uses http or https; otherwise returns '#'. */
export function safeUrl(raw: unknown): string {
  const s = String(raw ?? '').trim();
  return /^https?:\/\//i.test(s) ? s : '#';
}
```

Usage: `<a href="${safeUrl(item.url)}">${esc(item.title)}</a>`

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
    this.element.className = `panel ${options.className || ''}`;
    this.element.dataset.panel = options.id;

    // Header
    this.header = document.createElement('div');
    this.header.className = 'panel-header';

    const headerLeft = document.createElement('div');
    headerLeft.className = 'panel-header-left';

    const title = document.createElement('span');
    title.className = 'panel-title';
    title.textContent = options.title;  // textContent is always safe
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
    // message is a hardcoded string — no escaping needed here
    this.content.innerHTML = `
      <div class="panel-loading">
        <div class="panel-loading-spinner"></div>
        <div class="panel-loading-text">${message}</div>
      </div>`;
  }

  public showError(message = 'Failed to load', onRetry?: () => void): void {
    // message is a hardcoded string — no escaping needed here
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

---

## Creating a Concrete Panel (Example: StockPanel — XSS-safe)

Import `esc` and wrap every API-derived string field:

```typescript
import { Panel } from './Panel';
import { esc } from '../utils/esc';

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
        <span class="stock-symbol">${esc(q.symbol)}</span>
        <span class="stock-name">${esc(q.name)}</span>
        <span class="stock-price">${q.price != null ? '$' + q.price.toFixed(2) : '&mdash;'}</span>
        <span class="stock-change ${(q.change ?? 0) >= 0 ? 'positive' : 'negative'}">
          ${q.change != null ? (q.change >= 0 ? '+' : '') + q.change.toFixed(2) + '%' : '&mdash;'}
        </span>
      </div>
    `).join('');
    //                ^^^         ^^^
    // symbol and name come from an external API — always escape them.
    // price and change are numbers rendered via .toFixed() — safe without esc().

    this.setContent(`<div class="stock-list">${rows}</div>`);
  }

  public override destroy(): void {
    if (this.refreshTimer) clearInterval(this.refreshTimer);
    super.destroy();
  }
}
```

## Creating a Concrete Panel (Example: EventPanel — calendar/news data)

Calendar and news panels are highest-risk because titles, locations, and URLs all come from untrusted sources:

```typescript
import { Panel } from './Panel';
import { esc, safeUrl } from '../utils/esc';

interface CalendarEvent {
  id: string;
  title: string;       // untrusted — user-created content
  location?: string;   // untrusted
  url?: string;        // untrusted — must validate scheme
  startTime: Date;
}

export class SchedulePanel extends Panel {
  private refreshTimer: ReturnType<typeof setInterval> | null = null;

  constructor() {
    super({ id: 'schedule', title: "Today's Schedule", showCount: true });
    this.fetchData();
    this.refreshTimer = setInterval(() => this.fetchData(), 5 * 60_000);
  }

  private async fetchData(): Promise<void> {
    if (this.isFetching) return;
    this.setFetching(true);
    try {
      const events = await fetchCalendarEvents();
      this.render(events);
      this.setCount(events.length);
    } catch (err) {
      this.showError('Failed to load calendar', () => this.fetchData());
    } finally {
      this.setFetching(false);
    }
  }

  private render(events: CalendarEvent[]): void {
    if (events.length === 0) {
      this.setContent('<div class="schedule-empty">No events today</div>');
      return;
    }

    const rows = events.map(ev => `
      <div class="schedule-row">
        <span class="schedule-time">${esc(formatTime(ev.startTime))}</span>
        <span class="schedule-title">
          ${ev.url
            ? `<a href="${safeUrl(ev.url)}" target="_blank" rel="noopener noreferrer">${esc(ev.title)}</a>`
            : esc(ev.title)
          }
        </span>
        ${ev.location ? `<span class="schedule-location">${esc(ev.location)}</span>` : ''}
      </div>
    `).join('');
    //              ^^^              ^^^              ^^^
    // title, location, url — ALL from external API, ALL escaped.
    // safeUrl() prevents javascript: / data: scheme injection in href.

    this.setContent(`<div class="schedule-list">${rows}</div>`);
  }

  public override destroy(): void {
    if (this.refreshTimer) clearInterval(this.refreshTimer);
    super.destroy();
  }
}
```

---

## Key Patterns

1. **Constructor** calls `super()` with panel config, then triggers initial data fetch
2. **fetchData()** is async, uses `isFetching` guard, shows error on failure with retry
3. **render()** builds HTML strings with `esc()` around every API-derived string, then calls `this.setContent(html)`
4. **destroy()** cleans up timers and event listeners
5. Use `showLoading()` during initial load (auto-called in constructor)
6. Use `showError(msg, retryFn)` on failure — `msg` should be a hardcoded string, not API data
7. **Import `esc` from `../utils/esc`** in every panel that consumes external data
8. Numbers and booleans rendered via `.toFixed()` / `.toString()` / template arithmetic are safe — no `esc()` needed
9. Use `textContent` instead of `innerHTML` for single text nodes when convenient — it is always safe

## Sparkline Utility

Sparklines use computed numbers only — no escaping needed:

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

---

## Checklist Before Submitting a Panel

- [ ] Every `string` field from an external API is wrapped in `esc()` before HTML interpolation
- [ ] Every URL from an external API is passed through `safeUrl()` before use in `href`/`src`
- [ ] `showError()` is only called with hardcoded messages (never raw API error bodies)
- [ ] `esc` and `safeUrl` are imported from `src/utils/esc`
- [ ] Numbers / computed values rendered with `.toFixed()` or arithmetic are left unwrapped (they cannot contain HTML)
