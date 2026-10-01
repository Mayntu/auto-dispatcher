/**
 * Работа с бэкендом команды (docs/frontend-contract.md). Движок перестаёт считать сам и становится «зеркалом»
 * сервера: состояние приходит потоком WS (snapshot + конверты), действия интерфейса уходят командами в REST.
 * Компоненты читают то же состояние движка, что и в демо.
 */
import {
  baselineFromTimetable,
  disruptionFromSpec,
  factFromSpec,
  fieldFromSpec,
  indexFromSpec,
  journalFromSpec,
  planFromSpec,
  SPEC_TYPE,
  trainFromSpec,
  variantsFromSpec,
  whatIfFromSpec,
} from '../api/adapter';
import { api, connectStream } from '../api/backend';
import { m } from '../i18n/msg';
import { SECTION } from '../core/section';
import { shownIndex } from './engine';

let uidSeq = 0;
const uid = (p) => `${p}${Date.now().toString(36)}${(++uidSeq).toString(36)}`;
const LATENCY_REPORT_MS = 5000;
const FACT_STEP_S = 15;
const FACT_WINDOW_S = 3 * 3600;

export function startLive(engine) {
  engine.stop();
  const trains = (SECTION.server?.timetable ?? []).map(trainFromSpec);
  const baseline = baselineFromTimetable(trains);
  const latency = [];
  let fact = {};
  let incidentsKey = '';
  const journalSeen = new Set();

  const st = () => engine.getState();
  const ctx = () => ({ trains, baseline, prev: st().plan, disruptions: st().disruptions });

  engine.set({ trains, baseline, connection: 'connecting', liveMode: true });

  // ───────────── входящий поток ─────────────
  const setPlan = (plan) => {
    const p = planFromSpec(plan, trains);
    if (p) engine.set({ plan: p, conflicts: [] });
  };
  const setIndex = (k) => {
    if (!k) return;
    engine.set({
      index: indexFromSpec(k.index),
      kpi: k.kpi,
      planBroken: !!k.plan_broken,
      forecastOk: k.forecast_ok !== false,
      metrics: { ...st().metrics, lastReplanMs: k.last_solve_ms ?? st().metrics.lastReplanMs },
    });
  };
  /** planner.variants заменяет пачку целиком; пустая пачка — «Активных решений нет». */
  const setVariants = (payload) => {
    if (!payload) return engine.set({ pending: undefined });
    const variants = variantsFromSpec(payload, ctx());
    const prev = st().pending;
    if (!variants.length) {
      // Все карточки устарели без решения — сервер сам пересчитывает (§27.5), показываем «считаем».
      // Если хоть одна применена или отклонена, остальные тоже помечаются stale: решение принято, карточки уходят в журнал.
      const list = payload.variants ?? [];
      const stale = list.length > 0 && list.every((v) => v.status === 'stale');
      return engine.set({ pending: stale ? { ...(prev ?? {}), id: prev?.id ?? uid('R'), status: 'computing', variants: [], disruptionIds: payload.incident_ids ?? [] } : undefined });
    }
    const sameBatch = prev?.status === 'ready' && variants.every((v) => prev.variants.some((x) => x.id === v.id));
    engine.set({
      pending: {
        id: sameBatch ? prev.id : uid('R'),
        createdAt: sameBatch ? prev.createdAt : st().now,
        status: 'ready',
        variants,
        disruptionIds: payload.incident_ids ?? [],
        basePlanVersion: payload.base_plan_version,
        solveMs: payload.solve_ms,
        notice: sameBatch ? prev.notice : undefined,
      },
      metrics: { ...st().metrics, lastVariantsMs: payload.solve_ms ?? st().metrics.lastVariantsMs },
    });
  };
  /** Активные сбои — из field.state (источник правды); пересобираем, только если список изменился. */
  const setIncidents = (list) => {
    const key = JSON.stringify(list ?? []);
    if (key === incidentsKey) return;
    incidentsKey = key;
    engine.set({ disruptions: (list ?? []).map((d) => disruptionFromSpec(d, st().now)).filter(Boolean) });
  };
  const addJournal = (entries) => {
    const fresh = entries.filter((e) => e && !journalSeen.has(e.id));
    if (!fresh.length) return;
    for (const e of fresh) journalSeen.add(e.id);
    const rows = fresh.map(journalFromSpec).sort((a, b) => b.t - a.t);
    const applied = fresh
      .filter((e) => e.kind === 'variant_applied')
      .map((e) => ({ id: e.id, t: rows.find((r) => r.id === e.id).t, title: e.text, strategy: e.strategy, indexBefore: shownIndex(st()).value, indexAfter: shownIndex(st()).value, weightedDelayMin: 0, by: 'Сервер', auto: false }));
    engine.set({ log: [...rows, ...st().log].slice(0, 800), decisions: [...applied, ...st().decisions] });
  };
  /** Нитка «факт» ГИД: снимок сервера + точки из field.state (тот же шаг, что у сервера). */
  const trackFact = (f) => {
    let changed = false;
    for (const tr of f.trains) {
      const arr = fact[tr.trainId] ?? (fact[tr.trainId] = []);
      const last = arr[arr.length - 1];
      if (!last || f.t - last[0] >= FACT_STEP_S || (tr.km !== last[1] && f.t > last[0])) {
        arr.push([f.t, tr.km]);
        while (arr.length && arr[0][0] < f.t - FACT_WINDOW_S) arr.shift();
        changed = true;
      }
    }
    if (changed) engine.set({ factTrack: { ...fact } });
  };
  const onField = (p) => {
    const f = fieldFromSpec(p);
    engine.lastReal = performance.now();
    setIncidents(p.incidents);
    engine.set({
      now: f.t,
      running: !f.paused,
      speed: f.speed ?? st().speed,
      live: f,
      connection: 'online',
      safetyViolations: f.safetyViolations,
    });
    trackFact(f);
    engine.bus.emit('tick', f.t);
  };

  const onMessage = (env) => {
    const p = env.payload;
    if (env.ts_wall) {
      const ms = Date.now() - env.ts_wall * 1000;
      if (ms >= 0 && ms < 60000) {
        latency.push(ms);
        if (latency.length > 200) latency.shift();
      }
    }
    switch (env.type) {
      case 'snapshot':
        fact = factFromSpec(p.fact);
        engine.set({ factTrack: { ...fact } });
        if (p.plan) setPlan(p.plan);
        setIndex(p.index);
        setVariants(p.variants);
        addJournal(p.journal ?? []);
        if (p.field) onField(p.field);
        return;
      case 'field.state':
        onField(p);
        return;
      case 'kpi.index':
        setIndex(p);
        return;
      case 'plan.approved':
      case 'plan.refreshed':
        setPlan(p);
        return;
      case 'planner.variants':
        setVariants(p);
        return;
      case 'journal.entry':
        addJournal([p]);
        return;
      case 'incident.created':
        // Пока сервер считает варианты — диспетчер видит «Подбираю варианты…».
        if (!st().pending) engine.set({ pending: { id: uid('R'), createdAt: st().now, status: 'computing', variants: [], disruptionIds: [p.id] } });
        return;
      case 'planner.metrics':
        if (p.solve_ms !== undefined) engine.set({ metrics: { ...st().metrics, lastVariantsMs: p.solve_ms } });
        return;
      case 'safety.violation':
        engine.log('crit', m('Нарушение безопасности: {d}', { d: typeof p === 'string' ? p : JSON.stringify(p) }), 'Безопасность');
        return;
      default:
    }
  };

  // ───────────── команды интерфейса → сервер ─────────────
  const ok = (r, what) => {
    if (!r.ok) engine.log('warn', m('Сервер отклонил «{what}» ({code}): {detail}', { what: m(what), code: r.status, detail: r.detail ?? '' }), 'Сервер');
    return r.ok;
  };
  const unsupported = (what) => engine.log('warn', m('«{what}» пока нет в API сервера (docs/backend-issues.md)', { what: m(what) }), 'Сервер');
  const commands = {
    async applyVariant(variantId) {
      if (!engine.allowed('section', 'утверждение плана')) return;
      const s = st();
      const v = s.pending?.variants.find((x) => x.id === variantId);
      if (!v) return;
      const r = await api.apply(variantId, v.basePlanVersion ?? s.pending.basePlanVersion);
      if (r.status === 409) {
        // Вариант устарел — сервер уже пересчитывает варианты (§12.1), это нормально.
        if (st().pending) engine.set({ pending: { ...st().pending, notice: r.detail ?? m('Вариант устарел — план уже изменился, пересчитываю варианты…') } });
        return;
      }
      ok(r, 'утверждение плана');
    },
    async rejectVariant(variantId) {
      if (engine.allowed('section', 'отклонение варианта')) ok(await api.reject(variantId), 'отклонение варианта');
    },
    async inject(specs) {
      if (!engine.allowed('scenario', 'создание события')) return;
      for (const sp of specs) {
        const g = sp.segment !== undefined ? SECTION.segments[sp.segment] : null;
        const from = g ? SECTION.stations[g.from].km : null;
        const at = sp.pos ?? (g ? g.length / 2 : 0);
        const onSeg = g && sp.kind !== 'breakdown' && sp.kind !== 'delay';
        ok(
          await api.createIncident({
            type: SPEC_TYPE[sp.kind],
            segment_id: onSeg ? g.specId : null,
            km: onSeg ? Math.round((from + (g.reversed ? g.length - at : at) / 1000) * 100) / 100 : null,
            train_id: sp.trainId ?? null,
            est_min_min: sp.minMin,
            est_max_min: sp.maxMin,
            ...(sp.note ? { description: sp.note } : {}),
          }),
          'создание события',
        );
      }
    },
    /** 8 сбоев подряд (сценарий проверки 8): сценариев в API нет, поэтому восемь POST /api/incidents. */
    async massDisruptions(n = 8) {
      if (!engine.allowed('scenario', 'вброс событий')) return;
      const running = (st().live?.trains ?? []).filter((t) => t.segment !== undefined && t.status === 'running').map((t) => t.trainId);
      const specs = [];
      for (let i = 0; i < n; i++) {
        const lo = 5 + Math.floor(Math.random() * 10);
        const hi = lo + 5 + Math.floor(Math.random() * 15);
        if (i % 3 === 2 && running.length) specs.push({ kind: 'breakdown', trainId: running.splice(Math.floor(Math.random() * running.length), 1)[0], minMin: lo, maxMin: hi });
        else specs.push({ kind: i % 2 ? 'closure' : 'livestock', segment: Math.floor(Math.random() * SECTION.segments.length), minMin: lo, maxMin: hi });
      }
      engine.log('warn', m('Массовый вброс: {n} нештатных ситуаций', { n: specs.length }), engine.actor());
      await commands.inject(specs);
    },
    refine() {
      unsupported('уточнение длительности сбоя');
    },
    async resolve(id) {
      if (engine.allowed('scenario', 'отметка об устранении')) ok(await api.resolveIncident(id), 'отметка об устранении');
    },
    async setRunning(running) {
      if (engine.allowed('scenario', 'управление временем')) ok(await api.clock({ paused: !running }), 'управление временем');
    },
    async setSpeed(speed) {
      if (engine.allowed('scenario', 'управление временем')) ok(await api.clock({ paused: false, speed }), 'управление временем');
    },
    async askVariants() {
      if (!engine.allowed('section', 'запрос вариантов') || st().pending) return;
      if (ok(await api.replan(), 'запрос вариантов') && !st().pending)
        engine.set({ pending: { id: uid('R'), createdAt: st().now, status: 'computing', variants: [], disruptionIds: [] } });
    },
    reset() {
      unsupported('сброс участка');
    },
    whatIfOpen() {
      if (!engine.allowed('section', 'what-if моделирование')) return;
      engine.set({ whatIf: { openedAt: st().now, mods: { speed: {}, priority: {}, cancelled: {}, durationMin: {} } } });
    },
    whatIfClose() {
      engine.set({ whatIf: undefined });
    },
    whatIfSetMods(mods) {
      if (!st().whatIf || !engine.allowed('section', 'what-if моделирование')) return;
      engine.set({ whatIf: { ...st().whatIf, mods, result: undefined, error: undefined } });
      clearTimeout(commands.whatIfTimer);
      // Пользователь вводит значения — запрос уходит, когда он остановился.
      commands.whatIfTimer = setTimeout(async () => {
        // Сервер (MVP) понимает скорость поезда (км/ч) и длительность сбоя (мин).
        const modifications = [
          ...Object.entries(mods.speed).filter(([, v]) => v >= 10).map(([id, v]) => ({ kind: 'train_speed', target_id: id, value: v })),
          ...Object.entries(mods.durationMin).map(([id, v]) => ({ kind: 'incident_duration', target_id: id, value: v })),
        ];
        if (Object.keys(mods.priority).length || Object.values(mods.cancelled).some(Boolean)) unsupported('приоритет и отмена поезда в what-if');
        if (!modifications.length) return;
        const r = await api.whatif(modifications);
        if (!st().whatIf || st().whatIf.mods !== mods) return;
        if (r.ok) engine.set({ whatIf: { ...st().whatIf, result: whatIfFromSpec(r.data, ctx()) } });
        else ok(r, 'what-if моделирование');
      }, 400);
    },
    whatIfApply() {
      unsupported('перенос what-if в работу');
    },
    updateSettings() {
      unsupported('изменение настроек индекса');
    },
  };
  for (const [name, fn] of Object.entries(commands)) if (typeof fn === 'function') engine[name] = fn;

  // ───────────── поток ─────────────
  const stream = connectStream(onMessage, (connection) => engine.set({ connection }));
  setInterval(() => {
    if (!latency.length) return;
    const sorted = [...latency].sort((a, b) => a - b);
    const p95 = sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * 0.95))];
    engine.set({ latencyP95: p95 });
    stream.reportLatency(p95);
  }, LATENCY_REPORT_MS);
}
