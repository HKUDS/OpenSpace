---
name: data-service-circuit-breaker-aware
description: Create data fetching services with circuit breaker pattern for API resilience. Services handle fetch, cache, retry, and expose typed data to panel components. Includes clear guidance on when to skip the circuit breaker for user-triggered one-shot calls versus background auto-polling services.
---

# Data Service Pattern

Each panel's data comes from a dedicated **service module** in `src/services/`. Services handle API calls, caching, error handling with circuit breaker pattern.

## Circuit Breaker

The circuit breaker prevents cascading failures when an API is down. After N consecutive failures, it enters "cooldown" mode and serves cached data instead of hitting the API.

Create `src/utils/circuit-breaker.ts`:

```typescript
interface CircuitState {
  failures: number;
  cooldownUntil: number;
}

interface CacheEntry<T> {
  data: T;
  timestamp: number;
}

export interface CircuitBreakerOptions {
  name: string;
  maxFailures?: number;
  cooldownMs?: number;
  cacheTtlMs?: number;
}

export class CircuitBreaker<T> {
  private state: CircuitState = { failures: 0, cooldownUntil: 0 };
  private cache: CacheEntry<T> | null = null;
  private name: string;
  private maxFailures: number;
  private cooldownMs: number;
  private cacheTtlMs: number;

  constructor(options: CircuitBreakerOptions) {
    this.name = options.name;
    this.maxFailures = options.maxFailures ?? 2;
    this.cooldownMs = options.cooldownMs ?? 5 * 60 * 1000;  // 5 minutes
    this.cacheTtlMs = options.cacheTtlMs ?? 10 * 60 * 1000; // 10 minutes
  }

  isOnCooldown(): boolean {
    if (Date.now() < this.state.cooldownUntil) return true;
    if (this.state.cooldownUntil > 0) {
      this.state = { failures: 0, cooldownUntil: 0 };
    }
    return false;
  }

  getCached(): T | null {
    if (this.cache && Date.now() - this.cache.timestamp < this.cacheTtlMs) {
      return this.cache.data;
    }
    return null;
  }

  recordSuccess(data: T): void {
    this.state = { failures: 0, cooldownUntil: 0 };
    this.cache = { data, timestamp: Date.now() };
  }

  recordFailure(error?: string): void {
    this.state.failures++;
    if (this.state.failures >= this.maxFailures) {
      this.state.cooldownUntil = Date.now() + this.cooldownMs;
      console.warn(`[${this.name}] Cooldown for ${this.cooldownMs / 1000}s`);
    }
  }

  async execute<R extends T>(fn: () => Promise<R>, defaultValue: R): Promise<R> {
    if (this.isOnCooldown()) {
      const cached = this.getCached();
      return (cached as R) ?? defaultValue;
    }
    const cached = this.getCached();
    if (cached !== null) return cached as R;
    try {
      const result = await fn();
      this.recordSuccess(result);
      return result;
    } catch (e) {
      console.error(`[${this.name}] Failed:`, e);
      this.recordFailure(String(e));
      return defaultValue;
    }
  }
}

export function createCircuitBreaker<T>(options: CircuitBreakerOptions): CircuitBreaker<T> {
  return new CircuitBreaker<T>(options);
}
```

## Service Module Pattern

Each service is a TypeScript module that exports async data-fetching functions:

