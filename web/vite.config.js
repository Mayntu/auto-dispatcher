import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

// Бэкенд команды (FastAPI) — по умолчанию на :8000; dev-сервер проксирует к нему /api и /ws, как nginx в docker-compose.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const target = env.VITE_BACKEND_URL || 'http://localhost:8000';
  return {
    plugins: [react()],
    worker: { format: 'es' },
    server: {
      proxy: {
        '/api': target,
        '/ws': { target: target.replace(/^http/, 'ws'), ws: true },
      },
    },
  };
});
