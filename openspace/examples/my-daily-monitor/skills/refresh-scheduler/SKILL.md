---
name: refresh-scheduler
description: Manage timed data refresh intervals for all dashboard panels. Handles pause-when-hidden, stale-data flush on visibility restore, and staggered refresh to avoid API bursts.
---

# Refresh Scheduler Pattern

The refresh scheduler manages periodic data fetching for all panels, pausing when the tab is hidden and flushing stale data when the user returns.

## Implementation

Create `src/services/refresh-scheduler.ts`:

```typescript
export interface RefreshRegistration {
  name: string;
  fn: () => Promise<void>;
  intervalMs: number;
  condition?: () => boolean;
}

interface RunnerEntry {
  timer: ReturnType<typeof setInterval> | null;
  intervalMs: number;
  fn: () => Promise<void>;
  condition?: () => boolean;
  lastRun: number;
}

export class RefreshScheduler {
  private runners = new Map<string, RunnerEntry>();
  private inFlight = new Set<string>();
  private hiddenSince = 0;
  private visibilityHandler: (() => void) | null = null;

  constructor() {
    this.visibilityHandler = () => this.onVisibilityChange();
    document.addEventListener('visibilitychange', this.visibilityHandler);
  }

  private onVisibilityChange(): void {
    if (document.hidden) {
      this.hiddenSince = Date.now();
      // Pause all timers
      for (const entry of this.runners.values()) {
        if (entry.timer) {
          clearInterval(entry.timer);
          entry.timer = null;
        }
      }
    } else {
      // Resume and flush stale
      this.flushStaleRefreshes();
      for (const [name, entry] of this.runners) {
        if (!entry.timer) {
          entry.timer = setInterval(() => this.runRefresh(name), entry.intervalMs);
        }
      }
    }
  }

  private async runRefresh(name: string): Promise<void> {
    const entry = this.runners.get(name);
    if (!entry) return;
    if (this.inFlight.has(name)) return;
    if (entry.condition && !entry.condition()) return;

    this.inFlight.add(name);
    try {
      await entry.fn();
      entry.lastRun = Date.now();
    } catch (err) {
      console.error(`[Refresh] ${name} failed:`, err);
    } finally {
      this.inFlight.delete(name);
    }
  }

  scheduleRefresh(
    name: string,
    fn: () => Promise<void>,
    intervalMs: number,
    condition?: () => boolean,
  ): void {
    // Clean up existing
    const existing = this.runners.get(name);
    if (existing?.timer) clearInterval(existing.timer);

    const entry: RunnerEntry = {
      timer: null,
      intervalMs,
      fn,
      condition,
      lastRun: 0,
    };

    if (!document.hidden) {
      entry.timer = setInterval(() => this.runRefresh(name), intervalMs);
    }

    this.runners.set(name, entry);
  }

  /**
   * After returning from hidden, flush all runners whose data
   * is older than their interval. Stagger to avoid API burst.
   */
  flushStaleRefreshes(): void {
    if (!this.hiddenSince) return;
    const hiddenMs = Date.now() - this.hiddenSince;
    this.hiddenSince = 0;

    let stagger = 0;
    for (const [name, entry] of this.runners) {
      if (hiddenMs < entry.intervalMs) continue;
      const delay = stagger;
      stagger += 150; // 150ms between each flush
      setTimeout(() => this.runRefresh(name), delay);
    }
  }

  registerAll(registrations: RefreshRegistration[]): void {
    for (const reg of registrations) {
      this.scheduleRefresh(reg.name, reg.fn, reg.intervalMs, reg.condition);
    }
  }

  /** Manually trigger a specific refresh immediately */
  trigger(name: string): void {
    this.runRefresh(name);
  }

  destroy(): void {
    if (this.visibilityHandler) {
      document.removeEventListener('visibilitychange', this.visibilityHandler);
    }
    for (const entry of this.runners.values()) {
      if (entry.timer) clearInterval(entry.timer);
    }
    this.runners.clear();
    this.inFlight.clear();
  }
}
```

## Usage

```typescript
// src/main.ts
import { RefreshScheduler } from './services/refresh-scheduler';

const scheduler = new RefreshScheduler();

// Register panel refresh intervals
scheduler.registerAll([
  { name: 'stocks', fn: () => stockPanel.refresh(), intervalMs: 60_000 },
  { name: 'news',   fn: () => newsPanel.refresh(),  intervalMs: 5 * 60_000 },
  { name: 'email',  fn: () => emailPanel.refresh(), intervalMs: 2 * 60_000 },
  { name: 'calendar', fn: () => calendarPanel.refresh(), intervalMs: 5 * 60_000 },
]);
```

## Key Behaviors

1. **Pause when hidden** — stops all intervals when tab is not visible
2. **Flush on return** — refreshes stale panels with 150ms stagger to avoid API burst
3. **In-flight guard** — prevents duplicate requests for the same panel
4. **Configurable intervals** — each panel has its own refresh rate
5. **Condition guard** — optional condition function to skip refresh (e.g. panel hidden)

