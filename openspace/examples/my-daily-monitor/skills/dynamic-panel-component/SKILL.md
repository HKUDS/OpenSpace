---
name: dynamic-panel-component
description: Create a dashboard panel component with advanced dynamic content updates and robust error handling, including retry logic and state management, using vanilla TypeScript DOM API.
---

# Dynamic Panel Component Pattern

Create dashboard panel components using **vanilla TypeScript** (no framework, no JSX). Each panel is a class extending a `Panel` base class, with enhanced dynamic content updates and error handling.

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

## Base Panel Class (Enhanced)

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
  private _errorState = false;

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
    this._errorState = false;
    this.content.innerHTML = `
     <div class="panel-loading">
        <div class="panel-loading-spinner"></div>
        <div class="panel-loading-text">${message}</div>
      </div>`;
  }

  public showError(message = 'Failed to load', onRetry?: () => void): void {
    this._errorState = true;
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
    this._errorState = false;
    this.content.innerHTML = html;
  }

  public setCount(count: number): void {
    if (this.countEl) this.countEl.textContent = count.toString();
  }

  public show(): void { this.element.classList.remove('hidden'); }
  public hide(): void { this.element.classList.add('hidden'); }

  protected setFetching(v: boolean): void { this._fetching = v; }
  protected get isFetching(): boolean { return this._fetching; }
  protected get isErrorState(): boolean { return this._errorState; }

  public destroy(): void {
    this.element.remove();
  }
}
```

## Creating a Concrete Panel (Example: StockPanel with Enhanced Error Handling)

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
  private retryCount = 0;
  private maxRetries = 3;

  constructor() {
    super({ id: 'stocks', title: 'Stock Market', showCount: true });
    this.fetchData();
    this.refreshTimer = setInterval(() => this.fetchData(), 60_000);
  }

  private async fetchData(): Promise<void> {
    if (this.isFetching || this.isErrorState) return;
    this.setFetching(true);
    try {
      const quotes = await fetchStockQuotes(); // from data-service
      this.render(quotes);
      this.setCount(quotes.length);
      this.retryCount = 0; // Reset retry count on success
    } catch (err) {
      this.retryCount++;
      if (this.retryCount < this.maxRetries) {
        setTimeout(() => this.fetchData(), 1000 * this.retryCount); // Exponential backoff
      } else {
        this.showError('Failed to load stock data', () => {
          this.retryCount = 0;
          this.fetchData();
        });
      }
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

1. **Constructor** calls `super()` with panel config, then triggers initial data fetch.
2. **fetchData()** includes retry logic with exponential backoff and max retry limit.
3. **render()** builds HTML strings and calls `this.setContent(html)`.
4. **destroy()** cleans up timers and event listeners.
5. Use `showLoading()` during initial load (auto-called in constructor).
6. Use `showError(msg, retryFn)` on failure with retry button.
7. Track error state with `_errorState` to prevent redundant fetches.
8. Sparkline SVGs use inline `<svg>` with `<polyline>` — see sparkline utility.

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

## Additional Examples

### Dynamic Content Update Example

```typescript
export class NewsPanel extends Panel {
  private newsItems: NewsItem[] = [];

  constructor() {
    super({ id: 'news', title: 'Latest News' });
    this.fetchNews();
    setInterval(() => this.fetchNews(), 300_000); // Update every 5 minutes
  }

  private async fetchNews(): Promise<void> {
    if (this.isFetching) return;
    this.setFetching(true);
    try {
      const news = await fetchNewsItems();
      this.newsItems = news;
      this.renderNews();
    } catch (err) {
      this.showError('Failed to load news', () => this.fetchNews());
    } finally {
      this.setFetching(false);
    }
  }

  private renderNews(): void {
    const items = this.newsItems.map(item => `
      <div class="news-item">
        <h3 class="news-title">${item.title}</h3>
        <p class="news-summary">${item.summary}</p>
        <span class="news-time">${formatTime(item.time)}</span>
      </div>
    `).join('');
    this.setContent(`<div class="news-list">${items}</div>`);
  }
}
```

### Error Handling with State Management

```typescript
export class WeatherPanel extends Panel {
  private lastSuccessfulFetch: Date | null = null;

  constructor() {
    super({ id: 'weather', title: 'Weather Forecast' });
    this.fetchWeather();
  }

  private async fetchWeather(): Promise<void> {
    if (this.isFetching) return;
    this.setFetching(true);
    try {
      const weather = await fetchWeatherData();
      this.lastSuccessfulFetch = new Date();
      this.renderWeather(weather);
    } catch (err) {
      const message = this.lastSuccessfulFetch
        ? `Failed to update (last success: ${this.lastSuccessfulFetch.toLocaleTimeString()})`
        : 'Failed to load initial data';
      this.showError(message, () => this.fetchWeather());
    } finally {
      this.setFetching(false);
    }
  }

  private renderWeather(weather: WeatherData): void {
    const html = `
      <div class="weather-current">
        <span class="weather-temp">${weather.temp}°</span>
        <span class="weather-desc">${weather.description}</span>
      </div>
      <div class="weather-forecast">
        ${weather.forecast.map(day => `
          <div class="weather-day">
            <span class="weather-day-name">${day.day}</span>
            <span class="weather-day-temp">${day.high}°/${day.low}°</span>
          </div>
        `).join('')}
      </div>
    `;
    this.setContent(html);
  }
}
```
