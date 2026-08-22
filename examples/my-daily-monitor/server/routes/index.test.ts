import assert from 'node:assert/strict';
import { apiRoutes } from './index';
import { handleSocialRequest } from './social';

const requiredRoutes = [
  '/api/stocks',
  '/api/news',
  '/api/github',
  '/api/emails',
  '/api/calendar',
  '/api/feishu',
  '/api/social',
  '/api/system',
  '/api/office',
  '/api/health',
];

for (const route of requiredRoutes) {
  assert.equal(typeof apiRoutes[route], 'function', `${route} must be registered`);
}

assert.deepEqual(Object.keys(apiRoutes), requiredRoutes);

const requests: { url: string; apiKey: string }[] = [];
const fakeFetch: typeof fetch = async (input, init) => {
  const requestHeaders = new Headers(init?.headers);
  requests.push({ url: String(input), apiKey: requestHeaders.get('x-api-key') || '' });
  return new Response(JSON.stringify({
    tweets: [{
      id: '1893456789012345678',
      text: 'A useful OpenSpace update',
      createdAt: '2026-08-22T10:00:00.000Z',
      likeCount: 12,
      retweetCount: 3,
      replyCount: 2,
      quoteCount: 1,
      url: 'https://example.com/untrusted',
      author: { username: 'openspace', name: 'OpenSpace' },
    }],
  }), { status: 200, headers: { 'Content-Type': 'application/json' } });
};

const xResult = await handleSocialRequest(
  { source: 'x', q: 'agent skills' },
  '',
  { 'x-xquik-key': 'xq_test' },
  fakeFetch,
) as { posts: unknown[]; source: string; configured: boolean };

assert.equal(requests.length, 1);
const requestUrl = new URL(requests[0].url);
assert.equal(requestUrl.origin + requestUrl.pathname, 'https://xquik.com/api/v1/x/tweets/search');
assert.equal(requestUrl.searchParams.get('q'), 'agent skills');
assert.equal(requestUrl.searchParams.get('queryType'), 'Latest');
assert.equal(requestUrl.searchParams.get('limit'), '10');
assert.equal(requests[0].apiKey, 'xq_test');
assert.deepEqual(xResult, {
  posts: [{
    id: 'x-1893456789012345678',
    title: 'A useful OpenSpace update',
    url: 'https://x.com/openspace/status/1893456789012345678',
    score: 16,
    comments: 2,
    author: '@openspace',
    platform: 'x',
    timestamp: '2026-08-22T10:00:00.000Z',
  }],
  source: 'x',
  configured: true,
});

const unconfiguredResult = await handleSocialRequest({ source: 'x', q: 'agent skills' }, '', {}, fakeFetch);
assert.deepEqual(unconfiguredResult, { posts: [], source: 'x', configured: false });
assert.equal(requests.length, 1);
