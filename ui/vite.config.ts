import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Один файлик orb.js → подключаем в index.html как <script src="/static/orb.js">.
// IIFE-глобал: window.__BertaOrb__ (setSignal).
export default defineConfig({
  root: ".",
  base: "./",
  plugins: [react()],
  // orb-ui тянет свою копию react из libs/orb-ui/node_modules — без dedupe
  // в бандле оказывается две React, dispatcher не тот → хуки падают
  resolve: {
    dedupe: ["react", "react-dom", "react/jsx-runtime", "react-dom/client"],
  },
  define: {
    "global": "globalThis",
    // без этого в бандле остаётся process.env.NODE_ENV → в браузере
    // «process is not defined» и орб не рендерится
    "process.env.NODE_ENV": JSON.stringify("production"),
  },
  build: {
    rollupOptions: {
      // вшиваем react + orb-ui в бандл (не external)
      external: [],
      output: {
        assetFileNames: "[name].[ext]",
        chunkFileNames: "[name].[ext]",
      },
    },
    lib: {
      entry: "src/main.jsx",
      name: "BertaOrb",
      fileName: () => "orb.js",
      formats: ["iife"],
    },
    outDir: "dist",
    emptyOutDir: false,
    sourcemap: false,
    assetsDir: ".",
  },
});
