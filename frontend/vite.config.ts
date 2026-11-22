import { readFileSync } from 'node:fs';
import { Agent } from 'node:https';
import { resolve } from 'node:path';
import { defineConfig } from 'vitest/config';
import type { Plugin } from 'vite';
import react from '@vitejs/plugin-react';

// Opt-in local HTTPS only. Private keys/CA never enter the client bundle.
// All three certificate paths are mandatory; partial TLS configuration fails closed.
const tlsCert = process.env.KP_DESK_LOCAL_TLS_CERT;
const tlsKey = process.env.KP_DESK_LOCAL_TLS_KEY;
const tlsCa = process.env.KP_DESK_LOCAL_TLS_CA;
const enabled = Boolean(tlsCert && tlsKey && tlsCa);
if (!enabled && (tlsCert || tlsKey || tlsCa)) {
  throw new Error('Set all KP_DESK_LOCAL_TLS_CERT, KEY and CA paths for HTTPS.');
}
const devHttps = enabled ? {
  cert: readFileSync(tlsCert!),
  key: readFileSync(tlsKey!),
} : undefined;
const proxyAgent = enabled
  ? new Agent({ ca: readFileSync(tlsCa!), rejectUnauthorized: true })
  : undefined;

// Vite's default SPA fallback serves /index.html at /features, even if
// dist/features/index.html exists. Resolve prerendered routes before fallback.
// Production proxies must supply equivalent routing: preview is not a deployment.
const marketingPaths = new Set([
  '/features', '/how-it-works', '/integrations', '/pricing', '/faq', '/privacy', '/demo',
]);
const publicHtmlPreview: Plugin = {
  name: 'kpdesk-prerendered-route-preview',
  configurePreviewServer(server) {
    server.middlewares.use((request, response, next) => {
      if (!request.url || !['GET', 'HEAD'].includes(request.method ?? '')) return next();
      const parsed = new URL(request.url, 'http://localhost');
      const path = parsed.pathname.replace(/\/+$/, '') || '/';
      if (path === '/' || path.startsWith('/api/')
        || /\.[^/]+$/.test(path)) return next();
      if (marketingPaths.has(path)) {
        request.url = path + '/index.html' + parsed.search;
      } else if (path.startsWith('/demo/')) {
        request.url = '/demo/index.html' + parsed.search;
      } else if (path === '/admin') {
        request.url = '/admin/index.html' + parsed.search;
        response.setHeader('Cache-Control', 'no-store');
        response.setHeader('X-Robots-Tag', 'noindex, nofollow');
      } else if (path === '/proposal') {
        request.url = '/proposal/index.html' + parsed.search;
        response.setHeader('Cache-Control', 'no-store');
        response.setHeader('X-Robots-Tag', 'noindex, nofollow');
        response.setHeader('Referrer-Policy', 'no-referrer');
      } else if (path === '/app' || path.startsWith('/app/')) {
        request.url = '/app/index.html' + parsed.search;
      } else if (path === '/auth' || path.startsWith('/auth/')) {
        request.url = '/auth/login/index.html' + parsed.search;
      } else {
        const errorPage = resolve(server.config.root, server.config.build.outDir, '404.html');
        response.writeHead(404, {
          'Content-Type': 'text/html; charset=utf-8',
          'X-Robots-Tag': 'noindex, nofollow',
        });
        response.end(readFileSync(errorPage));
        return;
      }
      next();
    });
  },
};

export default defineConfig({
  plugins: [react(), publicHtmlPreview],
  build: {
    manifest: true,
    rollupOptions: {
      input: {
        app: resolve(import.meta.dirname, 'index.html'),
        publicLite: resolve(import.meta.dirname, 'src/public-lite.ts'),
      },
    },
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    https: devHttps,
    proxy: {
      '/api': {
        target: enabled ? 'https://127.0.0.1:8000' : 'http://127.0.0.1:8000',
        changeOrigin: false,
        ...(enabled ? { secure: true, agent: proxyAgent } : {}),
      },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    css: true,
  },
});
