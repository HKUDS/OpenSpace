---
name: panel-visual-badges
description: Create visually rich, robust TypeScript UI components with panel architecture, combining enhanced features like retry logic, state persistence, and dynamic styling with a systematic workflow for visual enhancements.
---

# Robust UI Panel Components

Build visually rich, robust TypeScript UI components using a panel architecture pattern. This skill combines enhanced panel features (retry logic, state persistence) with a systematic workflow for adding visual enhancements (badges, thumbnails, dynamic styling).

## Architecture Overview

### Core Panel Features
- **Retry Logic**: Automatically retries failed data fetches with exponential backoff.
- **State Persistence**: Saves panel state (expanded/collapsed, size) to localStorage.
- **Detailed Error Handling**: Provides granular error messages and recovery options.
- **Visual Enhancements**: Systematic approach for adding badges, thumbnails, and dynamic styling.

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

## Implementation Workflow

### 1. Define Constants and Configuration

```typescript
// Panel configuration
const PANEL_DEFAULTS = {
  maxRetries: 3,
  initialRetryDelay: 1000, // 1s
};

// Visual configuration
const SOURCE_COLORS: { [key: string]: string } = {
  'Category A': '#FF6B6B',
  'Category B': '#4ECDC4',
  'default': '#95A5A6'
};

const THUMBNAIL_SIZE = 48; // pixels
```

### 2. Base Panel Class with Enhanced Features

```typescript
export class Panel {
  protected element: HTMLElement;
  protected content: HTMLElement;
  protected panelId: string;
  private retryAttempts = 0;
  private retryDelay = PANEL_DEFAULTS.initialRetryDelay;

  constructor(options: PanelOptions) {
    // ... existing constructor code ...
    this.loadState();
  }

  protected async fetchWithRetry(url: string):<any> {
    while (this.retryAttempts < PANEL_DEFAULTS.maxRetries) {
      try {
        const response = await fetch(url);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return await response.json();
      } catch (error) {
        this.retryAttempts++;
        if (this.retryAttempts >= PANEL_DEFAULTS.maxRetries) {
          throw new Error(`Failed after ${PANEL_DEFAULTS.maxRetries} attempts: ${error.message}`);
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

  // Visual enhancement helpers
  protected getColorForSource(source: string): string {
    return SOURCE_COLORS[source] || SOURCE_COLORS['default'];
  }

  protected getThumbnailUrl(imageUrl?: string): string {
    return imageUrl || `https://via.placeholder.com/${THUMBNAIL_SIZE}`;
  }
}
```

### 3. Creating Enhanced Components

```typescript
export class NewsPanel extends Panel {
  private refreshTimer: ReturnType<typeof setInterval> | null = null;

  constructor() {
    super({ id: 'news', title: 'News Feed', showCount: true });
    this.fetchData();
    this.refreshTimer = setInterval(() => this.fetchData(), 60_000);
  }

  private async fetchData(): Promise<void> {
    try {
      const newsItems = await this.fetchWithRetry('/api/news');
      this.render(newsItems);
      this.setCount(newsItems.length);
      this.saveState();
    } catch (err) {
      this.showError(`Failed to load news: ${err.message}`, () => {
        this.retryAttempts = 0;
        this.retryDelay = PANEL_DEFAULTS.initialRetryDelay;
        this.fetchData();
      });
    }
  }

  private render(items: NewsItem[]): void {
    const html = items.map(item => `
      <div class="news-item">
        <img src="${this.getThumbnailUrl(item.imageUrl)}" class="news-thumbnail">
        <div class="news-content">
          <h3>${item.title}</h3>
          <span class="news-badge" style="background-color: ${this.getColorForSource(item.source)};">
            ${item.source}
          </span>
          <p>${item.summary}</p>
        </div>
      </div>
    `).join('');

    this.setContent(`<div class="news-list">${html}</div>`);
  }

  public override destroy(): void {
    if (this.refreshTimer) clearInterval(this.refreshTimer);
    super.destroy();
  }
}
```

### 4. Corresponding CSS

```css
/* Panel structure */
.panel {
  border: 1px solid #ddd;
  border-radius: 8px;
  overflow: hidden;
}

/* News item styles */
.news-item {
  display: flex;
  gap: 16px;
  padding: 12px;
  border-bottom: 1px solid #eee;
}

.news-thumbnail {
  width: 48px;
  height: 48px;
  border-radius: 4px;
  object-fit: cover;
}

.news-badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 12px;
  color: white;
  margin-right: 8px;
}
```

## Key Patterns

1. **Panel Architecture**: Base class handles core functionality (retry logic, state management)
2. **Visual Enhancements**: Systematic workflow for adding badges, thumbnails, and dynamic styling
3. **Separation of Concerns**:
   - TypeScript for logic and data-driven styles
   - CSS for structural and static styling
4. **Error Handling**: Built-in retry logic with user-friendly error displays
5. **State Management**: Automatic persistence of UI state

## Best Practices

1. **Constants First**: Define colors, sizes, and configuration at the top
2. **Helper Methods**: Create focused methods for visual transformations
3. **Template Literals**: Build HTML with clear conditional rendering
4. **CSS Coordination**: Update stylesheets for every new visual element
5. **Testing**: Verify with edge cases (missing data, network errors)
