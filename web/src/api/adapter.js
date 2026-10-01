/**
 * Данные бэкенда (модели §8 ТЗ, конверты §15) → состояние движка, которое читают компоненты интерфейса.
 * Всё знание о формате сервера собрано здесь: если сервер поменяет поле, правится только этот файл.
 *
 * Время: на сервере sim_time — секунды от SIM_EPOCH, в интерфейсе — секунды от полуночи.
 */
import { impactsOf, prosCons, stepsOf } from '../core/planner';
import { destDelayMin } from '../core/qualityIndex';
import { SECTION } from '../core/section';

let epoch = parseEpoch(import.meta.env.VITE_SIM_EPOCH ?? '07:55');

function parseEpoch(v) {
  const m = /(\d{2}):(\d{2})(?::(\d{2}))?/.exec(String(v).includes('T') ? String(v).split('T')[1] : String(v));
  return m ? Number(m[1]) * 3600 + Number(m[2]) * 60 + Number(m[3] ?? 0) : 7 * 3600 + 55 * 60;
}
export const setEpoch = (v) => v && (epoch = parseEpoch(v));
/** Серверное время → время интерфейса и обратно. */
export const toUi = (t) => (t === null || t === undefined ? t : epoch + t);
export const toServer = (t) => t - epoch;

const stIdx = () => new Map(SECTION.stations.map((s, i) => [s.specId, i]));
const segIdx = () => new Map(SECTION.segments.map((g, i) => [g.specId, i]));

// ───────────── поезда и график ─────────────
const CATEGORY = { express: 'express', passenger: 'pass', freight: 'freight' };
export const toSpecCategory = (c) => ({ express: 'express', pass: 'passenger', suburb: 'passenger', freightFast: 'freight', freight: 'freight' })[c] ?? c;

/** Поезд расписания (§8 Train) → поезд интерфейса. */
export function trainFromSpec(t) {
  const idx = stIdx();
  const stops = t.stops.filter((s, i) => i > 0 && i < t.stops.length - 1 && s.stop).map((s) => idx.get(s.station_id));
  const dwell = Math.max(0, ...t.stops.filter((s) => s.stop).map((s) => s.min_dwell_s ?? 0));
  return {
    id: t.id,
    number: t.id,
    category: CATEGORY[t.category] ?? 'freight',
    dir: t.direction === 'odd' ? 1 : -1,
    departure: toUi(t.stops[0].dep),
    stops,
    dwell,
    vmaxOverride: t.v_max_override_kmh ?? undefined,
    priorityOverride: t.priority_override ?? undefined,
    cancelled: !!t.cancelled,
    spec: t,
  };
}

/** Норматив = расписание сервера в форме плана: от него считаются опоздания. */
export function baselineFromTimetable(trains) {
  const idx = stIdx();
  const out = {};
  for (const t of trains) {
    const sp = t.spec;
    out[t.id] = {
      trainId: t.id,
      stops: sp.stops.map((s, i) => {
        const arr = toUi(s.arr ?? s.dep);
        const dep = toUi(s.dep ?? s.arr);
        return { station: idx.get(s.station_id), arr: i === 0 ? dep : arr, dep: i === sp.stops.length - 1 ? arr : dep, track: 0, planned: i === 0 || i === sp.stops.length - 1 || s.stop };
      }),
    };
  }
  return { id: 'timetable', strategy: 'baseline', order: trains.map((t) => t.id), trains: out };
}

/** План сервера (§8 Plan: run/dwell) → план интерфейса: остановки поезда по всем раздельным пунктам. */
export function planFromSpec(plan, trains) {
  if (!plan) return null;
  const idx = stIdx();
  const byTrain = new Map();
  for (const e of plan.entries) {
    if (e.kind !== 'dwell') continue;
    if (!byTrain.has(e.train_id)) byTrain.set(e.train_id, []);
    byTrain.get(e.train_id).push(e);
  }
  const trainById = new Map(trains.map((t) => [t.id, t]));
  const out = {};
  for (const [id, dwells] of byTrain) {
    dwells.sort((a, b) => a.start - b.start);
    const t = trainById.get(id);
    out[id] = {
      trainId: id,
      stops: dwells.map((d, i) => {
        const station = idx.get(d.station_id);
        const st = SECTION.stations[station];
        const first = i === 0;
        const last = i === dwells.length - 1;
        return {
          station,
          arr: toUi(first ? d.end : d.start),
          dep: toUi(last ? d.start : d.end),
          track: Math.max(0, st?.trackIds?.indexOf(d.track_id) ?? 0),
          planned: first || last || !!t?.stops.includes(station),
          unplanned: !!d.unplanned,
        };
      }),
    };
  }
  const order = Object.keys(out).sort((a, b) => out[a].stops[0].dep - out[b].stops[0].dep);
  return {
    id: `v${plan.version}`,
    version: plan.version,
    strategy: plan.strategy ?? 'balanced',
    solver: plan.solver,
    solveMs: plan.solve_ms,
    order,
    trains: out,
    meetings: (plan.meetings ?? []).map((m) => ({ ...m, station: idx.get(m.station_id), time: toUi(m.time) })),
    kpi: plan.kpi,
  };
}

