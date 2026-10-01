import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { api, LIVE } from './api/backend';
import { applyInfra } from './core/section';
import './styles.css';

const root = createRoot(document.getElementById('root'));

/** С бэкендом участок берётся с сервера (GET /infra) — до того, как поднимется движок и интерфейс. */
async function boot() {
  if (LIVE) {
    try {
      const infra = await api.infra();
      if (infra?.stations?.length) applyInfra(infra);
    } catch (err) {
      console.error('Сервер недоступен — участок не загружен', err);
    }
  }
  const { default: App } = await import('./App');
  root.render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}
void boot();
