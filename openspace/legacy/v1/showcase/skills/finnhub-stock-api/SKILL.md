---
name: finnhub-stock-api
description: Integrate with Finnhub Stock API for real-time and historical stock market data. Covers quote fetching, company profiles, and WebSocket streaming.
---

# Finnhub Stock API Integration

Finnhub provides free stock market data (REST + WebSocket). API key required (free tier: 60 calls/min).

- **Docs**: https://finnhub.io/docs/api
- **Base URL**: `https://finnhub.io/api/v1`
- **Auth**: Query param `token=YOUR_API_KEY`
- **Rate limit**: 60 requests/minute (free), 300/min (premium)
- **CORS**: Unknown (use server-side proxy)

## Key Endpoints

### 1. Stock Quote (Real-Time)

```
GET /quote?symbol=AAPL&token=API_KEY
```

Response:
```json
{
  "c": 178.72,     // Current price
  "d": 2.38,       // Change (absolute)
  "dp": 1.35,      // Change percent
  "h": 179.60,     // High price of the day
  "l": 175.10,     // Low price of the day
  "o": 176.15,     // Open price
  "pc": 176.34,    // Previous close
  "t": 1702569600  // Timestamp
}
```

### 2. Company Profile

```
GET /stock/profile2?symbol=AAPL&token=API_KEY
```

Response:
```json
{
  "country": "US",
  "currency": "USD",
  "exchange": "NASDAQ NMS - GLOBAL MARKET",
  "name": "Apple Inc",
  "ticker": "AAPL",
  "ipo": "1980-12-12",
  "marketCapitalization": 2794000,
  "logo": "https://static.finnhub.io/logo/..."
}
```

### 3. Stock Candles (Historical)

```
GET /stock/candle?symbol=AAPL&resolution=D&from=1609459200&to=1640995200&token=API_KEY
```

- `resolution`: 1, 5, 15, 30, 60, D, W, M
- `from` / `to`: UNIX timestamps

Response:
```json
{
  "c": [130.21, 131.96, ...],  // Close prices
  "h": [131.74, 132.63, ...],  // Highs
  "l": [128.50, 130.23, ...],  // Lows
  "o": [130.92, 130.87, ...],  // Opens
  "v": [143301900, ...],       // Volumes
  "t": [1609459200, ...],      // Timestamps
  "s": "ok"                     // Status
}
```

### 4. Market News

```
GET /news?category=general&token=API_KEY
```

Categories: `general`, `forex`, `crypto`, `merger`

## Frontend Service Implementation

```typescript
// src/services/stock-market.ts
import { createCircuitBreaker } from '@/utils/circuit-breaker';

export interface StockQuote {
  symbol: string;
  name: string;
  price: number | null;
  change: number | null;
  changePercent: number | null;
  high: number | null;
  low: number | null;
  sparkline?: number[];
}

const quoteBreaker = createCircuitBreaker<StockQuote[]>({
  name: 'StockQuotes',
  cacheTtlMs: 30_000,
});

const DEFAULT_SYMBOLS = ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'TSLA', 'NVDA', 'META'];

export async function fetchStockQuotes(symbols = DEFAULT_SYMBOLS): Promise<StockQuote[]> {
  return quoteBreaker.execute(async () => {
    const resp = await fetch(`/api/stocks?symbols=${symbols.join(',')}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    return data.quotes.map((q: any) => ({
      symbol: q.symbol,
      name: q.name || q.symbol,
      price: q.price ?? q.c ?? null,
      change: q.change ?? q.d ?? null,
      changePercent: q.changePercent ?? q.dp ?? null,
      high: q.high ?? q.h ?? null,
      low: q.low ?? q.l ?? null,
    }));
  }, []);
}
```

## Alternative APIs (from public-apis)

If Finnhub rate limits are a concern, these alternatives are available:

| API | Free Tier | Notes |
|-----|-----------|-------|
| **Alpha Vantage** | 25 req/day (free) | `https://www.alphavantage.co/` |
| **Twelve Data** | 800 req/day | `https://twelvedata.com/` |
| **Financial Modeling Prep** | 250 req/day | `https://financialmodelingprep.com/` |
| **Yahoo Finance** (via RapidAPI) | Varies | `https://www.yahoofinanceapi.com/` |
| **Polygon** | 5 req/min (free) | `https://polygon.io/` |
| **IEX Cloud** | 50k credits/month | `https://iexcloud.io/` |
| **Marketstack** | 100 req/month | `https://marketstack.com/` |

## Server-Side Proxy

The frontend should **never** call Finnhub directly. Use the API proxy pattern (see `api-proxy-endpoint` skill) to:
1. Hide the API key
2. Batch multiple symbol requests
3. Add edge caching (30s `Cache-Control`)
4. Handle rate limit errors gracefully (return cached data)

