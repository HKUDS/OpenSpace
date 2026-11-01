---
name: worldmonitor-reference
description: Curated architecture index of the WorldMonitor open-source dashboard. Reference for extracting patterns — vanilla TypeScript, Panel class hierarchy, service layer, CSS grid, API edge functions, refresh scheduler, and 60+ data source integrations.
---

# WorldMonitor Architecture Reference

WorldMonitor is a real-time global intelligence dashboard built with vanilla TypeScript (no framework). This skill provides a curated index of its architecture, organized by the patterns most relevant to building a personal daily monitor.

## When to Use

- You are creating a new dashboard panel and need to follow WorldMonitor conventions
- You need to understand how a specific pattern works before adapting it
- You are deciding which WorldMonitor patterns to adopt vs. simplify for a personal project

## Project Overview

| Aspect | Detail |
|--------|--------|
| **Language** | TypeScript (strict), vanilla DOM — no React/Vue/Angular |
| **Bundler** | Vite |
| **Deployment** | Vercel (60+ Edge Functions) + Railway (WebSocket relay) |
| **Desktop** | Tauri 2 (Rust) with Node.js sidecar |
| **Map engines** | globe.gl + Three.js (3D), deck.gl + MapLibre GL (2D) |
| **CSS** | Custom properties, dark/light themes, responsive grid |
| **API contracts** | Protocol Buffers (92 proto files, 22 services) |
| **Data sources** | 435+ RSS feeds, 45 data layers, 30+ live video streams |

## Directory Structure

```
worldmonitor/
├── src/
│   ├── main.ts                    # Entry point: Sentry, analytics, App bootstrap
│   ├── App.ts                     # Root application class
│   ├── components/                # All UI panel classes (80+ files)
│   │   ├── Panel.ts               # *** BASE CLASS — most important file ***
│   │   ├── VirtualList.ts         # Virtual scrolling for large lists
│   │   ├── NewsPanel.ts           # News with clustering, NER, summaries
│   │   ├── MarketPanel.ts         # Stock market with sparklines
│   │   ├── SearchModal.ts         # Cmd+K command palette
│   │   ├── InsightsPanel.ts       # AI briefing + metrics
│   │   ├── WorldClockPanel.ts     # Multi-timezone clock
│   │   └── ...                    # 70+ more panels
│   ├── services/                  # Data layer (117 files)
│   │   ├── runtime.ts             # SmartPollLoop, desktop detection, fetch routing
│   │   ├── bootstrap.ts           # Initial data hydration from Redis
│   │   ├── rss.ts                 # RSS feed aggregation
│   │   ├── news/index.ts          # News fetching + processing
│   │   ├── market/index.ts        # Market data (Yahoo Finance, Finnhub)
│   │   ├── i18n.ts                # 21-language internationalization
│   │   ├── threat-classifier.ts   # 3-stage threat classification
│   │   ├── clustering.ts          # News headline clustering
│   │   └── ...                    # 100+ more services
│   ├── config/                    # Static configuration
│   │   ├── variant.ts             # Multi-variant (world/tech/finance/commodity/happy)
│   │   ├── feeds.ts               # 435+ RSS feed definitions
│   │   ├── panels.ts              # Panel enable/disable config
│   │   ├── markets.ts             # Stock symbol lists
│   │   └── ...
│   ├── app/                       # Application-level modules
│   │   ├── app-context.ts         # Shared state (inFlight, isDestroyed)
│   │   ├── refresh-scheduler.ts   # *** REFRESH PATTERN ***
│   │   ├── panel-layout.ts        # Grid layout management
│   │   ├── search-manager.ts      # Command palette logic
│   │   └── event-handlers.ts      # Global keyboard/visibility events
│   ├── utils/                     # Utilities (22 files)
│   │   ├── dom-utils.ts           # h(), fragment(), replaceChildren() — JSX-free helpers
│   │   ├── sanitize.ts            # escapeHtml(), sanitizeUrl() — XSS prevention
│   │   ├── index.ts               # formatTime, formatNumber, etc.
│   │   └── ...
│   ├── styles/                    # CSS
│   │   ├── main.css               # *** THEME VARIABLES + GRID LAYOUT ***
│   │   ├── panels.css             # Panel-specific styles (tabs, rows, badges)
│   │   ├── base-layer.css         # Foundation layer
│   │   └── ...
│   └── generated/                 # Auto-generated from .proto files
│       ├── client/                # TypeScript API clients
│       └── server/                # TypeScript API handlers
├── api/                           # Vercel Edge Functions (60+)
│   ├── _cors.js                   # CORS origin allowlist
│   ├── _rate-limit.js             # Upstash Redis rate limiting
│   ├── _api-key.js                # API key injection
│   ├── bootstrap.js               # Pre-fetch 15 Redis keys for first render
│   ├── rss-proxy.js               # RSS feed proxy with domain allowlist
│   ├── market/v1/[rpc].ts         # Market data endpoints (proto-first)
│   ├── news/v1/[rpc].ts           # News endpoints
│   └── ...                        # 22 service domains
├── server/                        # Railway relay server
├── proto/                         # Protocol Buffer definitions
└── tests/                         # 30 test files, 554 test cases
```

