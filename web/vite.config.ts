import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `npm run dev` proxies /api to the read-only API (python -m jevtrade.api).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": process.env.JEVTRADE_API ?? "http://127.0.0.1:8000" },
  },
});
