---
name: api-proxy-endpoint
description: Create serverless API proxy endpoints that hide API keys and provide a unified backend for the dashboard frontend. Designed for Vercel deployment.
---

# API Proxy Endpoint Pattern

External APIs often require API keys that must not be exposed in frontend code. Create serverless proxy endpoints that:
- Hide API keys on the server side
- Provide a unified `/api/*` namespace for the frontend
- Handle CORS, rate limiting, and error wrapping

## Endpoint Structure

Each API endpoint is a file in the `api/` directory:

```
api/
├── stocks.ts          # Stock market data proxy
├── news.ts            # News API proxy
├── calendar.ts        # Calendar events proxy
└── _cors.ts           # Shared CORS helper
```

## CORS Helper

```typescript
// api/_cors.ts
export function corsHeaders() {
  return {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
  };
}

export function handleCors(req: Request): Response | null {
  if (req.method === 'OPTIONS') {
    return new Response(null, { status: 204, headers: corsHeaders() });
  }
  return null;
}
```

## Example: Stock Proxy Endpoint

```typescript
// api/stocks.ts
import type { VercelRequest, VercelResponse } from '@vercel/node';

export default async function handler(req: VercelRequest, res: VercelResponse) {
  // CORS
  res.setHeader('Access-Control-Allow-Origin', '*');
  if (req.method === 'OPTIONS') return res.status(204).end();

  const symbols = (req.query.symbols as string || '').split(',').filter(Boolean);
  if (symbols.length === 0) {
    return res.status(400).json({ error: 'Missing symbols parameter' });
  }

  const apiKey = process.env.FINNHUB_API_KEY;
  if (!apiKey) {
    return res.status(500).json({ error: 'API key not configured' });
  }

  try {
    const quotes = await Promise.all(
      symbols.map(async (sym) => {
        const resp = await fetch(
          `https://finnhub.io/api/v1/quote?symbol=${sym}&token=${apiKey}`
        );
        if (!resp.ok) throw new Error(`Finnhub ${resp.status}`);
        const data = await resp.json();
        return {
          symbol: sym,
          price: data.c,       // current price
          change: data.dp,     // percent change
          high: data.h,
          low: data.l,
          open: data.o,
          prevClose: data.pc,
        };
      })
    );
    res.setHeader('Cache-Control', 's-maxage=30, stale-while-revalidate=60');
    return res.json({ quotes });
  } catch (err) {
    console.error('Stock API error:', err);
    return res.status(502).json({ error: 'Upstream API failed' });
  }
}
```

## Example: News Proxy Endpoint

```typescript
// api/news.ts
import type { VercelRequest, VercelResponse } from '@vercel/node';

export default async function handler(req: VercelRequest, res: VercelResponse) {
  res.setHeader('Access-Control-Allow-Origin', '*');
  if (req.method === 'OPTIONS') return res.status(204).end();

  const apiKey = process.env.NEWS_API_KEY;
  if (!apiKey) return res.status(500).json({ error: 'API key not configured' });

  const query = req.query.q as string || '';
  const category = req.query.category as string || 'general';
  const lang = req.query.lang as string || 'en';

  try {
    const url = query
      ? `https://gnews.io/api/v4/search?q=${encodeURIComponent(query)}&lang=${lang}&max=20&token=${apiKey}`
      : `https://gnews.io/api/v4/top-headlines?category=${category}&lang=${lang}&max=20&token=${apiKey}`;

    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`GNews ${resp.status}`);
    const data = await resp.json();

    res.setHeader('Cache-Control', 's-maxage=120, stale-while-revalidate=300');
    return res.json({
      articles: (data.articles || []).map((a: any) => ({
        title: a.title,
        description: a.description,
        url: a.url,
        source: a.source?.name || '',
        publishedAt: a.publishedAt,
        image: a.image,
      })),
    });
  } catch (err) {
    console.error('News API error:', err);
    return res.status(502).json({ error: 'Upstream API failed' });
  }
}
```

## Key Patterns

1. **One file per API domain** in the `api/` directory
2. **Always set CORS headers** — frontend runs on different origin during dev
3. **Environment variables** for API keys (`process.env.FINNHUB_API_KEY`)
4. **Cache-Control headers** for edge caching (Vercel CDN)
5. **Error wrapping** — return structured JSON errors, never raw upstream errors
6. **Input validation** — validate query parameters before calling upstream
7. **Typed responses** — keep response shapes consistent for frontend consumption

## Local Development

During `vite dev`, configure a proxy in `vite.config.ts`:

```typescript
export default defineConfig({
  server: {
    proxy: {
      '/api': {
        target: 'http://localhost:3000',
        changeOrigin: true,
      },
    },
  },
});
```

Or use `vercel dev` to run serverless functions locally.

