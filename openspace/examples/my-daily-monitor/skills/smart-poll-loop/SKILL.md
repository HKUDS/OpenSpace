---
name: smart-poll-loop
description: Adaptive polling pattern with exponential backoff on failure, automatic recovery on success, and visibility-aware scheduling
---

# Smart Poll Loop Pattern

## Overview

The Smart Poll Loop pattern implements resilient, adaptive polling for periodic data refresh operations. It combines exponential backoff on consecutive failures with automatic recovery on success, ensuring system resources are protected during outages while maintaining responsiveness when services are healthy.

This pattern is essential for production applications that need to poll external services or APIs continuously but must gracefully handle intermittent failures, rate limits, or temporary service disruptions without overwhelming the system or degrading user experience.

## Key Patterns Identified

### Pattern 1: Exponential Backoff on Failure

**Purpose**: Prevent overwhelming failing services with repeated requests while giving them time to recover.

**Implementation**: Each consecutive failure doubles the polling interval up to a configurable maximum multiplier. This creates exponentially increasing delays: 1x → 2x → 4x → 8x (default max).

**Key Elements**:
- `consecutiveFailures`: Counter tracking sequential failures
- `currentBackoffMultiplier`: Current delay multiplier (starts at 1)
- `maxBackoffMultiplier`: Upper bound to prevent infinite delays (typically 4-8x)
- Boolean return from callbacks (`true` = success, `false` = failure)

**Code Pattern**:
```typescript
// Track failure state per runner
interface RunnerEntry {
  consecutiveFailures: number;
  currentBackoffMultiplier: number;
  intervalMs: number; // base interval
  // ... other fields
}

// On failure, increase backoff
if (!success) {
  entry.consecutiveFailures++;
  const newMultiplier = Math.min(
    Math.pow(2, entry.consecutiveFailures),
    maxBackoffMultiplier
  );
  entry.currentBackoffMultiplier = newMultiplier;
  
  // Reschedule with new interval
  const effectiveInterval = entry.intervalMs * entry.currentBackoffMultiplier;
  scheduleNextRun(effectiveInterval);
}
```

### Pattern 2: Automatic Recovery on Success

**Purpose**: Immediately restore normal polling frequency when a service recovers, ensuring fresh data flows without unnecessary delay.

**Implementation**: On any successful callback execution, reset both the failure counter and backoff multiplier to their initial states (0 and 1), then reschedule at the base interval.

**Key Elements**:
- Reset logic triggered by `success === true` or no error thrown
- Immediate rescheduling at base interval (no waiting for current timer)
- Logging state transitions for observability

**Code Pattern**:
```typescript
if (success) {
  // Auto-recovery: reset backoff and failure count
  if (entry.consecutiveFailures > 0 || entry.currentBackoffMultiplier > 1) {
    entry.consecutiveFailures = 0;
    entry.currentBackoffMultiplier = 1;
    
    // Immediately reschedule at normal interval
    if (!paused && entry.timer) {
      scheduleNextRun(name, entry); // uses base intervalMs
    }
  }
}
```

### Pattern 3: Failure Detection via Boolean Return or Exception

**Purpose**: Provide flexible signaling for success/failure states, supporting both explicit boolean returns and exception-based error handling.

**Implementation**: Callbacks can return `boolean` (explicit success/failure), `void` (success assumed), or throw exceptions (failure). All three mechanisms trigger the same backoff/recovery logic.

**Key Elements**:
- Type signature: `() => Promise<boolean | void>`
- `true` = explicit success, triggers recovery
- `false` = explicit failure, triggers backoff
- `void`/`undefined` = implicit success (no news is good news)
- Exception thrown = failure, triggers backoff

**Code Pattern**:
```typescript
let success = true;
try {
  const result = await entry.fn();
  
  // Boolean return indicates explicit success/failure
  if (typeof result === 'boolean') {
    success = result;
  }
  // void/undefined is treated as success
  
  if (success) {
    // trigger recovery...
  } else {
    // trigger backoff...
  }
} catch (err) {
  // Exception is also treated as failure
  success = false;
  // trigger backoff...
}
```

### Pattern 4: Visibility-Aware Scheduling

**Purpose**: Pause or slow down polling when the page is hidden to conserve resources, then resume or refresh immediately when visible.

