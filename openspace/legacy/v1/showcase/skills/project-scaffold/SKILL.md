---
name: project-scaffold
description: Scaffold a Vite + TypeScript personal dashboard project with the correct directory structure, dependencies, and configuration files.
---

# Project Scaffold

Set up a new **Vite + TypeScript** project for a personal daily monitoring dashboard.

## Directory Structure

```
my-daily-monitor/
├── index.html
├── package.json
├── tsconfig.json
├── vite.config.ts
├── api/                      # Serverless API proxy endpoints (Vercel)
│   └── stocks.ts
├── src/
│   ├── main.ts               # Entry point
│   ├── components/            # Panel components
│   │   ├── Panel.ts           # Base panel class
│   │   ├── StockPanel.ts
│   │   ├── NewsPanel.ts
│   │   └── index.ts
│   ├── services/              # Data fetching services
│   │   ├── stock-market.ts
│   │   ├── news.ts
│   │   └── index.ts
│   ├── utils/                 # Utilities
│   │   ├── circuit-breaker.ts
│   │   ├── sparkline.ts
│   │   ├── format.ts
│   │   └── index.ts
│   ├── config/                # Panel config, API endpoints
│   │   └── panels.ts
│   └── styles/
│       └── main.css
└── skills/                    # OpenSpace skill files
```

## package.json

```json
{
  "name": "my-daily-monitor",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc && vite build",
    "preview": "vite preview"
  },
  "devDependencies": {
    "typescript": "^5.7.2",
    "vite": "^6.0.7"
  }
}
```

## tsconfig.json

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "ESNext",
    "moduleResolution": "bundler",
    "strict": true,
    "jsx": "preserve",
    "resolveJsonModule": true,
    "isolatedModules": true,
    "esModuleInterop": true,
    "lib": ["ES2022", "DOM", "DOM.Iterable"],
    "skipLibCheck": true,
    "noEmit": true,
    "paths": {
      "@/*": ["./src/*"]
    },
    "baseUrl": "."
  },
  "include": ["src"]
}
```

## vite.config.ts

```typescript
import { defineConfig } from 'vite';
import { resolve } from 'path';

export default defineConfig({
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
    },
  },
  server: {
    port: 5173,
  },
});
```

## index.html

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>My Daily Monitor</title>
  <link rel="stylesheet" href="/src/styles/main.css" />
</head>
<body>
  <div id="app">
    <header class="app-header">
      <h1>My Daily Monitor</h1>
      <span class="app-clock" id="headerClock"></span>
    </header>
    <main class="main-content">
      <div class="panels-grid" id="panelsGrid"></div>
    </main>
  </div>
  <script type="module" src="/src/main.ts"></script>
</body>
</html>
```

## src/main.ts (Entry Point)

```typescript
import './styles/main.css';
// Import panels
import { StockPanel } from './components/StockPanel';
import { NewsPanel } from './components/NewsPanel';
// ... other panels

const grid = document.getElementById('panelsGrid')!;

// Instantiate and mount panels
const panels = [
  new StockPanel(),
  new NewsPanel(),
  // ... more panels
];

for (const panel of panels) {
  grid.appendChild(panel.getElement());
}

// Header clock
function updateClock(): void {
  const el = document.getElementById('headerClock');
  if (el) el.textContent = new Date().toLocaleTimeString();
}
updateClock();
setInterval(updateClock, 1000);
```

## Key Conventions

1. **No framework** — vanilla TypeScript with DOM API
2. **Each panel** is a class in `src/components/`
3. **Each service** is a module in `src/services/`
4. **CSS** uses custom properties for theming
5. **Vite** handles bundling, HMR, and path aliases
6. **API proxy** endpoints go in `api/` for serverless deployment
7. Panel config in `src/config/panels.ts` controls which panels are enabled