## Core Pattern: Panel Base Class

**File**: `src/components/Panel.ts` (~1000 lines)

The Panel class is the foundation for ALL UI components (80+ panels inherit from it).

### Constructor Options

```typescript
interface PanelOptions {
  id: string;          // Unique panel identifier (used in DOM data-panel attr)
  title: string;       // Display title in header
  showCount?: boolean; // Show item count badge
  className?: string;  // Additional CSS class (e.g., 'panel-wide')
  trackActivity?: boolean; // Enable activity tracking
  infoTooltip?: string;    // Hover tooltip text
  premium?: 'locked' | 'enhanced'; // Pro feature gating
}
```

### DOM Structure

```
div.panel[data-panel="{id}"]
├── div.panel-header
│   ├── div.panel-header-left
│   │   ├── span.panel-title
│   │   └── span.panel-new-badge (optional)
│   ├── span.panel-data-badge (optional, "LIVE"/"CACHED")
│   └── span.panel-count (optional)
├── div.panel-content
│   └── (loading spinner | error state | rendered content)
└── div.panel-resize-handle
```

### Key Methods

| Method | Purpose |
|--------|---------|
| `getElement()` | Returns the outer `div.panel` |
| `showLoading(msg?)` | Display spinner + message |
| `showError(msg, onRetry?)` | Display error with optional retry button |
| `setContent(html)` | Set content innerHTML (debounced 150ms) |
| `setContentDirect(html)` | Set content immediately (no debounce) |
| `setCount(n)` | Update the count badge |
| `setStatusBadge(label, type)` | Show "LIVE"/"CACHED" badge |
| `show() / hide()` | Toggle visibility |
| `destroy()` | Remove element, abort controller, cleanup listeners |

### Resize System

Panels support both row span (1-4 rows) and column span (1-3 cols) resizing via drag handles. Sizes are persisted in `localStorage` with keys `worldmonitor-panel-spans` and `worldmonitor-panel-col-spans`.

## Core Pattern: Data Service Layer

**Directory**: `src/services/` (117 files)

### SmartPollLoop

**File**: `src/services/runtime.ts`

Instead of raw `setInterval`, WorldMonitor uses `startSmartPollLoop()`:

```typescript
const loop = startSmartPollLoop(async () => {
  // fetch data, return boolean for success/failure
}, intervalMs);
loop.stop(); // cleanup
```

Features:
- Exponential backoff on failure (1x → 2x → 4x, capped at 8x)
- Hidden-tab throttle (pauses when `document.hidden`)
- Circuit breaker integration (stops after N consecutive failures)
- Adaptive refresh (can adjust interval based on data freshness)

### Service Module Convention

Each service is a module (not a class) exporting async functions:

```typescript
// src/services/market/index.ts
export async function fetchMarketQuotes(symbols: string[]): Promise<MarketQuote[]> { ... }
export async function fetchMarketNews(): Promise<MarketNews[]> { ... }
```

Services call `/api/...` proxy endpoints, never external APIs directly.

## Core Pattern: CSS Theme System

**File**: `src/styles/main.css` (~17,000 lines)

### CSS Custom Properties (Dark Theme)

```css
:root {
  /* Backgrounds */
  --bg: #0a0a0a;
  --bg-secondary: #111;
  --surface: #141414;
  --surface-hover: #1e1e1e;

  /* Borders */
  --border: #2a2a2a;
  --border-strong: #444;
  --border-subtle: #1a1a1a;

  /* Text */
  --text: #e8e8e8;
  --text-secondary: #ccc;
  --text-dim: #888;
  --text-muted: #666;
  --accent: #fff;

  /* Semantic Colors */
  --semantic-critical: #ff4444;
  --semantic-high: #ff8800;
  --semantic-elevated: #ffaa00;
  --semantic-normal: #44aa44;
  --semantic-info: #3b82f6;

  /* Status */
  --green: #44ff88;
  --red: #ff4444;
  --yellow: #ffaa00;

  /* Font Stack */
  --font-mono: 'SF Mono', 'Monaco', 'Cascadia Code', 'Fira Code', monospace;
  --font-body: var(--font-mono);
}
```

### Grid Layout

