---
name: data-service-optional-params
description: Create data fetching services with circuit breaker pattern for API resilience, including a first-class pattern for building optional query parameters via URLSearchParams. Services handle fetch, cache, retry, optional filters, and expose typed data to panel components.
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

## Optional Query Parameter Pattern

When a service function accepts **optional filter arguments** (e.g. `owner?`, `repo?`, `branch?`), build the query string conditionally using `URLSearchParams` and only append a key when the value is defined. Never include `undefined` or empty-string values in the query string — omit the key entirely.

### Template

```typescript
export async function fetchItems(
  required: string,
  owner?: string,
  repo?: string,
  branch?: string,
): Promise<Item[]> {
  return breaker.execute(async () => {
    // 1. Start with required params
    const params = new URLSearchParams({ required });

    // 2. Conditionally append optional params
    if (owner)  params.set('owner',  owner);
    if (repo)   params.set('repo',   repo);
    if (branch) params.set('branch', branch);

    // 3. Append query string only when there are params
    const qs = params.toString();
    const url = qs ? `/api/items?${qs}` : '/api/items';

    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
  }, []);
}
```

### Real-world example — GitHub workflow runs

```typescript
// src/services/code-status.ts
import { createCircuitBreaker } from '../utils/circuit-breaker';

export interface WorkflowRun {
  id: number;
  name: string;
  status: string;
  conclusion: string | null;
  created_at: string;
  html_url: string;
}

const breaker = createCircuitBreaker<WorkflowRun[]>({
  name: 'CodeStatus',
  cacheTtlMs: 60_000, // 1 minute cache
});

/**
 * Fetches recent GitHub workflow runs.
 * owner and repo are optional — if omitted the server uses defaults
 * defined in the API proxy (e.g. from environment variables).
 */
export async function fetchWorkflowRuns(
  owner?: string,
  repo?: string,
): Promise<WorkflowRun[]> {
  return breaker.execute(async () => {
    const params = new URLSearchParams();
    if (owner) params.set('owner', owner);
    if (repo)  params.set('repo',  repo);

    const qs = params.toString();
    const resp = await fetch(qs ? `/api/github?${qs}` : '/api/github');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
  }, []);
}
```

### Rules for optional params

| Rule | Rationale |
|------|-----------|
| Use `params.set(key, value)` inside `if (value)` guards | Prevents `key=undefined` or `key=` appearing in the URL |
| Build `URLSearchParams` before the `fetch` call | Keeps URL construction readable and testable in isolation |
| Use `params.toString()` to decide whether to append `?` | `new URLSearchParams().toString()` is `""`, so `qs ? url?${qs} : url` is safe |
| Prefer `params.set()` over string concatenation | Automatically percent-encodes special characters |
| All optional params go after required params | Keeps the URLSearchParams object ordered and predictable |

### Testing optional params in isolation

Because URL construction is pure logic, test it separately before wiring up the fetch:

```typescript
function buildGithubParams(owner?: string, repo?: string): string {
  const params = new URLSearchParams();
  if (owner) params.set('owner', owner);
  if (repo)  params.set('repo',  repo);
  return params.toString();
}

// Examples
buildGithubParams();                    // ""
buildGithubParams('acme');              // "owner=acme"
buildGithubParams('acme', 'dashboard'); // "owner=acme&repo=dashboard"
buildGithubParams(undefined, 'dash');   // "repo=dash"
```

Extract this helper when the same param set is used in multiple service functions.

## Key Patterns

1. **One circuit breaker per API endpoint** — create at module level
2. **Export typed interfaces** for data shapes
3. **Wrap fetch calls** in `breaker.execute(fn, defaultValue)`
4. **Default values** are empty arrays `[]` or empty objects `{}`
5. **Services are stateless modules** — no class instances, just exported functions
6. **API proxy**: Frontend calls `/api/...` which proxies to external APIs (hides API keys)
7. **Optional params**: Use `URLSearchParams` + conditional `params.set()` — never build query strings by hand

## API Proxy Pattern

For APIs requiring keys, create server-side proxy endpoints:

```typescript
// api/github.ts (Vercel serverless function)
export default async function handler(req, res) {
  // Read optional overrides from the query string; fall back to env defaults
  const owner = (req.query.owner as string) ?? process.env.GITHUB_OWNER;
  const repo  = (req.query.repo  as string) ?? process.env.GITHUB_REPO;
  const token = process.env.GITHUB_TOKEN;

  const url = `https://api.github.com/repos/${owner}/${repo}/actions/runs?per_page=10`;
  const data = await fetch(url, {
    headers: { Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json' },
  });
  res.json(await data.json());
}
```

This keeps API keys server-side, forwards only the safe query parameters the frontend supplies, and provides a single endpoint for the frontend.
