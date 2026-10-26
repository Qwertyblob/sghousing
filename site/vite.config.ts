import { defineConfig } from "vite";

// The site is served from https://<user>.github.io/<repo>/; SITE_BASE overrides the path
// (for example "/" for a custom domain).
export default defineConfig({
  base: process.env.SITE_BASE ?? "/sghousing/",
  build: {
    target: "es2022",
    // MapLibre and onnxruntime-web are large but load only on their own pages.
    chunkSizeWarningLimit: 1200,
  },
  // onnxruntime-web finds its .wasm next to its own module; pre-bundling would move it.
  optimizeDeps: { exclude: ["onnxruntime-web"] },
});
