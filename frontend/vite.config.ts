import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The backend serves the built bundle from src/tsclaude/web/static (never committed; CI builds it).
const outDir = fileURLToPath(new URL("../src/tsclaude/web/static", import.meta.url));

// `pnpm dev` proxies reads to a running `tsclaude app` (default port 8765). Mutations from the dev
// server are refused by design: its pages carry no per-launch token. Use `pnpm watch` + the app URL
// for interactive work.
const backend = process.env.TSC_BACKEND ?? "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react()],
  base: "/",
  build: {
    outDir,
    emptyOutDir: true,
    sourcemap: true,
    target: "es2022",
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": { target: backend, changeOrigin: true },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    restoreMocks: true,
  },
});
