import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { api, LIVE } from './api/backend';
import { setEpoch } from './api/adapter';
import { applyInfra } from './core/section';
import './styles.css';

const root = createRoot(document.getElementById('root'));
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

/** С бэкендом участок берётся с сервера (GET /api/infra) — до того, как поднимется движок и интерфейс. */
async function boot() {
  if (LIVE) {
    for (let attempt = 0; ; attempt++) {
      const res = await api.infra();
      if (res?.infra?.stations?.length) {
        setEpoch(res.sim_epoch);
        applyInfra(res);
        break;
      }
      root.render(<div className="boot-wait">Нет связи с сервером — повторяю подключение… ({attempt + 1})</div>);
      await wait(Math.min(5000, 500 * 2 ** attempt));
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
