/**
 * API Proxy server — lightweight Express server for external API calls.
 * Runs alongside Vite dev server via `concurrently`.
 * In production, deploy as serverless functions or a standalone Node service.
 *
 * Usage: npx tsx server/index.ts
 */

import http from 'node:http';
import { parse as parseUrl } from 'node:url';
import { apiRoutes } from './routes/index';

const PORT = Number(process.env.API_PORT || 3001);

const server = http.createServer(async (req, res) => {
  // CORS
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, X-API-Key, X-Finnhub-Key, X-Github-Token, X-Feishu-App-Id, X-Feishu-App-Secret, X-Twitter-Token, X-Gmail-Token, X-OpenRouter-Key');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');

  if (req.method === 'OPTIONS') { res.writeHead(204); res.end(); return; }

  const parsed = parseUrl(req.url || '/', true);
  const pathname = parsed.pathname || '/';
  const query: Record<string, string> = {};
  for (const [k, v] of Object.entries(parsed.query || {})) {
    query[k] = Array.isArray(v) ? v[0] || '' : v || '';
  }

  const handler = apiRoutes[pathname];
  if (!handler) {
    res.writeHead(404, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'Not found' }));
    return;
  }

  // Read body for POST
  let body = '';
  if (req.method === 'POST') {
    for await (const chunk of req) body += chunk;
  }

  try {
    const result = await handler(query, body, req.headers);
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(result));
  } catch (err: any) {
    console.error(`[API] ${pathname} error:`, err.message);
    res.writeHead(500, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: err.message || 'Internal error' }));
  }
});

server.listen(PORT, () => {
  console.log(`[API] Proxy server running on http://localhost:${PORT}`);
});
