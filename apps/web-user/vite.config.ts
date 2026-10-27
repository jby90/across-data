import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

export default defineConfig({
  cacheDir: process.env.CYSH_VITE_CACHE_DIR,
  base: process.env.VITE_APP_BASE || '/',
  plugins: [vue()],
  server: {
    proxy: { '/api': process.env.CYSH_DEV_API_TARGET || 'http://127.0.0.1:8000' },
  },
})
