import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'
const MOCK = path.resolve(__dirname, 'preview/api-mock.ts')
export default defineConfig({
  root: path.resolve(__dirname),
  plugins: [
    {
      name: 'preview-api-mock',
      enforce: 'pre',
      configureServer(server) {
        server.middlewares.use('/api', (_req, res) => {
          res.statusCode = 405
          res.setHeader('Content-Type', 'text/plain; charset=utf-8')
          res.end('这是只读界面预览，未连接真实服务；下载与执行请使用正式工作台。')
        })
      },
      transformIndexHtml() {
        return [{ tag: 'aside', attrs: { role: 'status', style: 'padding:8px 16px;background:#fff4d6;color:#654e20;text-align:center;font:13px system-ui' }, children: '界面预览 · 所有数据均为示例，提交与保存不会执行。', injectTo: 'body-prepend' }]
      },
      resolveId(source, importer) {
        if (importer && /workspace\/api$/.test(source) && !importer.includes('preview/api-mock')) return MOCK
        return null
      },
    },
    react(),
  ],
  server: { port: 5200, open: false, hmr: { overlay: true } },
})
