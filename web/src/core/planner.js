import { m } from '../i18n/msg';
/**
 * Планировщик: стратегии разрешения конфликтов, оптимизатор очерёдности и генерация
 * 2–3 альтернативных планов при сбое с оценкой последствий и объяснением.
 * Чистые функции без DOM — выполняются в Web Worker и переносимы на backend.
 */
import { computeMeets, detectConflicts, forcedWaits } from './conflicts';
import { planEnergy } from './profile';
import { computeIndex, destDelayMin, weightedDelay } from './qualityIndex';
import { effectivePriority, propagate, routeOf, runTime, scheduleSequential, segOf } from './scheduler';
import { segmentName, stationName } from './section';
const ctxOf = (inp, durMode) => ({
  section: inp.section,
  settings: inp.settings,
  trains: inp.trains,
  disruptions: inp.disruptions,
  now: inp.now,
  prev: inp.prev,
  baseline: inp.baseline,
  pins: inp.pins ?? [],
  durMode,
});
function categoryOrder(trains, s) {
  return [...trains]
    .sort((a, b) => effectivePriority(a, s) - effectivePriority(b, s) || a.departure - b.departure)
    .map((t) => t.id);
}
const fifoOrder = (trains) => [...trains].sort((a, b) => a.departure - b.departure).map((t) => t.id);
const STRATEGY_TITLE = {
  baseline: 'Нормативный график',
  hold: 'Подождать устранения',
  naive: 'Без вмешательства',
  priority: 'По важности поездов',
  optimized: 'Лучший порядок пропуска',
  passengers: 'Сначала пассажирские',
  robust: 'С запасом времени',
  whatif: 'Что если',
  keep_order: 'Сохранить порядок',
  reoptimize: 'Переразвести',
};
const STRATEGY_LEAD = {
  hold: 'Поезда ждут на ближайших станциях, пока путь не освободится. Порядок движения не меняется.',
  priority: 'Сначала пропускаем пассажирские, затем пригородные, потом грузовые.',
  passengers: 'Грузовые уступают дорогу, чтобы пассажирские и пригородные пришли вовремя.',
  optimized: 'Система перебрала порядок пропуска поездов и нашла вариант с наименьшими опозданиями.',
  robust: 'Рассчитано на самый долгий срок устранения — план не придётся менять, если ремонт затянется.',
  keep_order: 'Ваше указание выполняется, порядок поездов прежний — остальные подстраиваются по времени.',
  reoptimize: 'Ваше указание выполняется, а порядок пропуска поездов перестроен так, чтобы опозданий было меньше.',
};
/** Время прибытия на конечный пункт при свободном ходе (без скрещений и обгонов). */
function idealArrival(ctx, t) {
  const route = routeOf(ctx.section, t);
  const free = { ...ctx, disruptions: [] };
  let time = t.departure;
  for (let k = 0; k + 1 < route.length; k++) {
    if (k > 0 && t.stops.includes(route[k])) time += t.dwell;
    time += runTime(free, t, segOf(route, k), time);
  }
  return time;
}
/** Взвешенная задержка относительно свободного хода — критерий построения нормативного графика. */
function idealDelay(ctx, plan) {
  let sum = 0;
  for (const t of ctx.trains) {
    const tp = plan.trains[t.id];
    if (!tp || t.cancelled) continue;
    sum += (ctx.settings.categoryWeight[t.category] * Math.max(0, tp.stops.at(-1).arr - idealArrival(ctx, t))) / 60;
  }
  return sum;
}
/** Оценка плана для локального поиска: взвешенная задержка + штрафы за конфликты. */
function cost(ctx, plan) {
  const conflicts = detectConflicts(ctx.section, ctx.settings, plan, ctx.trains, ctx.disruptions).length;
  const unresolved = Object.values(plan.trains).filter((t) => t.unresolved).length;
  const delay = ctx.baseline ? weightedDelay(plan, ctx.baseline, ctx.trains, ctx.settings) : idealDelay(ctx, plan);
  return delay + 200 * conflicts + 300 * unresolved;
}
/** Локальный поиск по перестановкам очерёдности (модель альтернативного графа). */
function optimizeOrder(ctx, seeds, strategy, label) {
  let best = null;
  for (const seed of seeds) {
    const plan = scheduleSequential(ctx, seed, strategy, label);
    const c = cost(ctx, plan);
    if (!best || c < best.c - 1e-6) best = { plan, order: plan.order, c };
  }
  for (let it = 0; it < ctx.settings.optimizerIterations && best; it++) {
    let improved = false;
    const moves = [];
    for (let i = 0; i + 1 < best.order.length; i++) {
      const order = [...best.order];
      [order[i], order[i + 1]] = [order[i + 1], order[i]];
      moves.push(order);
    }
    for (let i = 1; i < best.order.length; i++) {
      moves.push([best.order[i], ...best.order.slice(0, i), ...best.order.slice(i + 1)]);
    }
    for (const order of moves) {
      const plan = scheduleSequential(ctx, order, strategy, label);
      const c = cost(ctx, plan);
      if (c < best.c - 0.01) {
        best = { plan, order, c };
        improved = true;
      }
    }
    if (!improved) break;
  }
  return best;
}
/** Нормативный график: оптимизатор подбирает очерёдность под минимум задержки к свободному ходу. */
export function buildBaseline(section, settings, trains) {
  const ctx = {
    section,
    settings: { ...settings, optimizerIterations: 12 },
    trains,
    disruptions: [],
    now: 0,
    durMode: 'expected',
  };
  const seeds = [fifoOrder(trains), categoryOrder(trains, settings)];
  return optimizeOrder(ctx, seeds, 'baseline', STRATEGY_TITLE.baseline).plan;
}
/** План по стратегии от текущей обстановки (скользящий горизонт). */
/** Контекст, в котором опоздание пассажирских и пригородных весит многократно больше грузовых. */
function passengerCtx(ctx) {
  const w = ctx.settings.categoryWeight;
  return {
    ...ctx,
    settings: { ...ctx.settings, categoryWeight: { ...w, pass: w.pass * 5, suburb: w.suburb * 5 } },
  };
}

