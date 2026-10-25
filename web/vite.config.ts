import { defineConfig } from "vite";

const apiTarget = process.env.ECON_API_PROXY_TARGET ?? "http://127.0.0.1:8000";

export default defineConfig({
  server: {
    host: true,
    port: 5173,
    proxy: {
      "/world": {
        target: apiTarget,
        changeOrigin: true,
        ws: true,
      },
      "/rounds": apiTarget,
      "/join": apiTarget,
    },
  },
  build: {
    target: "es2022",
  },
});
