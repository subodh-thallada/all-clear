import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { viteSingleFile } from 'vite-plugin-singlefile';

// `npm run build` writes one self-contained web/dist/index.html: scripts, styles
// and fonts are inlined. The API serves that file at GET /wall, so the screen
// needs nothing else from /screen/ and nothing from the network.
//
// `npm run dev` proxies the API on 127.0.0.1:8000, so the dev server can show
// the real run with ?source=live.
export default defineConfig({
  plugins: [react(), viteSingleFile()],
  server: {
    port: 5173,
    proxy: {
      '/wall/state': 'http://127.0.0.1:8000',
      '/admin': 'http://127.0.0.1:8000',
      '/healthz': 'http://127.0.0.1:8000',
    },
  },
});