export function planWithStrategy(inp, strategy, durMode, seedOrder) {
  const ctx = ctxOf(inp, durMode);
  const title = STRATEGY_TITLE[strategy];
  switch (strategy) {
    case 'naive':
      return propagate(ctx, false, 'naive', title);
    case 'hold':
    case 'keep_order':
      return propagate(ctx, true, strategy, title);
    case 'priority':
    case 'baseline':
      return scheduleSequential(ctx, categoryOrder(inp.trains, inp.settings), strategy, title);
    case 'passengers': {
      const seeds = [categoryOrder(inp.trains, inp.settings), fifoOrder(inp.trains)];
      if (seedOrder) seeds.unshift(seedOrder);
      return optimizeOrder(passengerCtx(ctx), seeds, strategy, title).plan;
    }
    default: {
      const seeds = [categoryOrder(inp.trains, inp.settings), fifoOrder(inp.trains)];
      if (seedOrder) seeds.unshift(seedOrder);
      return optimizeOrder(ctx, seeds, strategy, title).plan;
    }
  }
}
export function evaluatePlan(inp, plan) {
  const conflicts = detectConflicts(inp.section, inp.settings, plan, inp.trains, inp.disruptions ?? []);
  const index = computeIndex({
    section: inp.section,
    settings: inp.settings,
    trains: inp.trains,
    plan,
    baseline: inp.baseline,
    conflicts,
  });
  return { conflicts, index };
}
export function impactsOf(before, after, baseline, trains) {
  return trains
    .filter((t) => !t.cancelled && after.trains[t.id])
    .map((t) => {
      const a = after.trains[t.id].stops.at(-1).arr;
      const b = before.trains[t.id]?.stops.at(-1)?.arr ?? a;
      return { trainId: t.id, number: t.number, delayMin: destDelayMin(after, baseline, t.id), deltaMin: (a - b) / 60 };
    });
}
function explain(trains, before, after, strategy) {
  const num = new Map(trains.map((t) => [t.id, t.number]));
  const lines = [];
  const lead = STRATEGY_LEAD[strategy];
  if (lead) lines.push(m(lead));
  const mb = computeMeets(before, trains);
  const ma = computeMeets(after, trains);
  let moved = 0;
  for (const [key, st] of ma) {
    const old = mb.get(key);
    if (old !== undefined && old !== st && moved < 3) {
      const [a, b] = key.split('|');
      lines.push(m('Поезда {a} и {b} разъедутся не на {from}, а на {to}', { a: num.get(a), b: num.get(b), from: stationName(old), to: stationName(st) }));
      moved++;
    }
  }
  // Показываем только новые стоянки — те, которых не было в прежнем плане.
  const old = forcedWaits(before, trains);
  const waits = forcedWaits(after, trains)
    .filter((w) => w.to > after.createdAt)
    .filter((w) => !old.some((o) => o.trainId === w.trainId && o.station === w.station && Math.abs(o.min - w.min) < 3))
    .sort((x, y) => y.min - x.min)
    .slice(0, 2);
  for (const w of waits) {
    lines.push(m('{n}: стоянка для скрещения / обгона на {st}, {min} мин', { n: num.get(w.trainId), st: stationName(w.station), min: Math.round(w.min) }));
  }
  return lines;
}
/**
 * Шаги варианта для диспетчера — конкретные действия относительно действующего плана, по времени:
 * задержать поезд на станции (и кого он пропускает), задержать отправление, перенести скрещение,
 * вести поезд медленнее по перегону. Возвращает [{ t, text }] — text сообщение для перевода.
 */
