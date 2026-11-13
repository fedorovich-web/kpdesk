import { resolve } from 'node:path';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
// Deliberately separate build output. No production import or feature switch.
export default defineConfig({
  plugins: [react()],
  base: '/__e2e/',
  build: { outDir: 'dist-e2e', emptyOutDir: true,
    rollupOptions: { input: [resolve(__dirname, 'e2e/large-review/index.html'),
      resolve(__dirname, 'e2e/universal/index.html')] },
  },
});
