import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8796",
      "/ws": {
        target: "ws://127.0.0.1:8796",
        ws: true,
      },
    },
  },
  build: {
    outDir: fileURLToPath(new URL("../sz002796/web_assets", import.meta.url)),
    emptyOutDir: true,
  },
});