// ───────────── указания диспетчера и ручное изменение ─────────────
/** Pin сервера → указание интерфейса. */
export function pinFromSpec(p) {
  return {
    id: p.id,
    trainId: p.train_id,
    station: stIdx().get(p.station_id),
    kind: p.kind,
    time: toUi(p.time),
    createdAt: toUi(p.created_at),
    status: p.status,
    reason: p.reason ?? null,
    description: p.description,
  };
}
const boundFromSpec = (b) =>
  b && {
    current: toUi(b.current),
    min: toUi(b.min),
    max: toUi(b.max),
    minReason: b.min_reason,
    maxReason: b.max_reason,
    locked: b.locked,
    lockedReason: b.locked_reason,
  };
/** BoundsResponse → границы точки для перетаскивания. */
export function boundsFromSpec(r) {
  const i = r.info ?? {};
  return {
    basePlanVersion: r.base_plan_version,
    pointType: r.point_type,
    arr: boundFromSpec(r.arr),
    dep: boundFromSpec(r.dep),
    info: {
      minDwell: i.min_dwell_s ?? 0,
      prevDep: toUi(i.prev_dep ?? null),
      prevStation: i.prev_station_id ? stIdx().get(i.prev_station_id) : null,
      run: i.min_run_s != null ? { min: i.min_run_s, max: i.max_run_s, lengthKm: (i.segment_length_m ?? 0) / 1000, vmax: i.v_max_kmh } : null,
      schedArr: toUi(i.sched_arr ?? null),
      schedDep: toUi(i.sched_dep ?? null),
      pin: i.pin ? pinFromSpec(i.pin) : null,
    },
  };
}
/** PreviewResponse → прогноз: нитки затронутых поездов накладываются на действующий план. */
export function previewFromSpec(r, plan, trains) {
  const idx = stIdx();
  const out = { ...plan.trains };
  for (const th of r.threads ?? []) {
    const cur = plan.trains[th.train_id]?.stops ?? [];
    const byStation = new Map(th.points.map((pt) => [idx.get(pt.station_id), pt]));
    out[th.train_id] = {
      ...(plan.trains[th.train_id] ?? { trainId: th.train_id }),
      stops: cur.map((st) => {
        const pt = byStation.get(st.station);
        if (!pt) return st;
        const arr = toUi(pt.arr ?? pt.dep);
        const dep = toUi(pt.dep ?? pt.arr);
        return { ...st, arr, dep };
      }),
    };
  }
  const num = new Map(trains.map((t) => [t.id, t.number]));
  const kmOf = (res) => {
    const g = SECTION.segments.find((x) => x.specId === res);
    if (g) return (SECTION.stations[g.from].km + SECTION.stations[g.to].km) / 2;
    const st = SECTION.stations.find((x) => x.specId === res || res?.startsWith?.(`${x.specId}:`));
    return st?.km ?? 0;
  };
  const d = r.dragged ?? {};
  return {
    time: toUi(r.time),
    clamped: r.clamped,
    dragged: { arr: toUi(d.arr ?? null), dep: toUi(d.dep ?? null), dwell: d.dwell_s ?? 0, prevRun: d.prev_run_s ?? null },
    plan: { ...plan, trains: out },
    changed: (r.affected ?? []).map((a) => ({ trainId: a.train_id, number: num.get(a.train_id) ?? a.train_id, deltaFinal: a.delta_final_s, deltaMax: a.delta_max_s })),
    conflicts: (r.conflicts ?? []).map((c) => ({ time: toUi(c.time_from), km: kmOf(c.resource_id), trains: c.trains, kind: c.kind })),
    index: { value: r.index_forecast },
    deltaIndex: r.delta_index ?? 0,
    totalDelayDelta: (r.total_delay_delta_s ?? 0) / 60,
    ms: r.compute_ms,
  };
}

// ───────────── индекс ─────────────
const CAT = { norm: 'norm', attention: 'warn', critical: 'crit' };
const fmtRaw = (v) => (Math.abs(v) >= 100 ? Math.round(v).toLocaleString('ru-RU') : (Math.round(v * 10) / 10).toString());

