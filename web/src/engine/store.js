import { useEffect, useState, useSyncExternalStore } from 'react';
import { Engine } from './engine';
import { startSync } from './sync';
import { LIVE } from '../api/backend';
import { startLive } from './live';

export const engine = new Engine();
// Для отладки в консоли браузера (только dev-сборка).
if (import.meta.env.DEV) window.__engine = engine;
// С бэкендом каждая вкладка подключается к серверу сама; без него экраны синхронизируются между собой.
if (LIVE) startLive(engine);
else startSync(engine);

export function useEngineState() {
  return useSyncExternalStore(engine.subscribe, engine.getState);
}

/**
 * Модельное время для анимации: движок шагает 4 раза в секунду, а между шагами время продлевается
 * от точного момента последнего шага по реальным часам — поезда едут плавно, без рывков.
 */
export function useSmoothNow() {
  const [t, setT] = useState(() => engine.getState().now);

  useEffect(() => {
    let id;
    let last = -Infinity;
    let lastNow = engine.getState().now;
    const loop = () => {
      const st = engine.getState();
      // С сервером время идёт с эффективной скоростью (×1, пока ждём решения диспетчера).
      const speed = st.live?.effectiveSpeed ?? st.speed;
      const ahead = st.running ? Math.min(1, (performance.now() - engine.lastReal) / 1000) * speed : 0;
      let v = st.now + ahead;
      // Время не идёт назад, кроме явного сброса или перемотки (скачок больше минуты).
      if (v < last && Math.abs(st.now - lastNow) < 60) v = last;
      last = v;
      lastNow = st.now;
      setT(v);
      id = requestAnimationFrame(loop);
    };
    id = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(id);
  }, []);

  return t;
}
