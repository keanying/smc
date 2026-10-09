import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import path from 'node:path'

// 开发时前端跑 5173，接口和 WebSocket 都转发到后端 8000。
// 生产构建产物会被后端直接托管（app/main.py 里 mount 了 frontend/dist），
// 所以打包出来的前端不需要知道后端地址，走同源相对路径即可。
const BACKEND = process.env.VITE_BACKEND || 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': path.resolve(__dirname, 'src') },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: BACKEND, changeOrigin: true },
      '/ws': { target: BACKEND, ws: true, changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    chunkSizeWarningLimit: 1500,
  },
})
