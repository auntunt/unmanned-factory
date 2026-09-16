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
      resolveId(source, importer) {
        if (importer && /workspace\/api$/.test(source) && !importer.includes('preview/api-mock')) return MOCK
        return null
      },
    },
    react(),
  ],
  server: { port: 5200, open: false, hmr: { overlay: true } },
})