export function stepsOf(before, after, trains, now = -Infinity) {
  const byId = new Map(trains.map((t) => [t.id, t]));
  const steps = [];
  const MIN = 120;
  for (const t of trains) {
    const a = after.trains[t.id]?.stops;
    const b = before?.trains[t.id]?.stops;
    if (!a || !b || t.cancelled) continue;
    for (let j = 0; j < a.length - 1; j++) {
      const st = a[j];
      const old = b.find((x) => x.station === st.station);
      if (!old || st.dep < now) continue;
      const dwell = st.dep - st.arr;
      const extra = dwell - (old.dep - old.arr);
      if (j === 0) {
        if (st.dep - old.dep >= MIN) steps.push({ t: st.dep, text: m('{n}: задержать отправление — {st}, на {d} мин', { n: t.number, st: stationName(st.station), d: Math.round((st.dep - old.dep) / 60) }) });
        continue;
      }
      if (extra < MIN) continue;
      // Кого пропускает, пока стоит: встречные — скрещение, попутные, ушедшие раньше него, — обгон.
      const passing = [];
      for (const q of trains) {
        if (q.id === t.id || q.cancelled) continue;
        const qs = after.trains[q.id]?.stops.find((x) => x.station === st.station);
        if (!qs || qs.dep < st.arr - 30 || qs.dep > st.dep + 60) continue;
        if (q.dir !== t.dir) passing.push(q.number);
        else if (qs.arr >= st.arr - 30) passing.push(q.number);
      }
      const base = { n: t.number, st: stationName(st.station), min: Math.round(dwell / 60) };
      steps.push({
        t: st.arr,
        text: passing.length
          ? m('{n}: стоянка — {st}, {min} мин, пропускает {list}', { ...base, list: passing.slice(0, 3).join(', ') })
          : m('{n}: стоянка — {st}, {min} мин, ждёт освобождения пути', base),
      });
    }
    // Медленнее по перегону без остановки — ход растянут на 2 мин и больше.
    for (let j = 0; j + 1 < a.length; j++) {
      const ob = b.findIndex((x) => x.station === a[j].station);
      if (ob < 0 || !b[ob + 1] || a[j].dep < now) continue;
      const slower = a[j + 1].arr - a[j].dep - (b[ob + 1].arr - b[ob].dep);
      if (slower >= MIN) {
        steps.push({ t: a[j].dep, text: m('{n}: вести медленнее — {seg}, +{d} мин', { n: t.number, seg: segmentName(Math.min(a[j].station, a[j + 1].station)), d: Math.round(slower / 60) }) });
      }
    }
  }
  // Перенесённые скрещения — если пара разъезжается на другом пункте.
  const mb = computeMeets(before, trains);
  const ma = computeMeets(after, trains);
  for (const [key, st] of ma) {
    const old = mb.get(key);
    if (old === undefined || old === st) continue;
    const [x, y] = key.split('|');
    const tt = Math.max(after.trains[x]?.stops.find((s) => s.station === st)?.arr ?? 0, after.trains[y]?.stops.find((s) => s.station === st)?.arr ?? 0);
    if (tt < now) continue;
    steps.push({ t: tt, text: m('Скрещение {a} и {b} — на {to} вместо {from}', { a: byId.get(x)?.number ?? x, b: byId.get(y)?.number ?? y, to: stationName(st), from: stationName(old) }) });
  }
  return steps.sort((p, q) => p.t - q.t);
}
const signature = (p) =>
  Object.values(p.trains)
    .map((t) => `${t.trainId}:${Math.round(t.stops.at(-1).arr / 30)}`)
    .sort()
    .join(',');
