---
name: data-driven-panel
description: Create dashboard panel components with integrated resilient data services, combining UI construction and data fetching into a unified pattern.
---

# Data-Driven Panel Pattern

Create dashboard panels that seamlessly integrate UI construction with resilient data fetching. Each panel is a self-contained component that manages its own data lifecycle.

## Architecture Overview

```
Panel (base class)
├── element: HTMLElement (outer container)
│   ├── header (title, status indicators)
│   └── content (data display area)
└── DataService (internal)
    ├── Circuit Breaker (failure handling)
    └── Caching layer
```

## Implementation

### 1. Base Panel with Integrated Data Service

Create `src/components/DataPanel.ts`:

```typescript
import { createCircuitBreaker } from '../utils/circuit-breaker';

export interface PanelOptions<T> {
  id: string;
  title: string;
  showCount?: boolean;
  className?: string;
  dataService: () => Promise<T>;
  defaultValue: T;
  serviceOptions?: {
    maxFailures?: number;
    cooldownMs?: number;
    cacheTtlMs?: number;
  };
}

export abstract class DataPanel<T> {
  protected element: HTMLElement;
  protected content: HTMLElement;
  private breaker: CircuitBreaker<T>;
  private refreshTimer: ReturnType<typeof setInterval> | null = null;

  constructor(protected options: PanelOptions<T>) {
    this.breaker = createCircuitBreaker<T>({
      name: options.id,
      maxFailures: options.serviceOptions?.maxFailures ?? 2,
      cooldownMs: options.serviceOptions?.cooldownMs ?? 300_000,
      cacheTtlMs: options.serviceOptions?.cacheTtlMs ?? 60_000,
    });

    this.element = document.createElement('div');
    this.element.className = `panel ${options.className || ''}`;
    this.element.dataset.panel = options.id;

    // Initialize UI
    this.initUI();
    this.fetchData();
  }

  private initUI(): void {
    // Header setup
    const header = document.createElement('div');
    header.className = 'panel-header';
    
    const title = document.createElement('span');
    title.className = 'panel-title';
    title.textContent = this.options.title;
    header.appendChild(title);

    // Content area
    this.content = document.createElement('div');
    this.content.className = 'panel-content';

    this.element.appendChild(header);
    this.element.appendChild(this.content);
    this.showLoading();
  }

  protected async fetchData(): Promise<void> {
    try {
      const data = await this.breaker.execute(
        this.options.dataService,
        this.options.defaultValue
      );
      this.render(data);
    } catch (err) {
      this.showError('Failed to load data', () => this.fetchData());
    }
  }

  protected abstract render(data: T): void;

  public showLoading(message = 'Loading...'): void {
    this.content.innerHTML = `<div class="loading">${message}</div>`;
  }

  public showError(message: string, retryHandler: () => void): void {
    this.content.innerHTML = `
      <div class="error">
        <p>${message}</p>
        <button class="retry-btn">Retry</button>
      </div>`;
    this.content.querySelector('.retry-btn')?.addEventListener('click', retryHandler);
  }

  public setAutoRefresh(intervalMs: number): void {
    this.refreshTimer = setInterval(() => this.fetchData(), intervalMs);
  }

  public destroy(): void {
    if (this.refreshTimer) clearInterval(this.refreshTimer);
    this.element.remove();
  }
}
```

### 2. Concrete Panel Implementation Example

```typescript
import { DataPanel } from './DataPanel';

interface StockData {
  quotes: StockQuote[];
  updatedAt: string;
}

export class StockPanel extends DataPanel<StockData> {
  constructor() {
    super({
      id: 'stocks',
      title: 'Stock Market',
      dataService: fetchStockData,
      defaultValue: { quotes: [], updatedAt: new Date().toISOString() },
      serviceOptions: {
        cacheTtlMs: 30_000 // Refresh cache every 30 seconds
      }
    });
    this.setAutoRefresh(60_000); // Auto-refresh every minute
  }

  protected render(data: StockData): void {
    const html = data.quotes.map(quote => `
      <div class="stock-item">
        <span class="symbol">${quote.symbol}</span>
        <span class="price">$${quote.price?.toFixed(2) || '—'}</span>
      </div>
    `).join('');
    
    this.content.innerHTML = `
      <div class="stock-list">
        ${html}
        <div class="updated">Updated: ${new Date(data.updatedAt).toLocaleTimeString()}</div>
      </div>`;
  }
}
```

### 3. Data Service Implementation

```typescript
// src/services/stockService.ts
interface StockQuote {
  symbol: string;
  price: number | null;
}

export async function fetchStockData(): Promise<StockData> {
  const response = await fetch('/api/stocks');
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const quotes: StockQuote[] = await response.json();
  return {
    quotes,
    updatedAt: new Date().toISOString()
  };
}
```

## Key Benefits

1. **Unified Pattern**: Combines UI and data logic in one coherent pattern
2. **Resilient Data Fetching**: Built-in circuit breaker and caching
3. **Simplified Implementation**: Concrete panels only need to implement rendering
4. **Consistent Behavior**: All panels share the same loading/error states
5. **Flexible Configuration**: Service options customizable per panel

## Best Practices

1. **Service Composition**: Create small, focused services for each data type
2. **Default Values**: Always provide meaningful fallback data
3. **Error Boundaries**: Use the built-in error handling for consistency
4. **Memory Management**: Clean up timers and event listeners in destroy()
5. **Type Safety**: Maintain strong typing throughout the data flow
