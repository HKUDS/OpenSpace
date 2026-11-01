import assert from 'node:assert/strict';
import { apiRoutes } from './index';

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
