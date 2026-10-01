/**
 * Клиент бэкенда (docs/frontend-contract.md): REST для команд и начальных данных, WebSocket для потока событий.
 * Режим включается в .env: VITE_DATA_SOURCE=live. Без него интерфейс работает на встроенном движке (демо).
 * Авторизации в бэкенде MVP нет — вход на фронте локальный.
 */
export const LIVE = import.meta.env.VITE_DATA_SOURCE === 'live';
const API = import.meta.env.VITE_API_BASE ?? '/api';
const WS_PATH = import.meta.env.VITE_WS_PATH ?? '/ws';

/** Ответ REST: статус, данные и текст ошибки сервера (FastAPI кладёт его в detail). */
async function call(method, path, body) {
  let res;
  try {
    res = await fetch(`${API}${path}`, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (err) {
    return { status: 0, ok: false, data: null, detail: String(err) };
  }
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  const d = data?.detail;
  const detail = typeof d === 'string' ? d : Array.isArray(d) ? d.map((x) => x.msg).join('; ') : null;
  return { status: res.status, ok: res.status < 300, data, detail };
}

const get = async (path) => {
  const r = await call('GET', path);
  return r.ok ? r.data : null;
};

export const api = {
  /** {infra, categories, timetable, sim_epoch, thresholds, station_order, intervals, segment_times} */
  infra: () => get('/infra'),
  /** {field, plan, variants, index, fact, journal} — то же, что WS snapshot. */
  state: () => get('/state'),
  apply: (variantId, baseVersion) => call('POST', '/plan/apply', { variant_id: variantId, base_plan_version: baseVersion }),
  reject: (variantId) => call('POST', `/plan/variants/${encodeURIComponent(variantId)}/reject`),
  replan: () => call('POST', '/plan/replan'),
  /** What-if считается синхронно (≤ 3 с), ответ — WhatIfResult. */
  whatif: (modifications) => call('POST', '/whatif', { modifications }),
  /** SpeedProfile на текущий/ближайший перегон → список (карточка поезда умеет рисовать несколько). */
  async ato(trainId) {
    const r = await call('GET', `/ato/${encodeURIComponent(trainId)}`);
    return r.ok && r.data ? (Array.isArray(r.data) ? r.data : [r.data]) : [];
  },
  clock: (body) => call('POST', '/sim/clock', body),
  /** {type: obstacle|train_failure|segment_closed, segment_id?, train_id?, km?, est_min_min, est_max_min, description?} */
  createIncident: (body) => call('POST', '/incidents', body),
  resolveIncident: (id) => call('POST', `/incidents/${encodeURIComponent(id)}/resolve`),
  // ── ручное изменение времени на ГИД и указания диспетчера (tasks/02-manual-drag-*, API ещё не в бэкенде) ──
  manualBounds: (trainId, stationId) => call('GET', `/plan/manual/bounds?train_id=${encodeURIComponent(trainId)}&station_id=${encodeURIComponent(stationId)}`),
  manualPreview: (req) => call('POST', '/plan/manual/preview', req),
  manualCommit: (req) => call('POST', '/plan/manual/commit', req),
  async pins() {
    const d = await get('/plan/pins');
    return Array.isArray(d) ? d : (d?.pins ?? []);
  },
  deletePin: (id) => call('DELETE', `/plan/pins/${encodeURIComponent(id)}`),
};

/**
 * Поток событий: при подключении сервер шлёт {type: 'snapshot', payload}, дальше — конверты шины (§15.2).
 * Обрыв — переподключение с паузой; снимок при переподключении восстанавливает весь экран.
 */
export function connectStream(onMessage, onStatus) {
  let ws = null;
  let stopped = false;
  let retry = 0;
  const open = () => {
    onStatus('connecting');
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${location.host}${WS_PATH}`);
    ws.onopen = () => {
      retry = 0;
      onStatus('online');
    };
    ws.onmessage = (m) => {
      let msg;
      try {
        msg = JSON.parse(m.data);
      } catch (err) {
        console.error('Некорректное сообщение сервера', err);
        return;
      }
      try {
        onMessage(msg);
      } catch (err) {
        console.error(`Ошибка обработки «${msg?.type}»`, err);
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
    /** Фронт сообщает серверу p95 задержки доставки (§16.3). */
    reportLatency(p95) {
      if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ op: 'latency', p95_ms: Math.round(p95) }));
    },
  };
}
