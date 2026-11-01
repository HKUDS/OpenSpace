/**
 * Stock API proxy — Finnhub + Yahoo Finance fallback.
 * Reference: worldmonitor/server/worldmonitor/market/v1/list-market-quotes.ts
 */
import type { IncomingHttpHeaders } from 'node:http';

const FINNHUB_BASE = 'https://finnhub.io/api/v1';
const YAHOO_BASE = 'https://query1.finance.yahoo.com/v8/finance/chart';

// Symbols that only exist on Yahoo (indices/futures)
const YAHOO_ONLY = new Set(['^GSPC', '^DJI', '^IXIC', '^RUT', '^VIX', 'GC=F', 'SI=F', 'CL=F', 'BTC-USD', 'ETH-USD']);

// In-memory cache
const cache = new Map<string, { data: unknown; ts: number }>();
const CACHE_TTL = 8 * 60_000; // 8 min

async function fetchFinnhub(symbol: string, apiKey: string) {
  const resp = await fetch(`${FINNHUB_BASE}/quote?symbol=${symbol}&token=${apiKey}`);
  if (!resp.ok) return null;
  const d = await resp.json() as Record<string, number>;
  if (!d.c && d.c !== 0) return null;
  return {
    symbol,
    name: symbol,
    price: d.c,
    change: d.d ?? null,
    changePercent: d.dp ?? null,
    high: d.h ?? null,
    low: d.l ?? null,
    previousClose: d.pc ?? null,
    sparkline: [] as number[],
  };
}

async function fetchYahoo(symbol: string) {
  try {
    const resp = await fetch(`${YAHOO_BASE}/${encodeURIComponent(symbol)}?interval=5m&range=1d`, {
      headers: { 'User-Agent': 'Mozilla/5.0' },
    });
    if (!resp.ok) return null;
    const json = await resp.json() as any;
    const result = json?.chart?.result?.[0];
    if (!result) return null;
    const meta = result.meta;
    const closes = result.indicators?.quote?.[0]?.close?.filter((v: any) => v != null) || [];
    const price = meta.regularMarketPrice ?? closes[closes.length - 1] ?? null;
    const prevClose = meta.chartPreviousClose ?? meta.previousClose ?? null;
    const change = price != null && prevClose ? price - prevClose : null;
    const changePct = change != null && prevClose ? (change / prevClose) * 100 : null;
    return {
      symbol,
      name: meta.shortName || meta.symbol || symbol,
      price,
      change,
      changePercent: changePct,
      high: meta.regularMarketDayHigh ?? null,
      low: meta.regularMarketDayLow ?? null,
      previousClose: prevClose,
      sparkline: closes.slice(-20),
    };
  } catch { return null; }
}

export async function handleStockRequest(
  query: Record<string, string>,
  _body: string,
  headers: IncomingHttpHeaders,
): Promise<unknown> {
  const apiKey = (headers['x-finnhub-key'] as string) || process.env.FINNHUB_API_KEY || '';
  const symbolsRaw = query.symbols || 'AAPL,MSFT,GOOGL,AMZN,TSLA,NVDA,META';
  const symbols = symbolsRaw.split(',').map(s => s.trim()).filter(Boolean);
  const cacheKey = symbols.sort().join(',');

  // Check cache
  const cached = cache.get(cacheKey);
  if (cached && Date.now() - cached.ts < CACHE_TTL) {
    return cached.data;
  }

  const finnhubSymbols = symbols.filter(s => !YAHOO_ONLY.has(s));
  const yahooSymbols = symbols.filter(s => YAHOO_ONLY.has(s));

  const quotes: unknown[] = [];

  // Finnhub
  if (finnhubSymbols.length > 0 && apiKey) {
    const results = await Promise.allSettled(
      finnhubSymbols.map(s => fetchFinnhub(s, apiKey))
    );
    for (const r of results) {
      if (r.status === 'fulfilled' && r.value) quotes.push(r.value);
    }
  } else if (finnhubSymbols.length > 0) {
    // Fallback to Yahoo for all symbols if no Finnhub key
    yahooSymbols.push(...finnhubSymbols);
  }

  // Yahoo
  if (yahooSymbols.length > 0) {
    const results = await Promise.allSettled(
      yahooSymbols.map(s => fetchYahoo(s))
    );
    for (const r of results) {
      if (r.status === 'fulfilled' && r.value) quotes.push(r.value);
    }
  }

  const response = { quotes, finnhubAvailable: !!apiKey };
  cache.set(cacheKey, { data: response, ts: Date.now() });
  return response;
}

