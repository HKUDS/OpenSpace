---
name: panel-state-persistence
description: Unified dashboard panel component with robust error handling, retry logic, state persistence, and vanilla TypeScript implementation.
---

# Unified Panel Component Pattern

Create dashboard panel components using **vanilla TypeScript** (no framework, no JSX). Each panel is a class extending a `Panel` base class with built-in advanced features.

## Architecture Overview

### Unified Features
- **Retry Logic**: Automatically retries failed data fetches with exponential backoff.
- **State Persistence**: Saves panel state (expanded/collapsed, size) to localStorage.
- **Detailed Error Handling**: Provides granular error messages and recovery options.
- **Base Panel Structure**: Standardized DOM structure with header, content area, and optional count badge.

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
  private retryAttempts = 0;
  private maxRetries = 3;
  private retryDelay = 1000; // starts at 1s, doubles each retry

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
    this.loadState();
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

  public destroy(): void {
    this.element.remove();
  }
}
```

## Creating a Concrete Panel (Example: StockPanel)

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

1. **Constructor** calls `super()` with panel config, then triggers initial data fetch and loads saved state.
2. **fetchData()** uses built-in retry logic and state persistence.
3. **render()** builds HTML strings and calls `this.setContent(html)`.
4. **destroy()** cleans up timers and event listeners.
5. Use `showLoading()` during initial load (auto-called in constructor).
6. Use `showError(msg, retryFn)` on failure with detailed error messages.
7. Sparkline SVGs use inline `<svg>` with `<polyline>` — see sparkline utility.

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