/** Генерация альтернативных планов при сбое. */
/** Сколько минут суммарно опаздывают пассажирские и пригородные поезда. */
function passengerDelay(plan, baseline, trains) {
  return trains
    .filter((t) => !t.cancelled && (t.category === 'pass' || t.category === 'suburb'))
    .reduce((sum, t) => sum + destDelayMin(plan, baseline, t.id), 0);
}

/**
 * Условия выбора плана — как режимы маршрута в навигаторе: под каждое условие свой лучший вариант.
 * score: чем меньше, тем лучше. Планы с конфликтами всегда хуже бесконфликтных.
 */
export const CRITERIA = [
  { id: 'optimal', label: 'Сбалансированно', hint: 'лучшее общее качество движения', score: (v) => -v.index.value },
  { id: 'fast', label: 'Меньше опозданий', hint: 'наименьшие суммарные опоздания', score: (v) => v.weightedDelayMin },
  { id: 'passengers', label: 'Пассажиры вовремя', hint: 'пассажирские без опозданий', score: (v) => v.passengerDelayMin },
  { id: 'eco', label: 'Экономия энергии', hint: 'меньше энергии на тягу', score: (v) => v.energyKWh },
  { id: 'robust', label: 'Надёжнее', hint: 'лучше всех, если ремонт затянется', score: (v) => -v.worstIndex },
];

export function bestFor(variants, criterionId) {
  const c = CRITERIA.find((x) => x.id === criterionId) ?? CRITERIA[0];
  return [...variants].sort((a, b) => a.conflicts - b.conflicts || c.score(a) - c.score(b))[0];
}

/** Генерация альтернативных планов при сбое — каждый оптимизирован под своё условие. */
export function computeVariants(inp) {
  const t0 = performance.now();
  const mode = inp.settings.planDuration;
  const ctx = ctxOf(inp, mode);
  const max = ctxOf(inp, 'max');
  const active = inp.trains.filter((t) => !t.cancelled);
  const prevEnergy = planEnergy(inp.section, active, inp.prev);
  const seeds = [categoryOrder(inp.trains, inp.settings), fifoOrder(inp.trains), inp.prev.order];
  const opt = optimizeOrder(ctx, seeds, 'optimized', STRATEGY_TITLE.optimized);
  const pass = optimizeOrder(passengerCtx(ctx), [opt.order, ...seeds], 'passengers', STRATEGY_TITLE.passengers);
  const robust = optimizeOrder(max, [opt.order, ...seeds], 'robust', STRATEGY_TITLE.robust);
  const candidates = [
    { strategy: 'optimized', plan: opt.plan, worst: () => scheduleSequential(max, opt.order, 'optimized', '') },
    { strategy: 'passengers', plan: pass.plan, worst: () => scheduleSequential(max, pass.order, 'passengers', '') },
    { strategy: 'hold', plan: propagate(ctx, true, 'hold', ''), worst: () => propagate(max, true, 'hold', '') },
    {
      strategy: 'priority',
      plan: scheduleSequential(ctx, seeds[0], 'priority', ''),
      worst: () => scheduleSequential(max, seeds[0], 'priority', ''),
    },
    { strategy: 'robust', plan: robust.plan, worst: () => robust.plan },
  ];
  const seen = new Set();
  const variants = [];
  for (const c of candidates) {
    const sig = signature(c.plan);
    // «Ждать» показываем всегда — это базовая альтернатива для диспетчера.
    if (seen.has(sig) && c.strategy !== 'hold') continue;
    seen.add(sig);
    const { conflicts, index } = evaluatePlan(inp, c.plan);
    const energy = planEnergy(inp.section, active, c.plan);
    const worstPlan = c.worst();
    const totalDelay = (p) => active.reduce((sum, t) => sum + Math.max(0, destDelayMin(p, inp.baseline, t.id)), 0);
    variants.push({
      id: c.plan.id,
      strategy: c.strategy,
      title: STRATEGY_TITLE[c.strategy],
      plan: c.plan,
      index,
      worstIndex: evaluatePlan(inp, worstPlan).index.value,
      // На сколько вырастет суммарное опоздание, если сбой продлится максимум.
      robustExtraMin: Math.max(0, totalDelay(worstPlan) - totalDelay(c.plan)),
      unplannedStops: forcedWaits(c.plan, inp.trains).length,
      conflicts: conflicts.length,
      weightedDelayMin: weightedDelay(c.plan, inp.baseline, inp.trains, inp.settings),
      passengerDelayMin: passengerDelay(c.plan, inp.baseline, inp.trains),
      impacts: impactsOf(inp.prev, c.plan, inp.baseline, inp.trains),
      energyKWh: energy,
      energyDeltaPct: prevEnergy > 0 ? (energy / prevEnergy - 1) * 100 : 0,
      explanation: explain(inp.trains, inp.prev, c.plan, c.strategy),
      steps: stepsOf(inp.prev, c.plan, inp.trains, inp.now),
      wins: [],
    });
  }
  for (const c of CRITERIA) bestFor(variants, c.id).wins.push(c.id);
  const longest = Math.max(0, ...inp.disruptions.map((d) => d.durMax / 60));
  for (const v of variants) Object.assign(v, prosCons(v, variants, longest));
  variants.sort((a, b) => a.conflicts - b.conflicts || b.index.value - a.index.value);
  return { variants, computeMs: performance.now() - t0 };
}

