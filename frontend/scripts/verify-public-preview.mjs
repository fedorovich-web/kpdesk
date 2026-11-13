import { preview } from 'vite';

const server = await preview({
  preview: { host: '127.0.0.1', port: 0, strictPort: true },
  logLevel: 'error',
});
try {
  const address = server.httpServer.address();
  if (!address || typeof address === 'string') {
    throw new Error('Vite preview did not provide a TCP port');
  }
  process.env.KPDESK_SMOKE_ORIGIN = 'http://127.0.0.1:' + address.port;
  await import('./verify-public-http.mjs');
  console.log('Production HTML HTTP smoke: PASS');
} finally {
  server.httpServer.closeAllConnections?.();
  await new Promise((resolve, reject) => {
    server.httpServer.close(error => error ? reject(error) : resolve());
  });
}
