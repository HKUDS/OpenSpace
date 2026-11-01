---
name: create-full-stack-panel-feature
description: Multi-layer workflow for creating complete panel features by analyzing existing patterns and generating coordinated server routes, services, UI components, registration, and styling files.
---

# Create Full-Stack Panel Feature

This skill guides you through creating a complete, pattern-consistent panel feature across all application layers: backend API routes, service logic, frontend components, registration, and styling.

## When to Use

- Adding a new panel type to a dashboard or admin interface
- Creating a feature that spans multiple architectural layers
- Ensuring consistency with existing codebase patterns across the stack

## Workflow Steps

### 1. Identify Pattern Files

Locate existing examples for each layer you need to implement:

```bash
# Find existing server routes
find . -name "*route*.js" -o -name "*routes*.js" | grep -E "(panel|dashboard)"

# Find service files
find . -name "*service*.js" | grep -E "(panel|dashboard)"

# Find UI components
find . -name "*.jsx" -o -name "*.tsx" | grep -i panel

# Find registration/config files
find . -name "*config*.js" -o -name "*registry*.js"

# Find styling files
find . -name "*.css" -o -name "*.scss" | grep -i panel
```

### 2. Read and Analyze Patterns

Read 1-2 representative examples from **each layer** to understand:

- Naming conventions (e.g., `MetricsPanel`, `metrics-service.js`)
- Code structure and organization
- Import/export patterns
- Registration mechanisms
- API endpoint patterns
- Error handling approaches
- Styling conventions

**Example analysis checklist:**

```
Server Route Pattern:
- ✓ Endpoint naming: /api/panels/{type}
- ✓ Authentication middleware
- ✓ Response format: { success, data, error }

Service Pattern:
- ✓ Export structure: class vs functions
- ✓ Data transformation logic
- ✓ Error propagation

Component Pattern:
- ✓ Props interface
- ✓ State management (hooks/class)
- ✓ Data fetching approach
- ✓ Loading/error states

Registration Pattern:
- ✓ Registry file location
- ✓ Registration format (array/object)
- ✓ Required metadata fields
```

### 3. Plan Your Feature Files

Based on patterns, list the files you need to create:

**Typical full-stack panel structure:**
1. **Server route** (e.g., `server/routes/system-health-route.js`)
2. **Service layer** (e.g., `server/services/system-health-service.js`)
3. **UI component** (e.g., `client/components/SystemHealthPanel.jsx`)
4. **Panel registration** (e.g., update `client/config/panel-registry.js`)
5. **Styling** (e.g., `client/styles/system-health-panel.css`)
6. **Type definitions** (if TypeScript, e.g., `types/system-health.ts`)

### 4. Create Files in Order

### 3b. Detect Project Framework

**Before selecting templates in Step 4, determine the frontend framework in use:**

```bash
# Check for React
grep -s "react" package.json | head -5

# Check for TypeScript without React (vanilla TS)
ls client/components/*.ts 2>/dev/null | head -3
ls client/panels/*.ts 2>/dev/null | head -3

# Check for a Panel base class (common in vanilla TS dashboards)
grep -r "class.*Panel" client/ --include="*.ts" -l | head -3
grep -r "extends Panel" client/ --include="*.ts" -l | head -3
```

**Decision rule:**
- If `react` or `react-dom` is present in `package.json` → use the **React/JSX template** (Step 4C-React).
- If components are `.ts` files extending a `Panel` base class → use the **Vanilla TS template** (Step 4C-Vanilla).
- When in doubt, read one or two existing component files to confirm before proceeding.

### 4. Create Files in Order

Generate files in dependency order (backend → frontend → registration):

#### A. Server Route

```javascript
// server/routes/{feature}-route.js
const express = require('express');
const router = express.Router();
const featureService = require('../services/{feature}-service');

router.get('/api/panels/{feature}', async (req, res) => {
  try {
    const data = await featureService.getData();
    res.json({ success: true, data });
  } catch (error) {
    res.status(500).json({ success: false, error: error.message });
  }
});

module.exports = router;
```

#### B. Service Layer

```javascript
// server/services/{feature}-service.js
class FeatureService {
  async getData() {
    // Implementation following existing service patterns
    // - Data fetching
    // - Business logic
    // - Data transformation
    return processedData;
  }
}

module.exports = new FeatureService();
```

#### C-React. UI Component (React/JSX projects)

*Use this template only when React is confirmed in Step 3b.*

```javascript
// client/components/{Feature}Panel.jsx
import React, { useState, useEffect } from 'react';
import './styles/{feature}-panel.css';

const FeaturePanel = () => {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    fetch('/api/panels/{feature}')
      .then(res => res.json())
      .then(result => {
        setData(result.data);
        setLoading(false);
      })
      .catch(err => {
        setError(err.message);
        setLoading(false);
      });
  }, []);

  if (loading) return <div>Loading...</div>;
  if (error) return <div>Error: {error}</div>;

  return (
    <div className="{feature}-panel">
      {/* Render data following UI patterns */}
    </div>
  );
};

export default FeaturePanel;
```