**Implementation**: Listen to `visibilitychange` events. On hide, clear all timers. On show, either flush stale data (if enough time passed) or resume normal polling.

**Key Elements**:
- `document.hidden` state check
- `pauseWhenHidden` flag (full stop vs slower polling)
- `hiddenSince` timestamp for staleness detection
- Staggered flush on resume (prevent request bursts)

**Code Pattern**:
```typescript
private onVisibilityChange(): void {
  if (document.hidden) {
    this.hiddenSince = Date.now();
    // Pause all runners
    for (const entry of this.runners.values()) {
      if (entry.timer) {
        clearInterval(entry.timer);
        entry.timer = null;
      }
    }
  } else {
    // Resume: flush stale refreshes first
    this.flushStaleRefreshes();
    // Then restart timers
    for (const [name, entry] of this.runners) {
      if (!entry.timer) {
        this.scheduleNextRun(name, entry);
      }
    }
  }
}

private flushStaleRefreshes(): void {
  if (!this.hiddenSince) return;
  const hiddenMs = Date.now() - this.hiddenSince;
  this.hiddenSince = 0;

  let stagger = 0;
  for (const [name, entry] of this.runners) {
    const effectiveInterval = entry.intervalMs * entry.currentBackoffMultiplier;
    if (hiddenMs < effectiveInterval) continue; // not stale yet
    
    // Stagger refreshes to avoid thundering herd
    setTimeout(() => this.runRefresh(name), stagger);
    stagger += 150; // 150ms between each
  }
}
```

### Pattern 5: Per-Runner State Management

**Purpose**: Allow multiple independent polling loops with isolated failure tracking and backoff state.

**Implementation**: Use a map keyed by runner name, storing all state (timer, interval, backoff, failures) per entry. Each runner progresses through its own backoff cycle independently.

**Key Elements**:
- `Map<string, RunnerEntry>` for isolated state
- Unique names prevent conflicts
- Per-runner failure counters
- Per-runner backoff multipliers
- In-flight tracking prevents overlapping executions

**Code Pattern**:
```typescript
private runners = new Map<string, RunnerEntry>();
private inFlight = new Set<string>();

scheduleRefresh(
  name: string,
  fn: () => Promise<boolean | void>,
  intervalMs: number,
): void {
  // Each runner gets isolated state
  const entry: RunnerEntry = {
    timer: null,
    intervalMs,
    fn,
    lastRun: 0,
    consecutiveFailures: 0,
    currentBackoffMultiplier: 1,
  };
  
  this.runners.set(name, entry);
  this.scheduleNextRun(name, entry);
}

private async runRefresh(name: string): Promise<void> {
  const entry = this.runners.get(name);
  if (!entry) return;
  
  // Prevent overlapping executions of the same runner
  if (this.inFlight.has(name)) return;
  this.inFlight.add(name);
  
  try {
    // ... execute and handle backoff/recovery
  } finally {
    this.inFlight.delete(name);
  }
}
```

## Complete Code Template

Below is a minimal but complete implementation of the adaptive polling pattern:

```typescript
interface RefreshRegistration {
  name: string;
  fn: () => Promise<boolean | void>;
  intervalMs: number;
  condition?: () => boolean; // optional: skip if returns false
}

interface RunnerEntry {
  timer: ReturnType<typeof setInterval> | null;
  intervalMs: number;
  fn: () => Promise<boolean | void>;
  condition?: () => boolean;
  lastRun: number;
  consecutiveFailures: number;
  currentBackoffMultiplier: number;
}

class AdaptiveScheduler {
  private runners = new Map<string, RunnerEntry>();
  private inFlight = new Set<string>();
  private hiddenSince = 0;
  private readonly maxBackoffMultiplier = 8;

  constructor() {
    document.addEventListener('visibilitychange', () => this.onVisibilityChange());
  }

  private onVisibilityChange(): void {
    if (document.hidden) {
      this.hiddenSince = Date.now();
      for (const entry of this.runners.values()) {
        if (entry.timer) {
          clearInterval(entry.timer);
          entry.timer = null;
        }
      }
    } else {
      this.flushStaleRefreshes();
      for (const [name, entry] of this.runners) {
        if (!entry.timer) {
          this.scheduleNextRun(name, entry);
        }
      }
    }
  }

  private scheduleNextRun(name: string, entry: RunnerEntry): void {
    if (entry.timer) clearInterval(entry.timer);
    
    const effectiveInterval = entry.intervalMs * entry.currentBackoffMultiplier;
    entry.timer = setInterval(() => this.runRefresh(name), effectiveInterval);
  }

  private async runRefresh(name: string): Promise<void> {
    const entry = this.runners.get(name);
    if (!entry) return;
    if (this.inFlight.has(name)) return;
    if (entry.condition && !entry.condition()) return;

    this.inFlight.add(name);
    let success = true;

    try {
      const result = await entry.fn();
      
      if (typeof result === 'boolean') {
        success = result;
      }
      
      entry.lastRun = Date.now();

      if (success) {
        // Auto-recovery
        if (entry.consecutiveFailures > 0 || entry.currentBackoffMultiplier > 1) {
          entry.consecutiveFailures = 0;
          entry.currentBackoffMultiplier = 1;
          if (!document.hidden && entry.timer) {
            this.scheduleNextRun(name, entry);
          }
        }
      } else {
        // Exponential backoff
        entry.consecutiveFailures++;
        const newMultiplier = Math.min(
          Math.pow(2, entry.consecutiveFailures),
          this.maxBackoffMultiplier
        );
        
        if (newMultiplier !== entry.currentBackoffMultiplier) {
          entry.currentBackoffMultiplier = newMultiplier;
          if (!document.hidden && entry.timer) {
            this.scheduleNextRun(name, entry);
          }
        }
      }
    } catch (err) {
      success = false;
      entry.consecutiveFailures++;
      const newMultiplier = Math.min(
        Math.pow(2, entry.consecutiveFailures),
        this.maxBackoffMultiplier
      );
      
      if (newMultiplier !== entry.currentBackoffMultiplier) {
        entry.currentBackoffMultiplier = newMultiplier;
        if (!document.hidden && entry.timer) {
          this.scheduleNextRun(name, entry);
        }
      }
      console.error(`Refresh ${name} failed:`, err);
    } finally {
      this.inFlight.delete(name);
    }
  }

  private flushStaleRefreshes(): void {
    if (!this.hiddenSince) return;
    const hiddenMs = Date.now() - this.hiddenSince;
    this.hiddenSince = 0;

    let stagger = 0;
    for (const [name, entry] of this.runners) {
      const effectiveInterval = entry.intervalMs * entry.currentBackoffMultiplier;
      if (hiddenMs < effectiveInterval) continue;
      
      setTimeout(() => this.runRefresh(name), stagger);
      stagger += 150;
    }
  }

  scheduleRefresh(
    name: string,
    fn: () => Promise<boolean | void>,
    intervalMs: number,
    condition?: () => boolean,
  ): void {
    const existing = this.runners.get(name);
    if (existing?.timer) clearInterval(existing.timer);

    const entry: RunnerEntry = {
      timer: null,
      intervalMs,
      fn,
      condition,
      lastRun: 0,
      consecutiveFailures: 0,
      currentBackoffMultiplier: 1,
    };

    if (!document.hidden) {
      this.scheduleNextRun(name, entry);
    }

    this.runners.set(name, entry);
  }

  trigger(name: string): void {
    this.runRefresh(name);
  }

  destroy(): void {
    for (const entry of this.runners.values()) {
      if (entry.timer) clearInterval(entry.timer);
    }
    this.runners.clear();
    this.inFlight.clear();
  }
}
```

## Usage Examples

### Example 1: Basic Polling with Explicit Success/Failure

```typescript
const scheduler = new AdaptiveScheduler();

// API endpoint that may fail intermittently
scheduler.scheduleRefresh(
  'fetch-stock-prices',
  async () => {
    try {
      const response = await fetch('/api/stocks');
      if (!response.ok) return false; // explicit failure
      
      const data = await response.json();
      updateStockPrices(data);
      return true; // explicit success
    } catch (err) {
      console.error('Stock fetch failed:', err);
      return false; // explicit failure
    }
  },
  30_000 // 30 seconds base interval
);
```

**Behavior**:
- On success: polls every 30s
- After 1 failure: polls every 60s (2x)
- After 2 failures: polls every 120s (4x)
- After 3+ failures: polls every 240s (8x, max)
- On any success: immediately returns to 30s interval

### Example 2: Multiple Independent Runners