/** IndexValue сервера → индекс интерфейса (факторы с подписью и «сырым» значением). */
export function indexFromSpec(ix) {
  if (!ix) return null;
  return {
    value: Math.round(ix.value * 10) / 10,
    category: CAT[ix.category] ?? 'norm',
    factors: (ix.components ?? []).map((c) => ({ key: c.key, name: c.label, weight: c.weight, score: c.score, detail: `${fmtRaw(c.raw)} ${c.unit}` })),
  };
}

// ───────────── сбои ─────────────
const KIND = {
  obstacle: 'livestock',
  segment_closed: 'closure',
  speed_restriction: 'signal',
  signal_failure: 'signal',
  train_failure: 'breakdown',
  train_delay: 'delay',
};
/** Обратное соответствие: событие, брошенное инструктором на карту, → тип сбоя сервера. */
export const SPEC_TYPE = { livestock: 'obstacle', closure: 'segment_closed', signal: 'speed_restriction', breakdown: 'train_failure', delay: 'train_delay' };

/** Incident сервера → сбой интерфейса (перегон по индексу, место перекрытия в метрах от начала ребра). */
export function disruptionFromSpec(d, now) {
  const kind = KIND[d.type];
  if (!kind) return null;
  const segs = segIdx();
  let segment = d.segment_id ? segs.get(d.segment_id) : undefined;
  if (segment === undefined && d.station_id) {
    // Отказ светофора на станции — ограничение на перегоне отправления от неё.
    const i = SECTION.stations.findIndex((s) => s.specId === d.station_id);
    segment = Math.min(Math.max(0, i), SECTION.segments.length - 1);
  }
  const g = segment !== undefined ? SECTION.segments[segment] : null;
  let posStart;
  let posEnd;
  if (g) {
    const from = SECTION.stations[g.from].km;
    const at = d.km != null ? (d.km - from) * 1000 : g.length / 2;
    const warn = d.type === 'speed_restriction' && d.params?.km_from != null;
    const p0 = warn ? (Math.min(d.params.km_from, d.params.km_to) - from) * 1000 : at - 500;
    const p1 = warn ? (Math.max(d.params.km_from, d.params.km_to) - from) * 1000 : at + 500;
    // Позиции на ребре считаются от его start_node.
    posStart = Math.max(0, Math.round(g.reversed ? g.length - p1 : p0));
    posEnd = Math.min(g.length, Math.round(g.reversed ? g.length - p0 : p1));
  }
  const resolvedAt = d.status === 'resolved' ? toUi(d.params?.resolved_at ?? now - epoch) : undefined;
  return {
    id: d.id,
    kind,
    specType: d.type,
    segment,
    track: undefined,
    edgeId: g?.edgeId,
    posStart,
    posEnd,
    trainId: d.train_id ?? undefined,
    start: toUi(d.started_at),
    durMin: d.est_min_s,
    durMax: d.est_max_s,
    durExpected: d.est_expected_s ?? Math.round((d.est_min_s + d.est_max_s) / 2),
    speedLimit: kind === 'signal' ? Number(d.params?.v_kmh ?? d.params?.speed_kmh ?? 20) : undefined,
    anchors: [],
    resolvedAt,
    note: d.description,
    description: d.description,
  };
}

// ───────────── варианты ─────────────
const STRATEGY = { balanced: 'optimized', passenger_first: 'passengers', robust: 'robust', rescue: 'rescue', fewer_stops: 'eco', whatif: 'whatif' };

/** Variant сервера → вариант интерфейса с плюсами и минусами (как у локального планировщика). */
export function variantsFromSpec(payload, ctx) {
  const { trains, baseline, prev, disruptions } = ctx;
  const list = payload.variants.filter((v) => v.status === 'proposed');
  const variants = list.map((v) => {
    const plan = planFromSpec(v.plan, trains);
    const k = v.plan.kpi;
    const index = indexFromSpec(v.plan.index);
    const robustExtra = k.robust_total_delay_s != null ? Math.max(0, k.robust_total_delay_s - k.total_delay_s) / 60 : 0;
    return {
      id: v.id,
      // Рекомендацию ставит сервер по единому мерилу score (§27.5) — ровно одна карточка.
      recommended: !!v.recommended,
      score: v.score,
      kind: v.kind ?? 'incident',
      status: v.status,
      updatedAt: toUi(v.updated_at),
      specStrategy: v.strategy,
      strategy: STRATEGY[v.strategy] ?? v.strategy,
      title: v.title,
      plan,
      index,
      // Индекс при максимальной длительности сбоя сервер не присылает — оцениваем по приросту опоздания.
      worstIndex: Math.max(0, index.value - robustExtra * 0.5),
      robustExtraMin: k.robust_total_delay_s != null ? robustExtra : null,
      conflicts: k.conflicts ?? 0,
      weightedDelayMin: (k.weighted_delay_s ?? 0) / 60,
      passengerDelayMin: trains.filter((t) => t.category !== 'freight' && t.category !== 'freightFast').reduce((s, t) => s + destDelayMin(plan, baseline, t.id), 0),
      impacts: prev ? impactsOf(prev, plan, baseline, trains) : [],
      energyKWh: k.energy_kwh ?? 0,
      energyDeltaPct: k.energy_ideal_kwh ? (k.energy_kwh / k.energy_ideal_kwh - 1) * 100 : 0,
      unplannedStops: k.unplanned_stops ?? 0,
      delayedTrains: (k.delayed_trains ?? []).map(([id, s]) => ({ id, min: s / 60 })),
      totalDelayMin: (k.total_delay_s ?? 0) / 60,
      deltaIndex: v.delta_index,
      basePlanVersion: v.base_plan_version,
      solveMs: v.plan.solve_ms,
      explanation: ['', ...(v.explanation ?? [])],
      steps: prev ? stepsOf(prev, plan, trains) : [],
      wins: [],
    };
  });
  const longest = Math.max(0, ...disruptions.filter((d) => d.resolvedAt === undefined).map((d) => d.durMax / 60));
  for (const v of variants) Object.assign(v, prosCons(v, variants, longest));
  return variants;
}

