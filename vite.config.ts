import { defineConfig } from 'vite'
import { loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const backendHost = env.SERVER_HOST || '127.0.0.1'
  const backendPort = env.SERVER_PORT || env.PORT || '8765'
  const backendOrigin = `http://${backendHost}:${backendPort}`

  return {
    plugins: [react()],
    server: {
      proxy: {
        '/ws': {
          target: backendOrigin,
          ws: true,
          changeOrigin: true,
        },
        '/health': {
          target: backendOrigin,
          changeOrigin: true,
        },
      },
    },
  }
})