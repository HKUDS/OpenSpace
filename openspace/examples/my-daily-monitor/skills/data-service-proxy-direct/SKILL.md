---
name: data-service-proxy-direct
description: Create resilient data fetching services with circuit breaker pattern, supporting both proxied and direct API calls. Services handle fetch, cache, retry, and expose typed data to panel components.
---

# Data Service Pattern

Each panel's data comes from a dedicated **service module** in `src/services/`. Services handle API calls, caching, error handling with circuit breaker pattern, and can work with both proxied and direct API calls.

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

Each service is a TypeScript module that exports async data-fetching functions. Services can work with both proxied and direct API calls.

### Proxied API Example

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

### Direct API Example

```typescript
// src/services/weather.ts
import { createCircuitBreaker } from '../utils/circuit-breaker';

export interface WeatherData {
  city: string;
  temperature: number;
  condition: string;
  forecast: Array<{
    day: string;
    high: number;
    low: number;
    condition: string;
  }>;
}

const breaker = createCircuitBreaker<WeatherData>({
  name: 'WeatherAPI',
  cacheTtlMs: 30_000, // 30 seconds cache
});

export async function fetchWeather(city: string): Promise<WeatherData> {
  return breaker.execute(async () => {
    const resp = await fetch(`https://wttr.in/${city}?format=j1`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
  }, {
    city,
    temperature: 0,
    condition: 'Unknown',
    forecast: []
  });
}
```

## Key Patterns

1. **One circuit breaker per API endpoint** — create at module level
2. **Export typed interfaces** for data shapes
3. **Wrap fetch calls** in `breaker.execute(fn, defaultValue)`
4. **Default values** are empty arrays `[]` or empty objects `{}`
5. **Services are stateless modules** — no class instances, just exported functions
6. **Support both proxied and direct API calls** — choose based on API requirements
7. **API proxy**: Frontend calls `/api/...` which proxies to external APIs (hides API keys)
8. **Direct API**: Frontend calls external APIs directly when no sensitive data is involved

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
