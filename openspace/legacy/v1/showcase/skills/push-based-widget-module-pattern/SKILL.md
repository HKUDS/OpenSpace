---
name: push-based-widget-module-pattern
description: How to implement sidebar/widget UI components that receive pushed data updates from a caller, using a singleton module pattern with exported factory and update functions plus module-level DOM refs for efficient partial re-renders.
---

# Push-Based Widget Module Pattern

Use this pattern when a UI sidebar or widget component needs to be updated by its **caller** (push-based) rather than fetching data itself. Instead of a class or framework component, implement the widget as a plain module with singleton state, a factory function to create the DOM, and an update function to efficiently patch it.

---

## When to Use

- A sidebar, panel, or widget is rendered once but updated frequently with new data.
- The data source lives outside the component (e.g., a parent controller, event bus, or store).
- You want lightweight partial re-renders without rebuilding the entire DOM tree.
- You prefer a simple module interface over a class-based or framework-based component.

---

## Core Structure

```
widgets/
  MyWidget.ts          # The module (singleton state + exports)
  MyWidget.types.ts    # (Optional) Exported data interface
```

---

## Step-by-Step Instructions

### 1. Define the Data Interface

Export a typed interface so callers know exactly what data to push.

```typescript
// MyWidget.types.ts  (or inline at top of MyWidget.ts)
export interface MyWidgetData {
  title: string;
  items: string[];
  status: "idle" | "active" | "error";
  // ... all fields the widget needs to render
}
```

### 2. Declare Module-Level Singleton Refs

At module scope, keep references to the **container** and any **frequently updated child nodes**. This avoids querying the DOM on every update.

```typescript
// MyWidget.ts
import type { MyWidgetData } from "./MyWidget.types";

// Singleton DOM refs
let containerEl: HTMLElement | null = null;
let titleEl: HTMLElement | null = null;
let itemListEl: HTMLElement | null = null;
let statusEl: HTMLElement | null = null;

// Current state snapshot (optional — useful for diffing)
let currentData: MyWidgetData | null = null;
```

### 3. Export a Factory Function (`createMyWidget`)

The factory builds the full DOM tree **once**, stores refs to mutable nodes, and returns the root element to be mounted by the caller.

```typescript
export function createMyWidget(): HTMLElement {
  // Build the container
  containerEl = document.createElement("aside");
  containerEl.className = "my-widget";

  // Build static structure; keep refs to dynamic parts
  titleEl = document.createElement("h2");
  titleEl.className = "my-widget__title";

  itemListEl = document.createElement("ul");
  itemListEl.className = "my-widget__list";

  statusEl = document.createElement("span");
  statusEl.className = "my-widget__status";

  // Compose the tree
  const header = document.createElement("header");
  header.appendChild(titleEl);
  header.appendChild(statusEl);

  containerEl.appendChild(header);
  containerEl.appendChild(itemListEl);

  return containerEl;
}
```

> **Rule:** `createMyWidget` should be callable only once per page. If called again, consider returning the existing `containerEl` (idempotent factory).

### 4. Export an Update Function (`updateMyWidget`)

The update function receives the new data object and performs **surgical DOM mutations** on the stored refs. It does NOT rebuild the whole tree.

```typescript
export function updateMyWidget(data: MyWidgetData): void {
  if (!containerEl) {
    console.warn("updateMyWidget called before createMyWidget");
    return;
  }

  currentData = data;

  // Update only what changed
  if (titleEl) titleEl.textContent = data.title;
  if (statusEl) {
    statusEl.textContent = data.status;
    statusEl.dataset.status = data.status; // for CSS hooks
  }

  // Rebuild only dynamic list items
  if (itemListEl) {
    itemListEl.innerHTML = ""; // clear previous items
    data.items.forEach((item) => {
      const li = document.createElement("li");
      li.textContent = item;
      itemListEl!.appendChild(li);
    });
  }
}
```

> **Optimization tip:** For large lists, compare `currentData` to `data` before clearing and rebuilding, or use a keyed reconciliation approach.

### 5. Caller Usage Pattern

The caller mounts the widget once and pushes updates whenever its data changes.

```typescript
import { createMyWidget, updateMyWidget } from "./widgets/MyWidget";
import type { MyWidgetData } from "./widgets/MyWidget.types";

// Mount once
const sidebar = document.getElementById("sidebar")!;
sidebar.appendChild(createMyWidget());

// Push updates (e.g., from a store subscription, WebSocket, timer)
function onDataChange(newData: MyWidgetData) {
  updateMyWidget(newData);
}

// Example: push an initial state immediately
onDataChange({
  title: "Today's Focus",
  items: ["Task A", "Task B"],
  status: "active",
});
```

---

## Full Module Template

```typescript
// widgets/MyWidget.ts

export interface MyWidgetData {
  // Define all fields here
}

// --- Singleton state ---
let containerEl: HTMLElement | null = null;
// Add more refs as needed per section of the widget

// --- Factory ---
export function createMyWidget(): HTMLElement {
  if (containerEl) return containerEl; // idempotent

  containerEl = document.createElement("aside");
  // ... build DOM, store refs ...
  return containerEl;
}

// --- Update ---
export function updateMyWidget(data: MyWidgetData): void {
  if (!containerEl) return;
  // ... patch DOM refs with new data ...
}
```

---

## Guidelines & Best Practices

| Concern | Recommendation |
|---|---|
| **Idempotent factory** | Return existing `containerEl` if called twice to avoid duplicate mounts. |
| **Null guards** | Always check refs before mutating — the update fn may be called before mount in some flows. |
| **Granular refs** | Store refs for every section that updates independently. Avoid `querySelector` in the update path. |
| **Interface exports** | Always export the data interface so callers can type their push calls. |
| **No internal fetching** | The module must never fetch or subscribe to external data — that is the caller's responsibility. |
| **CSS hooks** | Use `data-*` attributes or CSS classes on refs to let stylesheets react to state changes. |
| **Cleanup** | Optionally export a `destroyMyWidget()` that nulls all refs for SPA teardown scenarios. |

---

## Anti-Patterns to Avoid

- ❌ **Class with constructor** — adds instantiation overhead and prevents the clean factory/update split.
- ❌ **Querying the DOM in the update function** (`document.querySelector(...)`) — slow and fragile.
- ❌ **Rebuilding the entire tree on every update** — defeats the purpose of the module-level refs.
- ❌ **Mixing data-fetching into the widget module** — breaks the push-based contract.