```typescript
const scheduler = new AdaptiveScheduler();

// Weather updates - critical, frequent
scheduler.scheduleRefresh(
  'weather',
  async () => {
    const res = await fetch('/api/weather');
    return res.ok; // boolean indicates success/failure
  },
  60_000 // 1 minute
);

// News feed - less critical, slower
scheduler.scheduleRefresh(
  'news',
  async () => {
    const res = await fetch('/api/news');
    if (!res.ok) return false;
    const articles = await res.json();
    updateNewsFeed(articles);
    return true;
  },
  300_000 // 5 minutes
);

// Each runner has independent backoff state
// Weather failures don't affect news polling, and vice versa
```

### Example 3: Conditional Execution

```typescript
scheduler.scheduleRefresh(
  'user-notifications',
  async () => {
    const res = await fetch('/api/notifications');
    if (!res.ok) return false;
    
    const notifications = await res.json();
    displayNotifications(notifications);
    return true;
  },
  60_000,
  // Only poll if user is logged in
  () => isUserLoggedIn()
);
```

### Example 4: Void Return (Implicit Success)

```typescript
scheduler.scheduleRefresh(
  'analytics-heartbeat',
  async () => {
    // No explicit return = void = success assumed
    await fetch('/api/analytics/heartbeat', { method: 'POST' });
    // If this throws, it's treated as failure
    // If it completes, it's success
  },
  120_000 // 2 minutes
);
```

### Example 5: Manual Trigger

```typescript
// Schedule background refresh
scheduler.scheduleRefresh('dashboard-data', fetchDashboard, 60_000);

// User clicks refresh button - trigger immediate run
document.getElementById('refresh-btn')?.addEventListener('click', () => {
  scheduler.trigger('dashboard-data');
});
```

## Best Practices

### Choose Appropriate Base Intervals

- **High-frequency data** (stock prices, live scores): 10-30 seconds
- **Medium-frequency data** (weather, news): 1-5 minutes
- **Low-frequency data** (configuration, settings): 10-30 minutes

### Set Reasonable Backoff Limits

- **Max multiplier 4-8x**: Prevents indefinite delays while allowing sufficient recovery time
- **Too low** (2x): May overwhelm failing services
- **Too high** (16x+): Data may become too stale during recovery

### Use Explicit Boolean Returns When Possible

```typescript
// Good: Explicit failure signaling
async () => {
  const res = await fetch('/api/data');
  if (res.status === 429) return false; // rate limited - back off
  if (res.status === 503) return false; // service unavailable - back off
  if (res.status === 404) return true;  // not found - but don't back off
  return res.ok;
}

// Less ideal: Relying on exceptions
async () => {
  const res = await fetch('/api/data');
  res.json(); // throws on error, but less explicit
}
```

### Handle Stale Data on Resume

When the page becomes visible after being hidden, stale refreshes are flushed with staggering. Ensure your data handlers can cope with rapid updates:

```typescript
// Debounce or batch UI updates
let updateTimer: number | null = null;
function updateUI(data: any) {
  if (updateTimer) clearTimeout(updateTimer);
  updateTimer = setTimeout(() => {
    renderData(data); // actual DOM update
  }, 100);
}
```

### Monitor Backoff State

Expose backoff state for debugging and monitoring:

```typescript
// Check current backoff status
const state = scheduler.getBackoffState('api-poller');
console.log(`Failures: ${state.failures}, Multiplier: ${state.multiplier}x`);

// Manual reset if needed (e.g., after user fixes credentials)
scheduler.resetBackoff('api-poller');
```

### Clean Up on Component Unmount

```typescript
// React example
useEffect(() => {
  const scheduler = new AdaptiveScheduler();
  scheduler.scheduleRefresh('data', fetchData, 30_000);
  
  return () => scheduler.destroy(); // clear all timers
}, []);
```

## When to Use This Pattern

### Ideal For:

- **Polling external APIs** that may experience intermittent failures or rate limits
- **Dashboard/monitoring UIs** that need to stay fresh but must handle service outages gracefully
- **Real-time-ish data** where strict real-time isn't required (use WebSockets for true real-time)
- **Multiple data sources** with different refresh rates and reliability profiles

### Not Ideal For:

- **True real-time requirements**: Use WebSockets, Server-Sent Events, or long polling instead
- **One-time operations**: Use direct async calls, not scheduled polling
- **Critical, must-not-miss updates**: Add push notifications or webhooks as a complement
- **High-frequency sub-second polling**: Consider WebSocket or EventSource

## Common Pitfalls to Avoid

### Thundering Herd on Resume

**Problem**: All runners trigger simultaneously when page becomes visible.

**Solution**: Use staggered flush (already implemented in the pattern):
```typescript
let stagger = 0;
for (const [name, entry] of this.runners) {
  setTimeout(() => this.runRefresh(name), stagger);
  stagger += 150; // stagger by 150ms
}
```

### Backoff Not Resetting

**Problem**: Forgetting to reset backoff on success keeps the service in slow-poll mode forever.

**Solution**: Always check and reset state on success:
```typescript
if (success && entry.currentBackoffMultiplier > 1) {
  entry.currentBackoffMultiplier = 1;
  entry.consecutiveFailures = 0;
  reschedule(); // immediate effect
}
```

### Overlapping Executions

**Problem**: Long-running callbacks overlap with the next scheduled run.

**Solution**: Use in-flight tracking (already implemented):
```typescript
if (this.inFlight.has(name)) return; // skip if already running
this.inFlight.add(name);
try {
  await callback();
} finally {
  this.inFlight.delete(name);
}
```

### Ignoring Visibility State

**Problem**: Polling continues in hidden tabs, wasting resources and battery.

**Solution**: Always implement visibility-aware pausing (already implemented):
```typescript
if (document.hidden) {
  clearAllTimers();
} else {
  restartTimers();
}
```

## Related Patterns

- **Circuit Breaker**: After N consecutive failures, stop polling entirely until manual reset or timeout
- **Jittered Backoff**: Add randomization to backoff delays to prevent synchronized retries across clients
- **Adaptive Intervals**: Adjust base interval based on data change frequency (not just failures)
- **WebSocket with Fallback**: Use WebSocket for real-time, fall back to smart polling on connection loss

## Performance Considerations

### Memory Usage

Each runner entry stores:
- Timer reference: ~8 bytes
- Function reference: ~8 bytes
- State integers: ~16 bytes
- Total: ~32-64 bytes per runner

For 100 concurrent runners: ~6 KB overhead (negligible).

### CPU Usage

- **Idle state** (no failures): Minimal - just timer callbacks
- **During backoff**: Reduced CPU usage due to longer intervals
- **On resume**: Brief spike from staggered flush, then normal

### Network Usage

- **Normal operation**: Consistent request rate
- **During failures**: Exponentially reduced request rate (desired behavior)
- **On recovery**: Immediate return to normal rate

## Testing Strategies

### Unit Tests

```typescript
describe('AdaptiveScheduler', () => {
  it('should double interval on consecutive failures', async () => {
    const scheduler = new AdaptiveScheduler();
    let callCount = 0;
    
    scheduler.scheduleRefresh('test', async () => {
      callCount++;
      return false; // always fail
    }, 1000);
    
    await sleep(1000);
    expect(callCount).toBe(1);
    
    await sleep(2000); // doubled interval
    expect(callCount).toBe(2);
    
    await sleep(4000); // doubled again
    expect(callCount).toBe(3);
  });
  
  it('should reset backoff on success', async () => {
    const scheduler = new AdaptiveScheduler();
    let shouldFail = true;
    
    scheduler.scheduleRefresh('test', async () => {
      return !shouldFail;
    }, 1000);
    
    // ... cause failures ...
    shouldFail = false;
    scheduler.trigger('test'); // manual trigger
    
    const state = scheduler.getBackoffState('test');
    expect(state.multiplier).toBe(1);
    expect(state.failures).toBe(0);
  });
});
```

### Integration Tests

Mock `fetch` to simulate service failures and recoveries, verify backoff timing and state transitions.

### Manual Testing

Use browser DevTools to throttle network, simulate hidden/visible transitions, and observe polling behavior in the Network tab.

## References

- **Source**: worldmonitor/src/services/runtime.ts - `startSmartPollLoop` function
- **Implementation**: my-daily-monitor/src/services/refresh-scheduler.ts
- **AWS Architecture Blog**: [Exponential Backoff and Jitter](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/)
- **Google SRE Book**: [Handling Overload](https://sre.google/sre-book/handling-overload/)
