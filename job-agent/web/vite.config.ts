import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 开发时：网页在 5173 端口，/api 请求转给后端 8001 端口（FastAPI；本机 8000 被 Docker 里的 Dify 占用）
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://127.0.0.1:8001" } },
});
