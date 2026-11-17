// NovelCraft 前端 Vite 配置（占位骨架）
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // 说明：项目目录可能经 junction/符号链接挂载，proxy 目标按实际部署调整，
  // vite 会把 root 解析成 E 盘物理路径，导致 dev transform 失效 / build 报绝对路径错误。
  // preserveSymlinks 让 vite 保留 C 盘逻辑路径做 root-relative 判断。
  resolve: {
    preserveSymlinks: true,
  },
  server: {
    port: 5173,
    // 本地开发时把 /api 请求代理到 FastAPI 后端
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true
      }
    }
  }
});