/** Результат what-if сервера → результат песочницы интерфейса. */
export function whatIfFromSpec(r, ctx) {
  const plan = planFromSpec(r.plan, ctx.trains);
  return {
    requestId: r.request_id,
    plan,
    trains: ctx.trains,
    index: indexFromSpec(r.plan.index),
    deltaIndex: r.delta_index,
    conflicts: r.plan.kpi?.conflicts ?? 0,
    affected: r.per_train.map((x) => ({ trainId: x.train_id, number: x.train_id, deltaMin: x.arr_delta_s / 60, delayMin: x.final_delay_s / 60 })),
    changedMeetings: (r.changed_meetings ?? []).map((m) => ({ ...m, time: toUi(m.time) })),
    explanation: r.explanation ?? [],
    computeMs: r.plan.solve_ms,
  };
}

// ───────────── поле ─────────────
/** Где поезда сейчас — по данным поля (§11.1), а не по плану: сервер — источник правды о положении. */
export function fieldFromSpec(f) {
  const idx = stIdx();
  const segs = segIdx();
  const trains = f.trains
    .filter((t) => t.on_field !== false && t.status !== 'finished')
    .map((t) => {
      const base = { trainId: t.train_id, status: t.status, speed: t.speed_kmh, delayS: t.delay_s, regime: t.regime };
      if (t.segment_id) return { ...base, km: t.km, segment: segs.get(t.segment_id), moving: t.status === 'running' };
      const station = idx.get(t.station_id);
      const st = SECTION.stations[station];
      return { ...base, station, km: st?.km, track: Math.max(0, st?.trackIds?.indexOf(t.track_id) ?? 0), moving: false };
    });
  return {
    t: toUi(f.sim_time),
    trains,
    signals: Object.fromEntries((f.signals ?? []).map((s) => [s.id, s.aspect])),
    /** Блок-участки: id → {occupied_by, obstacle}. */
    blocks: Object.fromEntries((f.blocks ?? []).map((b) => [b.id, b])),
    /** Установленное направление перегона: segment_id → {direction: odd|even|null, changing}. */
    directions: f.directions ?? {},
    counters: f.counters ?? null,
    closedSegments: f.closed_segments ?? [],
    speed: f.speed,
    effectiveSpeed: f.effective_speed ?? f.speed,
    paused: !!f.paused,
    decisionHold: !!f.decision_hold,
    planVersion: f.plan_version,
    safetyViolations: f.safety_violations ?? 0,
  };
}

/** Фактические нитки ГИД из снимка сервера: {train_id: [[t, km], …]} → время интерфейса. */
export const factFromSpec = (fact) =>
  Object.fromEntries(Object.entries(fact ?? {}).map(([id, pts]) => [id, pts.map(([t, km]) => [toUi(t), km])]));

/** Запись журнала решений сервера (JournalEntry) → строка журнала интерфейса. */
const JOURNAL_LEVEL = {
  incident_created: 'crit',
  plan_broken: 'crit',
  signal_stop: 'warn',
  variants_proposed: 'warn',
  variants_stale: 'warn',
  decision_hold: 'warn',
  guard_replan: 'warn',
  variant_applied: 'ok',
  incident_resolved: 'ok',
  no_decision_needed: 'ok',
};
export const journalFromSpec = (e) => ({ id: e.id, t: toUi(e.time), level: JOURNAL_LEVEL[e.kind] ?? 'info', text: e.text, source: 'Сервер', kind: e.kind });