/** Когда выбирать вариант — одной фразой. */
const STRATEGY_WHEN = {
  optimized: 'Когда главное — чтобы суммарно поезда опоздали как можно меньше.',
  passengers: 'Когда важнее всего пассажиры, а грузовые могут подождать.',
  hold: 'Когда сбой короткий и не хочется перестраивать движение.',
  keep_order: 'Поезда идут в прежнем порядке, остальные подстраиваются по времени.',
  reoptimize: 'Система меняет порядок пропуска вокруг вашего указания, если так опозданий меньше.',
  priority: 'Когда нужно действовать строго по регламенту категорий поездов.',
  robust: 'Когда неясно, сколько продлится сбой, и план не должен «сломаться».',
};

/**
 * Плюсы и минусы варианта — в сравнении с остальными вариантами, простыми словами и с цифрами.
 */
export function prosCons(v, all, longestMin) {
  const best = {
    delay: Math.min(...all.map((x) => x.weightedDelayMin)),
    pass: Math.min(...all.map((x) => x.passengerDelayMin)),
    energy: Math.min(...all.map((x) => x.energyKWh)),
    worst: Math.max(...all.map((x) => x.worstIndex)),
    index: Math.max(...all.map((x) => x.index.value)),
    total: Math.min(...all.map((x) => Math.round(x.impacts.reduce((sum, y) => sum + y.delayMin, 0)))),
  };
  const total = Math.round(v.impacts.reduce((sum, x) => sum + x.delayMin, 0));
  const pros = [];
  const cons = [];

  if (v.index.value >= best.index) pros.push(m('Лучшее качество движения: {v} из 100', { v: v.index.value.toFixed(0) }));
  if (v.weightedDelayMin <= best.delay + 0.5) pros.push(total ? m('Меньше всего опозданий: +{total} мин на все поезда', { total }) : m('Без опозданий'));
  if (v.passengerDelayMin < 1) pros.push(m('Пассажирские вовремя'));
  else if (v.passengerDelayMin <= best.pass + 0.5) pros.push(m('Пассажирские опоздают меньше всего: +{v} мин', { v: Math.round(v.passengerDelayMin) }));
  if (v.energyKWh <= best.energy * 1.005) pros.push(m('Меньше всего расход энергии'));
  if (v.worstIndex >= best.worst - 0.5 && all.length > 1) pros.push(m('Выдержит затяжной сбой: качество не ниже {v}', { v: v.worstIndex.toFixed(0) }));
  if (v.strategy === 'hold') pros.push(m('Порядок поездов не меняется'));
  if (v.conflicts === 0 && all.some((x) => x.conflicts > 0)) pros.push(m('Без конфликтов'));

  if (v.conflicts > 0) cons.push(m('Остаются конфликты: {n}', { n: v.conflicts }));
  if (v.weightedDelayMin > best.delay + 1 && total - best.total >= 1) cons.push(m('Опозданий на {d} мин больше, чем в лучшем варианте', { d: Math.round(total - best.total) }));
  if (v.passengerDelayMin >= 1 && v.passengerDelayMin > best.pass + 0.5) cons.push(m('Пассажирские опоздают на {v} мин', { v: Math.round(v.passengerDelayMin) }));
  if (v.worstIndex < best.worst - 2 && longestMin) cons.push(m('Если сбой затянется до {d} мин — качество {v}', { d: Math.round(longestMin), v: v.worstIndex.toFixed(0) }));
  if (v.energyKWh > best.energy * 1.02) cons.push(m('Энергии на {p}% больше', { p: Math.round((v.energyKWh / best.energy - 1) * 100) }));
  const worstTrain = [...v.impacts].sort((a, b) => b.delayMin - a.delayMin)[0];
  if (worstTrain && worstTrain.delayMin >= 15) cons.push(m('Поезд {n} опоздает на {d} мин', { n: worstTrain.number, d: Math.round(worstTrain.delayMin) }));
  if (v.strategy !== 'hold' && v.impacts.some((x) => Math.abs(x.deltaMin) >= 1)) cons.push(m('Меняется порядок поездов'));

  return { pros: pros.slice(0, 3), cons: cons.slice(0, 3), when: STRATEGY_WHEN[v.strategy] ?? '' };
}

