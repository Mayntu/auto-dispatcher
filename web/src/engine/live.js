/**
 * Работа с бэкендом команды. Движок перестаёт считать сам и становится «зеркалом» сервера — как второй экран
 * зеркалит ведущий: состояние приходит потоком событий, действия интерфейса уходят командами в REST.
 * Компоненты при этом не меняются: они читают то же состояние движка.
 */
import {
  baselineFromTimetable,
  disruptionFromSpec,
  fieldFromSpec,
  indexFromSpec,
  planFromSpec,
  setEpoch,
  SPEC_TYPE,
  toServer,
  toUi,
  trainFromSpec,
  variantsFromSpec,
  whatIfFromSpec,
  boundsFromSpec,
  pinFromSpec,
  previewFromSpec,
} from '../api/adapter';
import { fmtHM } from '../core/time';
import { api, connectStream } from '../api/backend';
import { m } from '../i18n/msg';
import { SECTION } from '../core/section';
import { shownIndex } from './engine';

const LEVEL = { true: 'info', false: 'warn' };
let uidSeq = 0;
const uid = (p) => `${p}${Date.now().toString(36)}${(++uidSeq).toString(36)}`;

export function startLive(engine) {
  engine.stop();
  let trains = [];
  let baseline = null;
  let whatIfTimer = null;
  let whatIfRequest = null;
  const latency = [];
  let manualBase = null;
  const fact = {};

  const st = () => engine.getState();
  const ctx = () => ({ trains, baseline, prev: st().plan, disruptions: st().disruptions });

  // ───────────── входящий поток ─────────────
  const setTrains = (specTrains) => {
    trains = specTrains.map(trainFromSpec);
    baseline = baselineFromTimetable(trains);
    engine.set({ trains, baseline });
  };
  const setPlan = (plan) => {
    const p = planFromSpec(plan, trains);
    if (p) engine.set({ plan: p, conflicts: [], metrics: { ...st().metrics, lastReplanMs: plan.solve_ms ?? 0 } });
    // Указания диспетчера приходят вместе с планом (§6.4).
    if (plan?.pins) engine.set({ pins: plan.pins.map(pinFromSpec) });
    return p;
  };
  const setVariants = (payload) => {
    if (!payload) return;
    const variants = variantsFromSpec(payload, ctx());
    if (!variants.length) {
      engine.set({ pending: undefined });
      return;
    }
    const prev = st().pending;
    engine.set({
      pending: {
        id: prev?.status === 'ready' && prev.variants.every((v) => variants.some((x) => x.id === v.id)) ? prev.id : uid('R'),
        createdAt: prev?.createdAt ?? st().now,
        status: 'ready',
        variants,
        disruptionIds: payload.incident_ids ?? [],
        basePlanVersion: payload.base_plan_version,
        solveMs: payload.solve_ms,
      },
      metrics: { ...st().metrics, lastVariantsMs: payload.solve_ms ?? st().metrics.lastVariantsMs },
    });
  };
  const upsertIncident = (d) => {
    const x = disruptionFromSpec(d, st().now);
    if (!x) return null;
    engine.set({ disruptions: [...st().disruptions.filter((y) => y.id !== x.id), x] });
    return x;
  };

  const onEnvelope = (env) => {
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
        if (p.sim_epoch) setEpoch(p.sim_epoch);
        if (p.trains) setTrains(p.trains);
        if (p.plan) setPlan(p.plan);
        if (p.index) engine.set({ index: indexFromSpec(p.index) });
        if (p.incidents) for (const d of p.incidents) upsertIncident(d);
        if (p.variants !== undefined) setVariants(p.variants);
        if (p.scenarios) engine.set({ scenarios: p.scenarios });
        if (p.field) onEnvelope({ type: 'field.state', payload: p.field });
        return;
      case 'field.state': {
        const f = fieldFromSpec(p);
        engine.lastReal = performance.now();
        // Нитка «факт» на ГИД: точка при заметном сдвиге или раз в 20 с модельного времени, за последние 4 ч.
        let changed = false;
        for (const tr of f.trains) {
          const arr = fact[tr.trainId] ?? (fact[tr.trainId] = []);
          const last = arr[arr.length - 1];
          if (!last || f.t - last[0] >= 20 || Math.abs(tr.km - last[1]) > 0.3) {
            arr.push([f.t, tr.km]);
            while (arr.length && arr[0][0] < f.t - 4 * 3600) arr.shift();
            changed = true;
          }
        }
        engine.set({
          now: f.t,
          running: !p.paused,
          speed: p.speed ?? st().speed,
          live: f,
          connection: 'online',
          safetyViolations: Math.max(st().safetyViolations ?? 0, f.safetyViolations),
          ...(changed ? { factTrack: { ...fact } } : {}),
        });
        return;
      }
      case 'kpi.index':
        engine.set({ index: indexFromSpec(p) });
        return;
      case 'plan.approved':
      case 'plan.refreshed': {
        const plan = setPlan(p);
        if (env.type === 'plan.approved' && plan)
          engine.log('ok', m('План v{v} утверждён, пересчитан за {ms} мс', { v: p.version, ms: p.solve_ms ?? 0 }), 'Планировщик');
        return;
      }
      case 'planner.variants':
        setVariants(p);
        if (p.variants?.some((v) => v.status === 'proposed'))
          engine.log('warn', m('Готово вариантов: {n} за {ms} мс', { n: p.variants.filter((v) => v.status === 'proposed').length, ms: Math.round(p.solve_ms ?? 0) }), 'Планировщик');
        return;
      case 'planner.metrics':
        if (p.solve_ms !== undefined) engine.set({ metrics: { ...st().metrics, lastVariantsMs: p.solve_ms } });
        return;
      case 'planner.whatif.result':
        if (st().whatIf && (!whatIfRequest || env.corr_id === whatIfRequest || p.request_id === whatIfRequest))
          engine.set({ whatIf: { ...st().whatIf, result: whatIfFromSpec(p, ctx()) } });
        return;
      case 'incident.created': {
        const d = upsertIncident(p);
        if (d) {
          engine.log('crit', d.description, 'ДЦ');
          // Пока сервер считает варианты — диспетчер видит «Подбираю варианты…».
          if (!st().pending) engine.set({ pending: { id: uid('R'), createdAt: st().now, status: 'computing', variants: [], disruptionIds: [d.id] } });
        }
        return;
      }
      case 'incident.updated':
        upsertIncident(p);
        engine.log('info', m('Уточнено: {d}', { d: p.description }), 'ДЦ');
        return;
      case 'incident.resolved':
        upsertIncident(p);
        engine.log('ok', m('Устранено: {kind}', { kind: p.description }), 'ДЦ');
        return;
      case 'dc.log':
      case 'dc.command_result':
        engine.log(LEVEL[p.ok !== false], p.reason && p.ok === false ? `${p.command}: ${p.reason}` : p.command, p.actor ?? 'ДЦ');
        return;
      case 'planner.pin_violated': {
        const pin = pinFromSpec(p.pin ?? p);
        engine.set({ pins: (st().pins ?? []).map((x) => (x.id === pin.id ? { ...pin, status: 'violated', reason: p.reason } : x)) });
        engine.toast(m('Указание по {n} невыполнимо: {r}', { n: pin.trainId, r: p.reason ?? '' }), 'crit', { pinId: pin.id, trainId: pin.trainId });
        return;
      }
      case 'safety.violation':
        engine.log('crit', m('Нарушение безопасности: {d}', { d: typeof p === 'string' ? p : JSON.stringify(p) }), 'Безопасность');
        engine.set({ safetyViolations: (st().safetyViolations ?? 0) + 1 });
        return;
      default:
    }
  };

  // ───────────── команды интерфейса → сервер ─────────────
  const ok = (r, what) => {
    if (!r.ok) engine.log('warn', m('Сервер отклонил команду «{what}» ({code})', { what: m(what), code: r.status }), 'Сервер');
    return r.ok;
  };
  const commands = {
    async applyVariant(variantId) {
      if (!engine.allowed('section', 'утверждение плана')) return;
      const s = st();
      const v = s.pending?.variants.find((x) => x.id === variantId);
      if (!v) return;
      const before = shownIndex(s).value;
      const r = await api.apply(variantId, v.basePlanVersion ?? s.pending.basePlanVersion);
      if (r.status === 409) {
        // Вариант устарел — сервер уже пересчитывает варианты (§12.1).
        engine.set({ pending: { ...st().pending, notice: m('Вариант устарел — план уже изменился, пересчитываю варианты…') } });
        engine.log('warn', m('Вариант «{title}» устарел', { title: v.title }), 'Планировщик');
        return;
      }
      if (!ok(r, 'утверждение плана')) return;
      const decision = { id: uid('S'), t: s.now, title: v.title, strategy: v.strategy, indexBefore: before, indexAfter: v.index.value, weightedDelayMin: v.weightedDelayMin, by: engine.actor(), auto: false };
      engine.set({ decisions: [decision, ...st().decisions], pending: undefined });
      if (s.pending?.source === 'manual') engine.toast(m('Указание: {d}', { d: s.pending.manual.description }));
      engine.log('ok', m('Утверждён вариант «{title}»: индекс {a} → {b}', { title: v.title, a: before, b: v.index.value }), engine.actor());
    },
    async inject(specs) {
      if (!engine.allowed('scenario', 'создание события')) return;
      for (const sp of specs) {
        const g = sp.segment !== undefined ? SECTION.segments[sp.segment] : null;
        const from = g ? SECTION.stations[g.from].km : null;
        const at = sp.pos ?? (g ? g.length / 2 : 0);
        const type = SPEC_TYPE[sp.kind];
        ok(
          await api.createIncident({
            type,
            segment_id: g?.specId ?? null,
            km: g ? Math.round((from + at / 1000) * 100) / 100 : null,
            train_id: sp.trainId ?? null,
            started_at: sp.start !== undefined ? toServer(sp.start) : toServer(st().now),
            est_min_s: sp.minMin * 60,
            est_max_s: sp.maxMin * 60,
            params: type === 'speed_restriction' ? { speed_kmh: 40, from_m: Math.max(0, at - 1500), to_m: Math.min(g?.length ?? at, at + 1500) } : type === 'train_delay' ? { delay_s: sp.minMin * 60 } : {},
            description: sp.note ?? '',
          }),
          'создание события',
        );
      }
    },
    async massDisruptions() {
      if (engine.allowed('scenario', 'вброс событий')) ok(await api.runScenario('mass_incidents'), 'вброс событий');
    },
    async refine(id, lo, hi) {
      if (engine.allowed('scenario', 'уточнение длительности сбоя')) ok(await api.estimate(id, lo * 60, hi * 60), 'уточнение длительности сбоя');
    },
    async resolve(id) {
      if (engine.allowed('scenario', 'отметка об устранении')) ok(await api.resolveIncident(id), 'отметка об устранении');
    },
    async setRunning(running) {
      if (engine.allowed('scenario', 'управление временем')) ok(await api.clock(!running, st().speed), 'управление временем');
    },
    async setSpeed(speed) {
      if (engine.allowed('scenario', 'управление временем')) ok(await api.clock(false, speed), 'управление временем');
    },
    async askVariants() {
      if (!engine.allowed('section', 'запрос вариантов') || st().pending) return;
      if (ok(await api.replan(), 'запрос вариантов'))
        engine.set({ pending: { id: uid('R'), createdAt: st().now, status: 'computing', variants: [], disruptionIds: [] } });
    },
    // ── ручное изменение времени на ГИД (ТЗ §6) ──
    async manualBounds(trainId, station) {
      const r = await api.manualBounds(trainId, SECTION.stations[station].specId);
      if (!r.ok) {
        engine.toast(r.status === 423 ? r.data?.reason ?? m('Точку менять нельзя') : r.status === 404 ? m('Поезд или пункт не найден в плане') : m('План обновился — повторите действие'), 'warn');
        return null;
      }
      manualBase = r.data.base_plan_version;
      return boundsFromSpec(r.data);
    },
    async manualPreview(req) {
      const r = await api.manualPreview({ base_plan_version: manualBase, train_id: req.trainId, station_id: SECTION.stations[req.station].specId, kind: req.kind, time: toServer(req.time) });
      if (!r.ok) throw new Error(String(r.status));
      return previewFromSpec(r.data, st().plan, trains);
    },
    async manualCommit(req) {
      if (!engine.allowed('section', 'ручное изменение времени')) return;
      const r = await api.manualCommit({ base_plan_version: manualBase, train_id: req.trainId, station_id: SECTION.stations[req.station].specId, kind: req.kind, time: toServer(req.time) });
      if (r.status === 409) {
        engine.toast(m('План обновился — повторите действие'), 'warn');
        return;
      }
      if (!r.ok) throw new Error(String(r.status));
      const variants = variantsFromSpec({ variants: r.data.variants ?? [] }, ctx());
      const p = st().plan.trains[req.trainId]?.stops.find((x) => x.station === req.station);
      const description = m(req.kind === 'dep' ? 'Задержать {n} отправлением со станции {st} до {t}' : 'Поезд {n}: прибытие на {st} в {t}', { n: req.trainId, st: m(SECTION.stations[req.station].short), t: fmtHM(req.time) });
      engine.set({
        pending: {
          id: uid('R'),
          source: 'manual',
          manual: { ...req, from: p ? (req.kind === 'dep' ? p.dep : p.arr) : null, description },
          createdAt: st().now,
          status: 'ready',
          variants,
          disruptionIds: [],
          basePlanVersion: manualBase,
        },
      });
    },
    /** С сервером: фиксируем изменение и сразу применяем вариант «Сохранить порядок». */
    async manualApply(req) {
      await commands.manualCommit(req);
      const p = st().pending;
      if (p?.source !== 'manual' || !p.variants.length) return;
      const v = p.variants.find((x) => x.specStrategy === 'keep_order') ?? p.variants[0];
      await commands.applyVariant(v.id);
    },
    cancelManual() {
      if (st().pending?.source === 'manual') engine.set({ pending: undefined });
    },
    async removePin(id) {
      if (!engine.allowed('section', 'снятие указания')) return;
      const pin = (st().pins ?? []).find((x) => x.id === id);
      if (ok(await api.deletePin(id), 'снятие указания')) {
        engine.set({ pins: (st().pins ?? []).filter((x) => x.id !== id) });
        engine.toast(m('Указание по {n} снято', { n: pin?.trainId ?? '' }));
      }
    },
    reset() {
      engine.log('warn', m('Сброс участка выполняется на сервере (сценарии), в интерфейсе недоступен'), 'Сервер');
    },
    whatIfOpen() {
      if (!engine.allowed('section', 'what-if моделирование')) return;
      engine.set({ whatIf: { openedAt: st().now, mods: { speed: {}, priority: {}, cancelled: {}, durationMin: {} } } });
    },
    whatIfClose() {
      whatIfRequest = null;
      engine.set({ whatIf: undefined });
    },
    whatIfSetMods(mods) {
      if (!st().whatIf || !engine.allowed('section', 'what-if моделирование')) return;
      engine.set({ whatIf: { ...st().whatIf, mods, result: undefined } });
      clearTimeout(whatIfTimer);
      // Пользователь двигает значения — запрос уходит, когда он остановился.
      whatIfTimer = setTimeout(async () => {
        const modifications = [
          ...Object.entries(mods.speed).filter(([, v]) => v >= 10).map(([id, v]) => ({ kind: 'train_speed', target_id: id, value: v })),
          ...Object.entries(mods.durationMin).map(([id, v]) => ({ kind: 'incident_duration', target_id: id, value: v * 60 })),
          ...Object.entries(mods.priority).map(([id, v]) => ({ kind: 'train_priority', target_id: id, value: v })),
          ...Object.entries(mods.cancelled).filter(([, v]) => v).map(([id]) => ({ kind: 'cancel_train', target_id: id })),
        ];
        if (!modifications.length) return;
        whatIfRequest = await api.whatif(modifications);
      }, 400);
    },
    async whatIfApply() {
      const r = st().whatIf?.result;
      if (!r?.requestId || !engine.allowed('section', 'перенос what-if в работу')) return;
      if (ok(await api.promoteWhatIf(r.requestId), 'перенос what-if в работу')) {
        engine.set({ whatIf: undefined });
        engine.log('info', m('Результат what-if передан диспетчеру как вариант'), engine.actor());
      }
    },
    updateSettings(settings) {
      // Индекс считает сервер (§14); локально настройки влияют только на отображение.
      engine.set({ settings });
      engine.log('info', m('Настройки индекса и оптимизации обновлены'), engine.actor());
    },
  };
  for (const [name, fn] of Object.entries(commands)) engine[name] = fn;

  // ───────────── старт ─────────────
  engine.set({ connection: 'connecting', liveMode: true });
  void Promise.all([api.trains(), api.plan(), api.variants(), api.incidents(), api.scenarios()]).then(([tr, plan, variants, incidents, scenarios]) => {
    if (tr.length) setTrains(tr);
    if (plan) {
      setPlan(plan);
      if (plan.index) engine.set({ index: indexFromSpec(plan.index) });
    }
    for (const d of incidents) upsertIncident(d);
    if (variants) setVariants(variants);
    if (scenarios.length) engine.set({ scenarios });
  });
  const stream = connectStream(onEnvelope, (connection) => engine.set({ connection }));
  setInterval(() => {
    if (!latency.length) return;
    const sorted = [...latency].sort((a, b) => a - b);
    const p95 = sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * 0.95))];
    engine.set({ latencyP95: p95 });
    stream.reportLatency(p95);
  }, 2000);
  void toUi;
}
