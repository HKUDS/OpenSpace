---
name: panel-grid-layout
description: CSS grid layout system for a responsive panel dashboard with dark theme. Uses CSS custom properties, auto-fill grid, and monospace font aesthetic inspired by worldmonitor.
---

# Panel Grid Layout & Dark Theme

## CSS Variables (Dark Theme)

Create `src/styles/main.css` with these CSS custom properties:

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
  --text-faint: #555;
  --accent: #fff;

  /* Overlays */
  --overlay-subtle: rgba(255, 255, 255, 0.03);
  --overlay-light: rgba(255, 255, 255, 0.05);

  /* Status & Semantic Colors */
  --green: #44ff88;
  --red: #ff4444;
  --yellow: #ffaa00;
  --semantic-critical: #ff4444;
  --semantic-high: #ff8800;
  --semantic-positive: #44ff88;
  --semantic-info: #3b82f6;

  /* Status indicators */
  --status-live: #44ff88;
  --status-cached: #ffaa00;
  --status-unavailable: #ff4444;

  /* Font */
  --font-mono: 'SF Mono', 'Monaco', 'Cascadia Code', 'Fira Code', monospace;
  --font-body: var(--font-mono);
}
```

## Base Styles

```css
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

html, body {
  height: 100%;
  background: var(--bg);
  color: var(--text);
  font-family: var(--font-body);
  font-size: 13px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}

/* Scrollbar */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: #333; border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: #555; }
```

## Grid Layout

```css
.panels-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  grid-auto-flow: row dense;
  grid-auto-rows: minmax(200px, 380px);
  gap: 4px;
  padding: 4px;
  align-content: start;
  align-items: stretch;
  min-height: 0;
}
```

## Panel Base Styles

```css
.panel {
  background: var(--surface);
  border: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  height: 100%;
  min-height: 200px;
  min-width: 0;
  transition: transform 0.15s, box-shadow 0.15s;
  position: relative;
}

.panel.hidden { display: none; }

.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 10px;
  background: var(--overlay-subtle);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}

.panel-header-left {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.panel-title {
  font-size: 11px;
  font-weight: 700;
  color: var(--accent);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.panel-count {
  font-size: 10px;
  background: var(--overlay-light);
  color: var(--text-dim);
  padding: 1px 6px;
  border-radius: 8px;
  font-variant-numeric: tabular-nums;
}

.panel-content {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
  font-size: 12px;
}

.panel-content::-webkit-scrollbar { width: 4px; }
.panel-content::-webkit-scrollbar-track { background: transparent; }
.panel-content::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }
```

## Loading & Error States

```css
.panel-loading {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 32px 16px;
  min-height: 120px;
}

.panel-loading-spinner {
  width: 32px;
  height: 32px;
  border: 2px solid var(--border);
  border-top-color: var(--green);
  border-radius: 50%;
  animation: spin 1s linear infinite;
  margin-bottom: 12px;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.panel-loading-text {
  font-size: 12px;
  color: var(--accent);
  letter-spacing: 0.5px;
}

.panel-error-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 1.5rem 1rem;
  min-height: 120px;
}

.panel-error-msg {
  color: var(--text-dim);
  font-size: 11px;
}

.panel-retry-btn {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--text-dim);
  padding: 4px 12px;
  font-size: 10px;
  cursor: pointer;
  border-radius: 3px;
}

.panel-retry-btn:hover {
  border-color: var(--accent);
  color: var(--accent);
}
```

## Row/Item Patterns (reusable across panels)

```css
/* Generic list row */
.item-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-bottom: 1px solid var(--border-subtle);
  transition: background 0.1s;
}
.item-row:hover { background: var(--surface-hover); }
.item-row:last-child { border-bottom: none; }

/* Positive/Negative change indicators */
.positive { color: var(--green); }
.negative { color: var(--red); }

/* Badge */
.badge {
  font-size: 9px;
  font-weight: 700;
  padding: 2px 6px;
  border-radius: 3px;
  letter-spacing: 0.5px;
}
.badge-live { background: rgba(68, 255, 136, 0.15); color: var(--green); }
.badge-warn { background: rgba(255, 170, 0, 0.15); color: var(--yellow); }
.badge-error { background: rgba(255, 68, 68, 0.15); color: var(--red); }

/* Tabular numbers for all numeric content */
.num { font-variant-numeric: tabular-nums; }
```

## Responsive Breakpoints

```css
@media (max-width: 768px) {
  .panels-grid {
    grid-template-columns: 1fr;
    grid-auto-rows: minmax(180px, auto);
  }
}

@media (min-width: 1600px) {
  .panels-grid {
    grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  }
}
```

## App Layout

```css
#app {
  display: flex;
  flex-direction: column;
  height: 100vh;
}

.app-header {
  display: flex;
  align-items: center;
  padding: 8px 12px;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}

.app-header h1 {
  font-size: 14px;
  font-weight: 700;
  color: var(--green);
  letter-spacing: 1px;
  text-transform: uppercase;
}

.main-content {
  flex: 1;
  overflow-y: auto;
}
```

