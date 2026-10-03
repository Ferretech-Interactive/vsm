import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The build lands inside the Python package so the backend can serve it.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: { outDir: "../vsm/static", emptyOutDir: true },
  server: {
    proxy: { "/api": { target: "http://127.0.0.1:8810", ws: true } },
  },
});
