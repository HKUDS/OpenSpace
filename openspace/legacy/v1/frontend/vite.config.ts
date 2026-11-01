import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, __dirname, '');
  const port = Number(env.VITE_PORT || 3888);
  const host = env.VITE_HOST || '127.0.0.1';
  const apiProxyTarget = env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:7788';

  return {
    plugins: [react()],
    resolve: {
      alias: {
        '@': path.resolve(__dirname, './src'),
      },
    },
    server: {
      host,
      port,
      proxy: {
        '/api': {
          target: apiProxyTarget,
          changeOrigin: true,
        },
      },
    },
    preview: {
      host,
      port,
    },
  };
});
