/**
 * Клиент бэкенда (§16 ТЗ): REST для команд и начальных данных, WebSocket для потока событий шины.
 * Режим включается в .env: VITE_DATA_SOURCE=live. Без него интерфейс работает на встроенном движке (демо).
 */
export const LIVE = import.meta.env.VITE_DATA_SOURCE === 'live';
const API = import.meta.env.VITE_API_BASE ?? '/api';
const WS_PATH = import.meta.env.VITE_WS_PATH ?? '/ws';
const TOKEN_KEY = 'autodispatcher.token';

const token = () => {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
};

async function call(method, path, body) {
  const t = token();
  const res = await fetch(`${API}${path}`, {
    method,
    headers: { 'Content-Type': 'application/json', ...(t ? { Authorization: `Bearer ${t}` } : {}) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  return { status: res.status, ok: res.status < 300, data };
}

const list = (d, key) => (Array.isArray(d) ? d : (d?.[key] ?? []));

export const api = {
  async infra() {
    return (await call('GET', '/infra')).data;
  },
  async trains() {
    return list((await call('GET', '/trains')).data, 'trains');
  },
  async plan() {
    return (await call('GET', '/plan/current')).data;
  },
  async variants() {
    return (await call('GET', '/plan/variants')).data;
  },
  async incidents() {
    return list((await call('GET', '/incidents')).data, 'incidents');
  },
  async scenarios() {
    return list((await call('GET', '/scenarios')).data, 'scenarios');
  },
  /** Вход на сервер (JWT, §16.1). Если авторизации на сервере ещё нет — работаем без токена. */
  async login(login, password) {
    const r = await call('POST', '/auth/login', { login, password });
    const tk = r.data?.access_token ?? r.data?.token;
    if (r.ok && tk) {
      try {
        localStorage.setItem(TOKEN_KEY, tk);
      } catch {
        /* токен не сохранится между перезагрузками */
      }
    }
    return { ok: r.ok || r.status === 404, role: r.data?.role };
  },
  apply: (variantId, baseVersion) => call('POST', '/plan/apply', { variant_id: variantId, base_plan_version: baseVersion }),
  reject: (variantId) => call('POST', `/plan/variants/${encodeURIComponent(variantId)}/reject`),
  replan: () => call('POST', '/plan/replan'),
  async whatif(modifications) {
    return (await call('POST', '/whatif', { modifications })).data?.request_id ?? null;
  },
  promoteWhatIf: (id) => call('POST', `/whatif/${encodeURIComponent(id)}/promote`),
  async ato(trainId) {
    return list((await call('GET', `/ato/${encodeURIComponent(trainId)}`)).data, 'profiles');
  },
  clock: (paused, speed) => call('POST', '/sim/clock', { paused, speed }),
  createIncident: (incident) => call('POST', '/incidents', incident),
  resolveIncident: (id) => call('POST', `/incidents/${encodeURIComponent(id)}/resolve`),
  estimate: (id, minS, maxS) => call('POST', `/incidents/${encodeURIComponent(id)}/estimate`, { est_min_s: minS, est_max_s: maxS }),
  runScenario: (id) => call('POST', `/scenarios/${encodeURIComponent(id)}/run`),
};

/**
 * Поток событий: при подключении сервер шлёт снимок, дальше — конверты шины (§15.2).
 * Обрыв — переподключение с паузой; снимок при переподключении восстанавливает всё состояние.
 */
export function connectStream(onEnvelope, onStatus) {
  let ws = null;
  let stopped = false;
  let retry = 0;
  const open = () => {
    onStatus('connecting');
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const t = token();
    ws = new WebSocket(`${proto}://${location.host}${WS_PATH}${t ? `?token=${encodeURIComponent(t)}` : ''}`);
    ws.onopen = () => {
      retry = 0;
      onStatus('online');
      ws.send(JSON.stringify({ op: 'subscribe', channels: ['#'] }));
    };
    ws.onmessage = (m) => {
      try {
        const msg = JSON.parse(m.data);
        // Снимок может прийти и конвертом, и просто объектом (§16.3).
        onEnvelope(typeof msg.type === 'string' && 'payload' in msg ? msg : { type: 'snapshot', ts_wall: Date.now() / 1000, payload: msg });
      } catch (err) {
        console.error('Некорректное сообщение сервера', err);
      }
    };
    ws.onclose = () => {
      onStatus('offline');
      if (!stopped) setTimeout(open, Math.min(10000, 500 * 2 ** retry++));
    };
  };
  open();
  return {
    close() {
      stopped = true;
      ws?.close();
    },
    /** Фронт сообщает серверу p95 задержки отрисовки (§16.3). */
    reportLatency(p95) {
      if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ op: 'latency', p95_ms: Math.round(p95) }));
    },
  };
}
