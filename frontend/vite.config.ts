import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 开发时前端跑在 5173，API 请求通过 proxy 转发到本地后端 8788。
// 生产环境由 Caddy 做同源反代，前端代码里始终使用相对路径 /api/*。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8788',
        changeOrigin: true,
      },
    },
  },
})
