// 景区社媒采集系统 · Copyright (c) 2026 keanying
// SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
// 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
// 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
// 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

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
