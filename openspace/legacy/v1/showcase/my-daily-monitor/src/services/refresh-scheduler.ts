export interface RefreshRegistration {
  name: string;
  fn: () => Promise<boolean | void>;
  intervalMs: number;
  condition?: () => boolean;
}

interface RunnerEntry {
  timer: ReturnType<typeof setInterval> | null;
  intervalMs: number;
  fn: () => Promise<boolean | void>;
  condition?: () => boolean;
  lastRun: number;
  // Exponential backoff state
  consecutiveFailures: number;
  currentBackoffMultiplier: number;
}

export class RefreshScheduler {
  private runners = new Map<string, RunnerEntry>();
  private inFlight = new Set<string>();
  private hiddenSince = 0;
  private visibilityHandler: (() => void) | null = null;
  private readonly maxBackoffMultiplier = 8;

  constructor() {
    this.visibilityHandler = () => this.onVisibilityChange();
    document.addEventListener('visibilitychange', this.visibilityHandler);
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
    if (entry.timer) {
      clearInterval(entry.timer);
      entry.timer = null;
    }

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
      
      // Boolean return indicates explicit success/failure
      if (typeof result === 'boolean') {
        success = result;
      }
      // void/undefined is treated as success
      
      entry.lastRun = Date.now();

      if (success) {
        // Auto-recovery: reset backoff and failure count on success
        if (entry.consecutiveFailures > 0 || entry.currentBackoffMultiplier > 1) {
          entry.consecutiveFailures = 0;
          entry.currentBackoffMultiplier = 1;
          
          // Reschedule at normal interval
          if (!document.hidden && entry.timer) {
            this.scheduleNextRun(name, entry);
          }
        }
      } else {
        // Exponential backoff on explicit failure
        entry.consecutiveFailures++;
        const newMultiplier = Math.min(
          Math.pow(2, entry.consecutiveFailures),
          this.maxBackoffMultiplier
        );
        
        if (newMultiplier !== entry.currentBackoffMultiplier) {
          entry.currentBackoffMultiplier = newMultiplier;
          
          // Reschedule with new backoff interval
          if (!document.hidden && entry.timer) {
            this.scheduleNextRun(name, entry);
          }
        }
        
        console.warn(
          `[Refresh] ${name} returned failure (${entry.consecutiveFailures} consecutive), ` +
          `backoff multiplier: ${entry.currentBackoffMultiplier}x`
        );
      }
    } catch (err) {
      // Exception is also treated as failure
      success = false;
      entry.consecutiveFailures++;
      const newMultiplier = Math.min(
        Math.pow(2, entry.consecutiveFailures),
        this.maxBackoffMultiplier
      );
      
      if (newMultiplier !== entry.currentBackoffMultiplier) {
        entry.currentBackoffMultiplier = newMultiplier;
        
        // Reschedule with new backoff interval
        if (!document.hidden && entry.timer) {
          this.scheduleNextRun(name, entry);
        }
      }
      
      console.error(
        `[Refresh] ${name} failed (${entry.consecutiveFailures} consecutive), ` +
        `backoff multiplier: ${entry.currentBackoffMultiplier}x:`,
        err
      );
    } finally {
      this.inFlight.delete(name);
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

  flushStaleRefreshes(): void {
    if (!this.hiddenSince) return;
    const hiddenMs = Date.now() - this.hiddenSince;
    this.hiddenSince = 0;

    let stagger = 0;
    for (const [name, entry] of this.runners) {
      // Use effective interval (base * backoff multiplier) for staleness check
      const effectiveInterval = entry.intervalMs * entry.currentBackoffMultiplier;
      if (hiddenMs < effectiveInterval) continue;
      
      const delay = stagger;
      stagger += 150;
      setTimeout(() => this.runRefresh(name), delay);
    }
  }

  registerAll(registrations: RefreshRegistration[]): void {
    for (const reg of registrations) {
      this.scheduleRefresh(reg.name, reg.fn, reg.intervalMs, reg.condition);
    }
  }

  trigger(name: string): void {
    this.runRefresh(name);
  }

  getBackoffState(name: string): { failures: number; multiplier: number } | null {
    const entry = this.runners.get(name);
    if (!entry) return null;
    return {
      failures: entry.consecutiveFailures,
      multiplier: entry.currentBackoffMultiplier,
    };
  }

  resetBackoff(name: string): void {
    const entry = this.runners.get(name);
    if (!entry) return;
    
    entry.consecutiveFailures = 0;
    entry.currentBackoffMultiplier = 1;
    
    // Reschedule at normal interval
    if (!document.hidden && entry.timer) {
      this.scheduleNextRun(name, entry);
    }
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