```css
.panels-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  grid-auto-flow: row dense;
  grid-auto-rows: minmax(200px, 380px);
  gap: 4px;
  padding: 4px;
}
```

### Panel Base Styles

```css
.panel {
  background: var(--surface);
  border: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 10px;
  background: var(--overlay-subtle);
  border-bottom: 1px solid var(--border);
}

.panel-title {
  font-size: 11px;
  font-weight: 700;
  color: var(--accent);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.panel-content {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
  font-size: 12px;
}
```

## Core Pattern: API Proxy Layer

**Directory**: `api/` (60+ Vercel Edge Functions)

### CORS Helper (`api/_cors.js`)

Origin allowlist pattern — only approved origins can call API endpoints:

```javascript
const ALLOWED_ORIGIN_PATTERNS = [
  /^https:\/\/(.*\.)?worldmonitor\.app$/,
  /^https?:\/\/localhost(:\d+)?$/,
];

export function getCorsHeaders(req, methods = 'GET, OPTIONS') {
  const origin = req.headers.get('origin') || '';
  const allowOrigin = isAllowedOrigin(origin) ? origin : 'https://worldmonitor.app';
  return {
    'Access-Control-Allow-Origin': allowOrigin,
    'Access-Control-Allow-Methods': methods,
    'Access-Control-Allow-Headers': 'Content-Type',
  };
}
```

### Edge Function Pattern

Each endpoint file follows this structure:
1. CORS preflight handling
2. Input validation
3. API key from `process.env`
4. Upstream fetch with error wrapping
5. `Cache-Control` headers for CDN caching
6. Structured JSON response

## Core Pattern: Refresh Scheduler

**File**: `src/app/refresh-scheduler.ts`

```typescript
interface RefreshRegistration {
  name: string;
  fn: () => Promise<boolean | void>;
  intervalMs: number;
  condition?: () => boolean;   // Skip refresh if false
}

class RefreshScheduler {
  scheduleRefresh(name, fn, intervalMs, condition?): void
  flushStaleRefreshes(hiddenMs): void   // Staggered 150ms between each
  destroy(): void
}
```

Key behaviors:
- Each panel registers with a unique name and interval
- Visibility-aware: pauses when tab hidden, flushes stale on return
- In-flight guard: `ctx.inFlight` set prevents duplicate refreshes
- Stagger on resume: 150ms gap between each stale refresh to avoid API burst

## Core Pattern: DOM Utilities

**File**: `src/utils/dom-utils.ts`

WorldMonitor uses a lightweight JSX-free helper `h()`:

```typescript
function h(tag: string, propsOrChild?, ...children): HTMLElement
function fragment(...children): DocumentFragment
function replaceChildren(el: Element, ...children): void
function rawHtml(html: string): DocumentFragment
function safeHtml(html: string): string  // XSS-safe innerHTML
```

Example usage:
```typescript
const row = h('div', { className: 'stock-row' },
  h('span', { className: 'symbol' }, 'AAPL'),
  h('span', { className: 'price' }, '$178.72'),
);
```

## Adaptation Notes for Personal Monitor

When adapting WorldMonitor patterns for my-daily-monitor:

| WorldMonitor Feature | Personal Monitor Adaptation |
|---------------------|----------------------------|
| 80+ panel classes | 15-20 panels (email, feishu, office, social, etc.) |
| Proto-first API | Simple REST endpoints (no proto needed) |
| Vercel Edge Functions | Vite dev server plugin or simple Express routes |
| SmartPollLoop | Simplified RefreshScheduler with setInterval |
| VirtualList | Not needed until panel has 100+ items |
| Multi-variant (5 sites) | Single variant only |
| 21 languages (i18n) | Chinese + English only |
| Tauri desktop app | Web-only (may add later) |
| Redis caching | In-memory cache only |

## Key Files to Study

When extracting a specific pattern, start with these files:

| Pattern | Read First | Then Read |
|---------|-----------|-----------|
| Panel architecture | `src/components/Panel.ts` | Any concrete panel (e.g., `NewsPanel.ts`) |
| Service layer | `src/services/runtime.ts` | `src/services/market/index.ts` |
| CSS theme | `src/styles/main.css` (first 60 lines) | `src/styles/panels.css` |
| API proxy | `api/_cors.js` | Any `api/*/v1/[rpc].ts` |
| Refresh | `src/app/refresh-scheduler.ts` | `src/app/event-handlers.ts` |
| Grid layout | `src/styles/main.css` (search `.panels-grid`) | `src/app/panel-layout.ts` |
| DOM helpers | `src/utils/dom-utils.ts` | `src/utils/sanitize.ts` |
| Entry point | `src/main.ts` | `src/App.ts` |

