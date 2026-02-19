import { defineConfig, loadEnv } from "vite";
import path from "node:path";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "VITE_");

  // 约定：前端请求走相对路径 /api/v1，开发态用 proxy 转发到后端
  const backend = env.VITE_BACKEND_URL || "http://localhost:8000";

  return {
    // 设计文档里 index.html 放在 src/ 下
    root: path.resolve(__dirname, "src"),
    publicDir: path.resolve(__dirname, "public"),
    base: "./",
    resolve: {
      alias: {
        "@": path.resolve(__dirname, "src"),
      },
    },
    server: {
      port: Number(env.VITE_DEV_PORT || 5173),
      strictPort: true,
      proxy: {
        // 统一代理 /api/* -> 后端
        "/api": {
          target: backend,
          changeOrigin: true,
          secure: false,
        },
        "/healthz": {
          target: backend,
          changeOrigin: true,
          secure: false,
        },
      },
    },
    build: {
      outDir: path.resolve(__dirname, "dist"),
      emptyOutDir: true,
      sourcemap: true,
    },
  };
});