// ───────────── ручное изменение времени на ГИД ─────────────
const sameSig = (a, b) => signature(a) === signature(b);

/**
 * Прогноз ручного изменения: план с указанием при прежнем порядке поездов (как «Сохранить порядок»),
 * без перебора — считается быстро, на каждый шаг перетаскивания.
 */
export function previewManual(inp) {
  const ctx = ctxOf(inp, inp.settings.planDuration);
  const plan = propagate(ctx, true, 'keep_order', STRATEGY_TITLE.keep_order);
  const { conflicts, index } = evaluatePlan(inp, plan);
  return { plan, conflicts, index };
}

/** Варианты после отпускания точки: «Сохранить порядок» и, если выгоднее, «Переразвести». */
export function manualVariants(inp) {
  const t0 = performance.now();
  const ctx = ctxOf(inp, inp.settings.planDuration);
  const active = inp.trains.filter((t) => !t.cancelled);
  const keep = propagate(ctx, true, 'keep_order', STRATEGY_TITLE.keep_order);
  const seeds = [inp.prev.order, categoryOrder(inp.trains, inp.settings), fifoOrder(inp.trains)];
  const reopt = optimizeOrder(ctx, seeds, 'reoptimize', STRATEGY_TITLE.reoptimize).plan;
  const prevEnergy = planEnergy(inp.section, active, inp.prev);
  const candidates = [{ strategy: 'keep_order', plan: keep }];
  const reoptBetter = !sameSig(keep, reopt) && cost(ctx, reopt) < cost(ctx, keep) - 0.5;
  if (reoptBetter) candidates.push({ strategy: 'reoptimize', plan: reopt });
  const variants = candidates.map((c) => {
    const { conflicts, index } = evaluatePlan(inp, c.plan);
    const energy = planEnergy(inp.section, active, c.plan);
    const explanation = explain(inp.trains, inp.prev, c.plan, c.strategy);
    if (!reoptBetter) explanation.push(m('Перестановка скрещений не даёт выигрыша'));
    return {
      id: c.plan.id,
      strategy: c.strategy,
      title: STRATEGY_TITLE[c.strategy],
      plan: c.plan,
      index,
      worstIndex: index.value,
      robustExtraMin: null,
      unplannedStops: forcedWaits(c.plan, inp.trains).length,
      conflicts: conflicts.length,
      weightedDelayMin: weightedDelay(c.plan, inp.baseline, inp.trains, inp.settings),
      passengerDelayMin: passengerDelay(c.plan, inp.baseline, inp.trains),
      impacts: impactsOf(inp.prev, c.plan, inp.baseline, inp.trains),
      energyKWh: energy,
      energyDeltaPct: prevEnergy > 0 ? (energy / prevEnergy - 1) * 100 : 0,
      explanation,
      steps: stepsOf(inp.prev, c.plan, inp.trains, inp.now),
      wins: [],
    };
  });
  for (const v of variants) Object.assign(v, prosCons(v, variants, 0));
  return { variants, computeMs: performance.now() - t0 };
}