#### C-Vanilla. UI Component (Vanilla TypeScript / Panel base-class projects)

*Use this template when components extend a Panel base class (no React).*

```typescript
// client/panels/{Feature}Panel.ts
import { Panel } from '../core/Panel';

export class FeaturePanel extends Panel {
  private intervalId: number | null = null;

  constructor(id: string) {
    super(id);
    this.setTitle('Feature Display Name');
  }

  async onLoad(): Promise<void> {
    if (this.isFetching) return;
    this.isFetching = true;
    try {
      const res = await fetch('/api/panels/{feature}', {
        headers: { Authorization: `Bearer ${localStorage.getItem('token') ?? ''}` }
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const { data } = await res.json();
      this.render(data);
    } catch (err) {
      this.showError(err instanceof Error ? err.message : String(err), () => this.onLoad());
    } finally {
      this.isFetching = false;
    }
  }

  private render(data: unknown): void {
    const container = document.createElement('div');
    container.className = '{feature}-panel';
    // Build DOM nodes from data instead of JSX:
    // const item = document.createElement('p');
    // item.textContent = String(data);
    // container.appendChild(item);
    this.setContent(container);
  }

  // Call this to start polling (optional)
  startPolling(intervalMs = 60_000): void {
    this.onLoad();
    this.intervalId = window.setInterval(() => this.onLoad(), intervalMs);
  }

  destroy(): void {
    if (this.intervalId !== null) clearInterval(this.intervalId);
    super.destroy();
  }
}
```

**Key differences from React template:**
- Extends `Panel` base class; uses `setContent()` / `showError()` / `setTitle()` / `setCount()` instead of React state.
- DOM construction via `document.createElement` / `innerHTML` instead of JSX.
- No `useState` / `useEffect`; lifecycle is `onLoad()` + `destroy()`.
- File extension is `.ts`, not `.jsx`/`.tsx`.
- Registration passes a factory function or class constructor, not a JSX component.

#### D. Panel Registration

**React projects** — import the component directly:

```javascript
// client/config/panel-registry.js (update existing)
import FeaturePanel from '../components/FeaturePanel';

export const panels = [
  // ... existing panels
  {
    id: '{feature}',
    name: 'Feature Display Name',
    component: FeaturePanel,
    icon: 'icon-name',
    category: 'appropriate-category'
  }
];
```

**Vanilla TS projects** — register via factory:

```typescript
// client/config/panel-registry.ts (update existing)
import { FeaturePanel } from '../panels/FeaturePanel';

export const panels = [
  // ... existing panels
  {
    id: '{feature}',
    name: 'Feature Display Name',
    factory: (id: string) => new FeaturePanel(id),
    icon: 'icon-name',
    category: 'appropriate-category'
  }
];
```

#### E. Styling

```css
/* client/styles/{feature}-panel.css */
.{feature}-panel {
  /* Follow existing panel styling conventions */
  padding: 1rem;
  border-radius: 4px;
}

.{feature}-panel__header {
  /* Consistent header styling */
}
```

### 5. Verify Pattern Consistency

After creating all files, check:

- [ ] Naming follows project conventions across all layers
- [ ] Import/export statements are consistent
- [ ] Error handling matches existing patterns
- [ ] API response format is uniform
- [ ] Component structure mirrors other panels
- [ ] Registration metadata is complete
- [ ] CSS class naming follows BEM or project convention

### 6. Test Integration Points

Verify that:

```bash
# Server route is registered
grep -r "require.*{feature}-route" server/

# Component is imported in registry
grep -r "import.*{Feature}Panel" client/config/

# Styles are imported
grep -r "import.*{feature}-panel.css" client/
```

## Key Principles

1. **Pattern before implementation**: Always read existing code first
2. **Maintain consistency**: Match naming, structure, and style exactly
3. **Complete the stack**: Don't leave layers incomplete
4. **Follow the chain**: Backend → Service → Frontend → Registration → Styling
5. **Verify integration**: Ensure all pieces connect properly

## Common Pitfalls

- Creating component before verifying API endpoint works
- Inconsistent naming across layers (camelCase vs kebab-case)
- Forgetting to register the panel in the appropriate config
- Missing error handling in any layer
- Skipping styling, leading to broken UI

## Variations

**For simple features**: May omit service layer if route logic is trivial

**For complex features**: May need additional files:
- Database migrations/models
- Redux actions/reducers (if using Redux)
- Test files for each layer
- Documentation files

**For TypeScript projects**: Add `.d.ts` or `.ts` type definition files
