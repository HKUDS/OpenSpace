/**
 * Vite plugin that embeds the API proxy server directly into the dev server.
 * No need for a separate process — `npm run dev` handles everything.
 */
import type { Plugin, ViteDevServer } from 'vite';
import { handleStockRequest } from './server/routes/stock';
import { handleNewsRequest } from './server/routes/news';
import { handleGithubRequest } from './server/routes/github';
import { handleEmailRequest } from './server/routes/email';
import { handleCalendarRequest } from './server/routes/calendar';
import { handleFeishuRequest } from './server/routes/feishu';
import { handleSocialRequest } from './server/routes/social';
import { handleSystemRequest } from './server/routes/system';
import { handleOfficeRequest } from './server/routes/office';
import { handleHealthRequest } from './server/routes/health';

type RouteHandler = (query: Record<string, string>, body: string, headers: Record<string, string | string[] | undefined>) => Promise<unknown>;

const routes: Record<string, RouteHandler> = {
  '/api/stocks': handleStockRequest,
  '/api/news': handleNewsRequest,
  '/api/github': handleGithubRequest,
  '/api/emails': handleEmailRequest,
  '/api/calendar': handleCalendarRequest,
  '/api/feishu': handleFeishuRequest,
  '/api/social': handleSocialRequest,
  '/api/system': handleSystemRequest,
  '/api/office': handleOfficeRequest,
  '/api/health': handleHealthRequest,
};

export function apiPlugin(): Plugin {
  return {
    name: 'embedded-api',
    configureServer(server: ViteDevServer) {
      server.middlewares.use(async (req, res, next) => {
        const url = new URL(req.url || '/', `http://${req.headers.host || 'localhost'}`);
        const handler = routes[url.pathname];
        if (!handler) return next();

        // Parse query
        const query: Record<string, string> = {};
        for (const [k, v] of url.searchParams) query[k] = v;

        // Read body for POST
        let body = '';
        if (req.method === 'POST') {
          for await (const chunk of req) body += chunk;
        }

        // CORS
        res.setHeader('Access-Control-Allow-Origin', '*');
        res.setHeader('Access-Control-Allow-Headers', '*');
        if (req.method === 'OPTIONS') { res.writeHead(204); res.end(); return; }

        try {
          const result = await handler(query, body, req.headers as any);
          res.writeHead(200, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify(result));
        } catch (err: any) {
          console.error(`[API] ${url.pathname} error:`, err.message);
          res.writeHead(500, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: err.message }));
        }
      });
    },
  };
}
