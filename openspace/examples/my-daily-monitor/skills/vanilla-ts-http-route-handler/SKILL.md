---
name: vanilla-ts-http-route-handler
description: How to correctly add route handlers in a vanilla TypeScript project that uses http.createServer with a manual switch/case router — without introducing Express Router or Next.js patterns.
---

# Adding Route Handlers to a Vanilla TypeScript HTTP Server

Use this skill whenever you need to add a new route to a TypeScript project
that uses Node's built-in `http` module and a `switch/case` pathname router
**without Express or Next.js**.

---

## Step 0 — Confirm the routing style

Before writing any code, inspect `server/index.ts` (or whichever file starts
the HTTP server).  Look for:

```ts
http.createServer((req, res) => {
  const { pathname } = new URL(req.url!, `http://${req.headers.host}`);
  switch (pathname) {
    case '/api/foo': ...
  }
});
```

If you see this pattern, follow the steps below.  
**Do NOT use `express.Router`, `next/server`, or any framework-specific
handler signature** — even if other files in `server/routes/` happen to
use those patterns.

---

## Step 1 — Create the route file

Create `server/routes/<feature>.ts`.  
Use **only** the native Node types: `http.IncomingMessage` and
`http.ServerResponse`.

```ts
// server/routes/health.ts
import http from 'http';

export async function handleHealth(
  req: http.IncomingMessage,
  res: http.ServerResponse
): Promise<void> {
  // Parse body for POST/PUT if needed
  const body = await readBody(req);   // helper shown in Step 3

  res.writeHead(200, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({ status: 'ok' }));
}
```

Rules for the route file:
- Export the handler as a **named async function** (`handle<Feature>`).
- Accept exactly `(req: http.IncomingMessage, res: http.ServerResponse)`.
- Return `Promise<void>`.
- Do **not** call `next()` or return a `Response` object.
- Handle errors internally and write an appropriate HTTP status + JSON body.

---

## Step 2 — Import the handler in `server/index.ts`

Add a named import at the top of the file alongside any existing imports:

```ts
import { handleHealth } from './routes/health';
```

---

## Step 3 — Add a `case` entry to the switch block

Locate the existing `switch (pathname)` block and add a new `case`:

```ts
switch (pathname) {
  case '/api/health':
    await handleHealth(req, res);
    break;

  // … existing cases …

  default:
    res.writeHead(404, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'Not found' }));
}
```

- Match the exact pathname string used by the client.
- Always `break` (or `return`) after calling the handler.
- Keep the `default` case last.

---

## Step 4 — Optional: shared body-reading helper

If multiple routes need to parse a JSON request body, add a small utility
rather than duplicating the logic:

```ts
// server/utils/readBody.ts
import http from 'http';

export function readBody(req: http.IncomingMessage): Promise<unknown> {
  return new Promise((resolve, reject) => {
    let data = '';
    req.on('data', (chunk) => (data += chunk));
    req.on('end', () => {
      try {
        resolve(data ? JSON.parse(data) : {});
      } catch {
        reject(new Error('Invalid JSON'));
      }
    });
    req.on('error', reject);
  });
}
```

Import and use it inside any route handler that needs it.

---

## Checklist

- [ ] `server/routes/<feature>.ts` exists and exports a named async handler.
- [ ] Handler signature is `(req: http.IncomingMessage, res: http.ServerResponse): Promise<void>`.
- [ ] Handler always calls `res.writeHead(...)` and `res.end(...)` on every code path.
- [ ] Handler is imported in `server/index.ts`.
- [ ] A matching `case` entry is present in the `switch (pathname)` block.
- [ ] No Express / Next.js / Koa types or patterns were introduced.

---

## Anti-patterns to avoid

| ❌ Wrong | ✅ Correct |
|---|---|
| `import { Router } from 'express'` | `import http from 'http'` |
| `export default function handler(req: NextApiRequest, ...)` | `export async function handleFoo(req: http.IncomingMessage, ...)` |
| `router.get('/foo', ...)` | `case '/api/foo': await handleFoo(req, res); break;` |
| Returning a value from the handler | Calling `res.end(...)` and returning `void` |