import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "node:path";

// Built into web/dist and served by the API under /screen/ (assets), with
// GET /wall -> dist/index.html and GET /panel/{serial} -> dist/panel.html.
export default defineConfig({
  base: "/screen/",
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/wall/state": "http://127.0.0.1:8000",
      "/admin": "http://127.0.0.1:8000",
      "/panels": "http://127.0.0.1:8000",
      "/panel-api": "http://127.0.0.1:8000",
      "/healthz": "http://127.0.0.1:8000",
    },
  },
  build: {
    outDir: "dist",
    chunkSizeWarningLimit: 2500,
    rollupOptions: {
      input: {
        index: resolve(__dirname, "index.html"),
        panel: resolve(__dirname, "panel.html"),
      },
    },
  },
});