```typescript
// src/services/stock-market.ts
import { createCircuitBreaker } from '../utils/circuit-breaker';

export interface StockQuote {
  symbol: string;
  name: string;
  price: number | null;
  change: number | null;
  sparkline?: number[];
}

const breaker = createCircuitBreaker<StockQuote[]>({
  name: 'StockMarket',
  cacheTtlMs: 60_000, // 1 minute cache
});

export async function fetchStockQuotes(symbols: string[]): Promise<StockQuote[]> {
  return breaker.execute(async () => {
    const params = new URLSearchParams({ symbols: symbols.join(',') });
    const resp = await fetch(`/api/stocks?${params}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
  }, []);
}
```

## Key Patterns

1. **One circuit breaker per API endpoint** — create at module level
2. **Export typed interfaces** for data shapes
3. **Wrap fetch calls** in `breaker.execute(fn, defaultValue)`
4. **Default values** are empty arrays `[]` or empty objects `{}`
5. **Services are stateless modules** — no class instances, just exported functions
6. **API proxy**: Frontend calls `/api/...` which proxies to external APIs (hides API keys)

## When to Skip the Circuit Breaker

The circuit breaker is designed to protect **background auto-polling services** from cascading failures. It is **not always appropriate** — applying it blindly adds unnecessary complexity and can actively cause problems for user-driven flows.

### Use the circuit breaker when:
- The service is **polled automatically** on an interval (e.g., every 30 seconds, every minute)
- Repeated failures would hammer a degraded API endpoint
- Serving stale cached data is an acceptable fallback (the user won't notice a brief outage)
- The data refreshes in the background without explicit user intent

```typescript
// ✅ Background polling — circuit breaker appropriate
const breaker = createCircuitBreaker<StockQuote[]>({ name: 'StockMarket', cacheTtlMs: 60_000 });

export async function fetchStockQuotes(symbols: string[]): Promise<StockQuote[]> {
  return breaker.execute(async () => {
    const resp = await fetch(`/api/stocks?symbols=${symbols.join(',')}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
  }, []);
}

// Called on a timer — circuit breaker protects against rapid failure storms
setInterval(() => fetchStockQuotes(['AAPL', 'MSFT']), 60_000);
```

### Skip the circuit breaker when:
- The call is **user-triggered** (button click, explicit refresh, form submit)
- The operation is **one-shot** and non-repeating (e.g., LLM text generation, send message)
- Failure should surface immediately to the user as an error — not silently return stale data
- The user expects a **fresh result**, not a cache hit
- Retry/error handling is managed by the UI layer (e.g., a "Try again" button)

```typescript
// ✅ User-triggered one-shot LLM call — NO circuit breaker
export interface DailyBriefingData {
  briefing: string;
  generatedAt: string;
}

export async function generateDailyBriefing(context: string): Promise<DailyBriefingData> {
  // No circuit breaker: this is explicitly triggered by the user clicking "Generate".
  // Failures should throw so the UI can show an error and offer a retry button.
  const resp = await fetch('/api/llm/generate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ prompt: context }),
  });
  if (!resp.ok) throw new Error(`LLM generation failed: HTTP ${resp.status}`);
  const { text } = await resp.json();
  return { briefing: text, generatedAt: new Date().toISOString() };
}
```

### Decision table

| Trigger              | Repeats automatically? | Stale data OK? | Use circuit breaker? |
|----------------------|------------------------|----------------|----------------------|
| Timer / interval     | ✅ Yes                 | ✅ Yes         | ✅ Yes               |
| App startup fetch    | ❌ No                  | ✅ Yes         | Optional             |
| Button / user action | ❌ No                  | ❌ No          | ❌ No                |
| LLM generation       | ❌ No                  | ❌ No          | ❌ No                |
| Explicit refresh     | ❌ No                  | ❌ No          | ❌ No                |

### UI error handling for one-shot calls

When skipping the circuit breaker, the calling component is responsible for surfacing errors:

```typescript
// In the panel/component that calls a one-shot service:
async generate(): Promise<void> {
  if (this.isFetching) return;
  this.isFetching = true;
  this.showLoading();
  try {
    const data = await generateDailyBriefing(this.buildContext());
    this.render(data);
  } catch (e) {
    // Surface the error directly — no silent cache fallback
    this.showError('Generation failed. Please try again.', () => this.generate());
  } finally {
    this.isFetching = false;
  }
}
```

> **Rule of thumb**: If a human is waiting for the result of a specific action they just took, skip the circuit breaker and let errors propagate to the UI. If a machine is polling in the background, use the circuit breaker.

## API Proxy Pattern

For APIs requiring keys, create server-side proxy endpoints:

```typescript
// api/stocks.ts (Vercel serverless function)
export default async function handler(req, res) {
  const { symbols } = req.query;
  const apiKey = process.env.FINNHUB_API_KEY;
  const data = await fetch(`https://finnhub.io/api/v1/quote?symbol=${symbols}&token=${apiKey}`);
  res.json(await data.json());
}
```

This keeps API keys server-side and provides a single endpoint for the frontend.
