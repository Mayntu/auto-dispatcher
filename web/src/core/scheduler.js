/**
 * Автодиспетчеризация: построение бесконфликтного графика на однопутном участке.
 *
 * Модель — «альтернативный граф»: каждый конфликт за перегон или путь раздельного пункта
 * разрешается выбором очерёдности. Два решателя:
 *  - scheduleSequential — поезда резервируют перегоны и пути в заданном порядке приоритета
 *    (порядок подбирает оптимизатор в variants.ts). Бесконфликтность гарантируется резервированием,
 *    встречные «тупики» исключаются проверкой ёмкости раздельного пункта с откатом назад.
 *  - propagate — очерёдность на каждом перегоне фиксируется из предыдущего плана, время
 *    сдвигается по самому длинному пути (вариант «ждать устранения»).
 * Всё, что уже произошло к моменту now, замораживается (перепланирование со скользящим горизонтом).
 */
import { blockRange, CATEGORIES } from './section';
/** Запас времени хода на перегоне (разгон/замедление, техническая надбавка), с. */
const RUN_MARGIN = 90;
/** Минимальное время занятия пути раздельного пункта при безостановочном проследовании, с. */
export const MIN_OCC = 60;
const DEFAULT_SIGNAL_LIMIT = 40;
let planSeq = 0;
const newPlanId = () => `P${Date.now().toString(36)}${(++planSeq).toString(36)}`;
export function routeOf(section, train) {
  const r = section.stations.map((_, i) => i);
  return train.dir === 1 ? r : r.reverse();
}
export const segOf = (route, k) => Math.min(route[k], route[k + 1]);
export const effectiveVmax = (t) => t.vmaxOverride ?? CATEGORIES[t.category].vmax;
export const effectivePriority = (t, s) => t.priorityOverride ?? s.categoryPriority[t.category];
export function durationOf(d, mode) {
  if (d.resolvedAt !== undefined) return Math.max(0, d.resolvedAt - d.start);
  return mode === 'max' ? d.durMax : d.durExpected;
}
export function isBlocking(d) {
  return d.kind === 'livestock' || d.kind === 'closure';
}
/**
 * Перекрытия перегона. На двухпутном перегоне помеха на одном пути не останавливает движение:
 * поезда идут по второму пути через съезды (однопутная работа), а встречные начинают конфликтовать.
 * full — перегон закрыт целиком (однопутка, оба пути или путь не указан); single — работа по одному пути.
 */
export function blockSets(section, disruptions, seg, durMode = 'expected') {
  const g = section.segments[seg];
  const full = [];
  const partial = [];
  const hard = [];
  for (const d of disruptions) {
    if (!isBlocking(d) || d.segment !== seg) continue;
    const w = [d.start, d.start + durationOf(d, durMode)];
    if (g.tracks < 2 || d.track === undefined) {
      full.push(w);
      hard.push({ w, range: d.posStart !== undefined ? blockRange(g, d.posStart, d.posEnd ?? g.length) : [0, 1] });
    }
    else partial.push({ w, track: d.track, range: blockRange(g, d.posStart ?? 0, d.posEnd ?? g.length) });
  }
  // Оба пути закрыты на одной и той же части в одно время — перегон закрыт целиком.
  for (let i = 0; i < partial.length; i++) {
    for (let j = i + 1; j < partial.length; j++) {
      const p = partial[i];
      const q = partial[j];
      if (p.track === q.track || p.range[1] <= q.range[0] || q.range[1] <= p.range[0]) continue;
      const a = Math.max(p.w[0], q.w[0]);
      const b = Math.min(p.w[1], q.w[1]);
      if (a < b) {
        full.push([a, b]);
        hard.push({ w: [a, b], range: [Math.max(p.range[0], q.range[0]), Math.min(p.range[1], q.range[1])] });
      }
    }
  }
  return { full, partial, hard };
}

/** Когда поезд проходит часть перегона [f0, f1] (доли от станции from). */
function partTimes(tr, f0, f1) {
  const s = tr.stuck;
  const at = (f) => {
    const u = tr.dir === 1 ? f : 1 - f;
    if (!s || !(s.from < s.to)) return tr.enter + (tr.exit - tr.enter) * u;
    // Остановка на перегоне (stuck: доля пути u, время from…to): до неё поезд идёт к точке, после — от неё.
    if (u < s.u - 1e-9) return tr.enter + (s.from - tr.enter) * (u / Math.max(s.u, 1e-9));
    return s.to + (tr.exit - s.to) * ((u - s.u) / Math.max(1 - s.u, 1e-9));
  };
  return [Math.min(at(f0), at(f1)), Math.max(at(f0), at(f1))];
}

/**
 * На сколько нужно задержать вход поезда cand на перегон, чтобы не встретиться со встречным other.
 * На однопутном перегоне встречные не могут быть на нём одновременно. На двухпутном мешают друг другу только
 * на закрытой части одного из путей (между съездами), пока перекрытие действует, — там остаётся один путь.
 */
export function oppositeClash(section, disruptions, seg, cand, other, clear, durMode = 'expected') {
  if (section.segments[seg].tracks < 2) {
    return cand.enter < other.exit + clear && cand.exit + clear > other.enter ? other.exit + clear - cand.enter : 0;
  }
  let need = 0;
  for (const p of blockSets(section, disruptions, seg, durMode).partial) {
    const [c0, c1] = partTimes(cand, p.range[0], p.range[1]);
    const [o0, o1] = partTimes(other, p.range[0], p.range[1]);
    const bothInWindow = c0 < p.w[1] && c1 > p.w[0] && o0 < p.w[1] && o1 > p.w[0];
    if (bothInWindow && c0 < o1 + clear && c1 + clear > o0) need = Math.max(need, o1 + clear - c0);
  }
  return need;
}

/** Правильный путь двухпутного перегона для направления: нечётные — путь 0, чётные — путь 1. */
export const ownTrack = (dir) => (dir === 1 ? 0 : 1);

const blockWindows = (ctx, seg) => blockSets(ctx.section, ctx.disruptions, seg, ctx.durMode).full;
const clash = (ctx, seg, cand, other) => oppositeClash(ctx.section, ctx.disruptions, seg, cand, other, ctx.settings.clearSec, ctx.durMode);
/** Проход по неправильному пути: два съезда на пониженной скорости. */
const CROSSOVER_PENALTY = 120;
function speedLimitAt(ctx, seg, t) {
  let v = Infinity;
  for (const d of ctx.disruptions) {
    if (d.kind !== 'signal' || d.segment !== seg) continue;
    if (t >= d.start && t < d.start + durationOf(d, ctx.durMode)) v = Math.min(v, d.speedLimit ?? DEFAULT_SIGNAL_LIMIT);
  }
  return v;
}
export function runTime(ctx, train, seg, enter) {
  const s = ctx.section.segments[seg];
  const v = Math.min(effectiveVmax(train), s.vmax, speedLimitAt(ctx, seg, enter));
  let t = (s.lengthKm / v) * 3600 + RUN_MARGIN;
  if (s.tracks >= 2) {
    const own = ownTrack(train.dir);
    // Объезд через съезды — только если поезд проходит закрытую часть своего пути, пока она закрыта.
    const blockedOwn = blockSets(ctx.section, ctx.disruptions, seg, ctx.durMode).partial.some((p) => {
      const [a, b] = partTimes({ enter, exit: enter + t, dir: train.dir }, p.range[0], p.range[1]);
      return p.track === own && a < p.w[1] && b > p.w[0];
    });
    if (blockedOwn) t += CROSSOVER_PENALTY;
  }
  return Math.round(t);
}
function anchorValue(ctx, trainId, kind, k) {
  let v = -Infinity;
  for (const d of ctx.disruptions) {
    for (const a of d.anchors) {
      if (a.trainId === trainId && a.kind === kind && a.routeIdx === k) {
        v = Math.max(v, a.base + durationOf(d, ctx.durMode));
      }
    }
  }
  return v;
}
function depLowerBound(ctx, train, k) {
  return Math.max(anchorValue(ctx, train.id, 'stop', k), k === 0 ? anchorValue(ctx, train.id, 'origin', 0) : -Infinity);
}
const isPlannedStop = (train, station) => train.stops.includes(station);
function baselineDep(ctx, trainId, k) {
  return ctx.baseline?.trains[trainId]?.stops[k]?.dep;
}
class Reservations {
  seg;
  sta;
  constructor(nSeg, nSta) {
    this.seg = Array.from({ length: nSeg }, () => []);
    this.sta = Array.from({ length: nSta }, () => []);
  }
  removeProvisional(trainId) {
    const removed = [];
    this.sta.forEach((list, st) => {
      for (let i = list.length - 1; i >= 0; i--) {
        if (list[i].trainId === trainId && list[i].provisional) removed.push([st, ...list.splice(i, 1)]);
      }
    });
    return removed;
  }
  /** Поезда, которые бессрочно (до своего планирования) занимают пути раздельного пункта в момент t. */
  provisionalAt(st, t, ex) {
    return this.sta[st].filter((r) => r.provisional && r.trainId !== ex && r.from <= t).map((r) => r.trainId);
  }
  occAt(st, t, ex) {
    let c = 0;
    for (const r of this.sta[st]) if (r.trainId !== ex && r.from <= t && t < r.to) c++;
    return c;
  }
  maxOcc(st, a, b, ex) {
    let m = this.occAt(st, a, ex);
    for (const r of this.sta[st]) {
      if (r.trainId !== ex && r.from > a && r.from < b) m = Math.max(m, this.occAt(st, r.from, ex));
    }
    return m;
  }
  /** Самый ранний момент τ ∈ [a, b), начиная с которого путь свободен до b. */
  freeFrom(st, a, b, cap, ex) {
    const cands = [a];
    for (const r of this.sta[st]) if (r.trainId !== ex && r.to > a && r.to < b) cands.push(r.to);
    cands.sort((x, y) => x - y);
    for (const c of cands) if (this.maxOcc(st, c, b, ex) < cap) return c;
    return null;
  }
  nextRelease(st, t, ex) {
    let best = null;
    for (const r of this.sta[st]) {
      if (r.trainId !== ex && r.to > t && Number.isFinite(r.to)) best = best === null ? r.to : Math.min(best, r.to);
    }
    return best;
  }
}
function earliestEntry(ctx, res, train, seg, t0) {
  const H = ctx.settings.headwaySec;
  const C = ctx.settings.clearSec;
  const blocks = blockWindows(ctx, seg);
  let t = t0;
  for (let guard = 0; guard < 400; guard++) {
    const run = runTime(ctx, train, seg, t);
    const exit = t + run;
    let moved = false;
    for (const [b0, b1] of blocks) {
      if (t < b1 && exit > b0) {
        t = b1;
        moved = true;
        break;
      }
    }
    if (!moved) {
      for (const r of res.seg[seg]) {
        if (r.dir !== train.dir) {
          const need = clash(ctx, seg, { enter: t, exit, dir: train.dir }, r);
          if (need > 0) {
            t += Math.max(1, need);
            moved = true;
            break;
          }
        } else {
          const before = t + H <= r.enter && exit + H <= r.exit;
          const after = t >= r.enter + H && exit >= r.exit + H;
          if (!before && !after) {
            t = Math.max(r.enter + H, r.exit + H - run, t + 1);
            moved = true;
            break;
          }
        }
      }
    }
    if (!moved) return { enter: t, run };
  }
  return { enter: t, run: runTime(ctx, train, seg, t) };
}
function freeze(ctx, train) {
  const route = routeOf(ctx.section, train);
  const tp = ctx.prev?.trains[train.id];
  const free = {
    train,
    route,
    prefix: [],
    startK: 0,
    done: false,
    minDep: Math.max(train.departure, ctx.prev ? ctx.now : -Infinity),
  };
  if (!tp || ctx.now < tp.stops[0].dep) return free;
  const st = tp.stops;
  let i = 0;
  while (i + 1 < st.length && st[i + 1].arr <= ctx.now) i++;
  const clone = (a) => a.map((s) => ({ ...s }));
  if (i === st.length - 1) {
    return { ...free, prefix: clone(st), startK: st.length - 1, done: true };
  }
  if (st[i].dep > ctx.now) {
    return { ...free, prefix: clone(st.slice(0, i)), startK: i, startArr: st[i].arr, minDep: ctx.now };
  }
  // Поезд на перегоне i → i+1: вход зафиксирован, выход пересчитывается с учётом сбоев.
  const seg = segOf(route, i);
  const enter = st[i].dep;
  const { exit, stuck } = frozenRun(ctx, train, seg, i, enter);
  return {
    ...free,
    prefix: clone(st.slice(0, i + 1)),
    startK: i + 1,
    startArr: exit,
    minDep: -Infinity,
    frozenSeg: { k: i, enter, exit },
    stuck,
  };
}

/**
 * Время выхода поезда, который уже идёт по перегону. Если сбой застал его на месте (поломка, помеха под ним) —
 * стоит, где был. Если впереди место, закрытое без объезда (оба пути или весь перегон), — останавливается
 * у съезда перед ним и ждёт открытия. stuck — где (доля пути по ходу) и когда поезд стоит, для карты и графика.
 */
function frozenRun(ctx, train, seg, k, enter) {
  const run = runTime(ctx, train, seg, enter);
  let exit = enter + run;
  let stuck;
  for (const d of ctx.disruptions) {
    for (const a of d.anchors) {
      if (a.trainId !== train.id || a.kind !== 'seg' || a.routeIdx !== k) continue;
      const dur = durationOf(d, ctx.durMode);
      if (a.base + dur <= exit) continue;
      exit = a.base + dur;
      const u = Math.min(1, Math.max(0, (d.start - enter) / Math.max(1, a.base - enter)));
      stuck = { u, from: d.start, to: d.start + dur };
    }
  }
  const free = { enter, exit: enter + run, dir: train.dir };
  for (const h of blockSets(ctx.section, ctx.disruptions, seg, ctx.durMode).hard) {
    const [reach] = partTimes(free, h.range[0], h.range[1]);
    if (reach < h.w[0] || reach >= h.w[1]) continue;
    const out = enter + run + (h.w[1] - reach);
    if (out <= exit) continue;
    exit = out;
    stuck = { u: train.dir === 1 ? h.range[0] : 1 - h.range[1], from: reach, to: h.w[1] };
  }
  return { exit: Math.round(exit), stuck };
}
/** Попутные поезда на одном перегоне не могут обогнать друг друга: выход ведомого ≥ выход ведущего + интервал. */
function enforceFrozenFollowers(ctx, states) {
  const groups = new Map();
  for (const st of states) {
    if (!st.frozenSeg) continue;
    const key = `${segOf(st.route, st.frozenSeg.k)}:${st.train.dir}`;
    groups.set(key, [...(groups.get(key) ?? []), st]);
  }
  for (const list of groups.values()) {
    list.sort((a, b) => a.frozenSeg.enter - b.frozenSeg.enter);
    for (let i = 1; i < list.length; i++) {
      const need = list[i - 1].frozenSeg.exit + ctx.settings.headwaySec;
      if (list[i].frozenSeg.exit < need) {
        const f = list[i].frozenSeg;
        const lead = list[i - 1].stuck;
        // Ведущий стоит на перегоне — ведомый встаёт за ним (а не проезжает сквозь) и трогается следом.
        if (lead && !list[i].stuck) {
          const u = Math.max(0, lead.u - 0.04);
          const from = f.enter + (f.exit - f.enter) * u;
          if (from < lead.to) list[i].stuck = { u, from, to: Math.max(from, lead.to + ctx.settings.headwaySec / 2) };
        }
        f.exit = need;
        list[i].startArr = need;
      }
    }
  }
}
/**
 * Встречные поезда, которые уже идут по одному перегону, а на нём закрыта часть пути одного из них.
 * Объезжающий по соседнему пути не может разминуться со встречным на закрытой части — он останавливается
 * у съезда перед ней и ждёт, пока встречный её пройдёт (если сам уже внутри — ждёт встречный).
 */
function enforceFrozenOpposing(ctx, states) {
  const frozen = states.filter((s) => s.frozenSeg);
  const clear = ctx.settings.clearSec;
  const iv = (s) => ({ enter: s.frozenSeg.enter, exit: s.frozenSeg.exit, dir: s.train.dir, stuck: s.stuck });
  const hold = (s, part, until) => {
    const [reach] = partTimes(iv(s), part.range[0], part.range[1]);
    const from = s.stuck && s.stuck.from < s.stuck.to ? Math.min(s.stuck.from, reach) : reach;
    const extra = Math.max(0, until - Math.max(reach, s.stuck?.to ?? -Infinity));
    if (extra <= 0) return;
    s.stuck = { u: s.train.dir === 1 ? part.range[0] : 1 - part.range[1], from, to: Math.max(reach, s.stuck?.to ?? reach) + extra };
    s.frozenSeg.exit += extra;
    s.startArr = s.frozenSeg.exit;
  };
  for (let i = 0; i < frozen.length; i++) {
    for (let j = i + 1; j < frozen.length; j++) {
      const a = frozen[i];
      const b = frozen[j];
      if (a.train.dir === b.train.dir) continue;
      const seg = segOf(a.route, a.frozenSeg.k);
      if (seg !== segOf(b.route, b.frozenSeg.k)) continue;
      for (const p of blockSets(ctx.section, ctx.disruptions, seg, ctx.durMode).partial) {
        // Ждёт тот, чей путь закрыт: он объезжает по пути встречного.
        let [w, o] = p.track === ownTrack(a.train.dir) ? [a, b] : p.track === ownTrack(b.train.dir) ? [b, a] : [null, null];
        if (!w) continue;
        if (oppositeClash(ctx.section, ctx.disruptions, seg, iv(w), iv(o), clear, ctx.durMode) <= 0) continue;
        // Если объезжающий уже на закрытой части — останавливаться поздно, ждёт встречный.
        if (partTimes(iv(w), p.range[0], p.range[1])[0] < ctx.now) [w, o] = [o, w];
        const [, oOut] = partTimes(iv(o), p.range[0], p.range[1]);
        hold(w, p, oOut + clear);
      }
    }
  }
}
function reservePrefix(ctx, res, st) {
  const { route, prefix, train } = st;
  const stations = ctx.section.stations;
  prefix.forEach((p, j) => {
    if (!stations[p.station].terminal) {
      res.sta[p.station].push({
        trainId: train.id,
        from: p.arr,
        to: Math.max(p.dep, p.arr + MIN_OCC),
        provisional: false,
      });
    }
    if (j + 1 < prefix.length) {
      res.seg[segOf(route, j)].push({ trainId: train.id, dir: train.dir, enter: p.dep, exit: prefix[j + 1].arr, stuck: prefix[j + 1].stuck });
    }
  });
  if (st.done || st.startArr === undefined) return;
  if (prefix.length > 0) {
    res.seg[segOf(route, st.startK - 1)].push({
      trainId: train.id,
      dir: train.dir,
      enter: prefix[prefix.length - 1].dep,
      exit: st.startArr,
      stuck: st.stuck,
    });
  }
  const s = route[st.startK];
  if (!stations[s].terminal) {
    // До пересчёта поезд занимает путь бессрочно — так более приоритетные поезда его не «перепрыгнут».
    res.sta[s].push({ trainId: train.id, from: st.startArr, to: Infinity, provisional: true });
  }
}
function scheduleFrom(ctx, res, st, reportBlock) {
  const { train, route } = st;
  const stations = ctx.section.stations;
  const n = route.length;
  const removed = res.removeProvisional(train.id);
  const arr = new Array(n).fill(NaN);
  const dep = new Array(n).fill(NaN);
  const minEnter = new Array(n).fill(-Infinity);
  const k0 = st.startK;
  arr[k0] = k0 === 0 ? st.minDep : st.startArr;
  const earliestAt = (k) => {
    const s = route[k];
    const planned = isPlannedStop(train, s);
    let e = k === 0 ? st.minDep : arr[k] + (planned ? train.dwell : 0);
    if (k === k0 && k !== 0) e = Math.max(e, st.minDep);
    const bd = baselineDep(ctx, train.id, k);
    if (planned && k !== 0 && bd !== undefined) e = Math.max(e, bd);
    return Math.max(e, depLowerBound(ctx, train, k), minEnter[k]);
  };
  let k = k0;
  let unresolved = false;
  let guard = 0;
  while (k < n - 1) {
    if (++guard > 800) {
      unresolved = true;
      break;
    }
    const s = route[k];
    const seg = segOf(route, k);
    const e = earliestEntry(ctx, res, train, seg, earliestAt(k));
    if (k > k0 && !stations[s].terminal) {
      const a = arr[k];
      const b = Math.max(e.enter, a + MIN_OCC);
      if (res.maxOcc(s, a, b, train.id) >= stations[s].tracks) {
        const tau = res.freeFrom(s, a, b, stations[s].tracks, train.id);
        if (tau !== null && tau > a) {
          // Нет свободного пути к моменту прибытия — задерживаем отправление с предыдущего пункта.
          minEnter[k - 1] = Math.max(minEnter[k - 1], dep[k - 1] + (tau - a));
          k--;
          continue;
        }
        const rel = res.nextRelease(s, b, train.id);
        if (rel === null) {
          const blockers = res.provisionalAt(s, b, train.id);
          if (reportBlock && blockers.length) {
            for (const [si, r] of removed) res.sta[si].push(r);
            return { blockedBy: blockers };
          }
          unresolved = true;
          break;
        }
        minEnter[k] = Math.max(minEnter[k], rel);
        continue;
      }
    }
    dep[k] = e.enter;
    arr[k + 1] = e.enter + e.run;
    k++;
  }
  if (unresolved) {
    // Не удалось разрешить — достраиваем «как есть», конфликт подсветит детектор.
    for (let j = k; j < n - 1; j++) {
      const d = earliestAt(j);
      dep[j] = d;
      arr[j + 1] = d + runTime(ctx, train, segOf(route, j), d);
    }
  }
  dep[n - 1] = arr[n - 1];
  if (k0 === 0) arr[0] = dep[0];
  const stops = [...st.prefix];
  for (let j = k0; j < n; j++) {
    stops.push({
      station: route[j],
      arr: arr[j],
      dep: dep[j],
      track: 0,
      planned: j === 0 || j === n - 1 || isPlannedStop(train, route[j]),
      ...(j === k0 && st.stuck ? { stuck: st.stuck } : {}),
    });
  }
  for (let j = k0; j < n; j++) {
    const s = route[j];
    if (!stations[s].terminal) {
      res.sta[s].push({ trainId: train.id, from: arr[j], to: Math.max(dep[j], arr[j] + MIN_OCC), provisional: false });
    }
    if (j < n - 1)
      res.seg[segOf(route, j)].push({ trainId: train.id, dir: train.dir, enter: dep[j], exit: arr[j + 1] });
  }
  return { trainId: train.id, stops, unresolved };
}
export function scheduleSequential(ctx, order, strategy, label) {
  const active = ctx.trains.filter((t) => !t.cancelled);
  const res = new Reservations(ctx.section.segments.length, ctx.section.stations.length);
  const states = new Map(active.map((t) => [t.id, freeze(ctx, t)]));
  enforceFrozenFollowers(ctx, [...states.values()]);
  enforceFrozenOpposing(ctx, [...states.values()]);
  // Ожидание встречного могло задержать ведущего — интервал попутных проверяется ещё раз.
  enforceFrozenFollowers(ctx, [...states.values()]);
  for (const st of states.values()) reservePrefix(ctx, res, st);
  const seq = [...order.filter((id) => states.has(id)), ...active.map((t) => t.id).filter((id) => !order.includes(id))];
  const trains = {};
  const inProgress = new Set();
  // Если путь раздельного пункта занят поездом, который ещё не спланирован, сначала планируется он:
  // стоящий на разъезде поезд физически должен уйти раньше, чем через разъезд пройдёт следующий.
  const place = (id) => {
    if (trains[id]) return;
    const st = states.get(id);
    if (st.done) {
      trains[id] = { trainId: id, stops: st.prefix, unresolved: false };
      return;
    }
    inProgress.add(id);
    for (let attempt = 0; attempt < states.size + 1; attempt++) {
      const r = scheduleFrom(ctx, res, st, true);
      if (!('blockedBy' in r)) {
        trains[id] = r;
        break;
      }
      const next = r.blockedBy.find((b) => !trains[b] && !inProgress.has(b));
      if (!next) {
        trains[id] = scheduleFrom(ctx, res, st, false);
        break;
      }
      place(next);
    }
    if (!trains[id]) trains[id] = scheduleFrom(ctx, res, st, false);
    inProgress.delete(id);
  };
  seq.forEach(place);
  const plan = { id: newPlanId(), createdAt: ctx.now, strategy, label, order: seq, trains };
  assignTracks(ctx.section, plan);
  return plan;
}
// ───────────────────────── Фиксированная очерёдность ─────────────────────────
/**
 * Сдвиг плана с сохранением очерёдности поездов на каждом перегоне.
 * interTrain=false — каждый поезд сдвигается только своими ограничениями (прогноз «без вмешательства»).
 */
export function propagate(ctx, interTrain, strategy, label) {
  const prev = ctx.prev;
  const H = ctx.settings.headwaySec;
  const C = ctx.settings.clearSec;
  const active = ctx.trains.filter((t) => !t.cancelled);
  const states = active.map((t) => freeze(ctx, t));
  enforceFrozenFollowers(ctx, states);
  enforceFrozenOpposing(ctx, states);
  enforceFrozenFollowers(ctx, states);
  const times = new Map();
  for (const st of states) {
    const tp = prev.trains[st.train.id];
    const n = st.route.length;
    const arr = tp ? tp.stops.map((s) => s.arr) : new Array(n).fill(NaN);
    const dep = tp ? tp.stops.map((s) => s.dep) : new Array(n).fill(NaN);
    if (st.startArr !== undefined) arr[st.startK] = st.startArr;
    times.set(st.train.id, { arr, dep });
  }
  const segOrder = ctx.section.segments.map(() => []);
  for (const st of states) {
    const tp = prev.trains[st.train.id];
    for (let k = 0; k < st.route.length - 1; k++) {
      segOrder[segOf(st.route, k)].push({
        id: st.train.id,
        k,
        dir: st.train.dir,
        t: tp?.stops[k].dep ?? st.train.departure,
      });
    }
  }
  segOrder.forEach((l) => l.sort((a, b) => a.t - b.t));
  let changed = true;
  for (let iter = 0; changed && iter < 200; iter++) {
    changed = false;
    for (const st of states) {
      if (st.done) continue;
      const { train, route } = st;
      const n = route.length;
      const tm = times.get(train.id);
      const prevStops = prev.trains[train.id]?.stops;
      for (let k = st.startK; k < n - 1; k++) {
        const planned = isPlannedStop(train, route[k]);
        let d = k === 0 ? st.minDep : tm.arr[k] + (planned ? train.dwell : 0);
        if (k === st.startK && k !== 0) d = Math.max(d, st.minDep);
        const bd = baselineDep(ctx, train.id, k);
        if (planned && k !== 0 && bd !== undefined) d = Math.max(d, bd);
        d = Math.max(d, depLowerBound(ctx, train, k));
        if (!interTrain && prevStops) d = Math.max(d, prevStops[k].dep);
        const seg = segOf(route, k);
        const blocks = blockWindows(ctx, seg);
        for (let g = 0; g < 200; g++) {
          const run = runTime(ctx, train, seg, d);
          let moved = false;
          for (const [b0, b1] of blocks)
            if (d < b1 && d + run > b0) {
              d = b1;
              moved = true;
            }
          if (interTrain) {
            for (const p of segOrder[seg]) {
              if (p.id === train.id) break;
              const pt = times.get(p.id);
              if (!pt) continue;
              const pEnter = pt.dep[p.k];
              const pExit = pt.arr[p.k + 1];
              if (!Number.isFinite(pEnter) || !Number.isFinite(pExit)) continue;
              if (p.dir !== train.dir) {
                const need = clash(ctx, seg, { enter: d, exit: d + run, dir: train.dir }, { enter: pEnter, exit: pExit, dir: p.dir });
                if (need > 0) {
                  d += Math.max(1, need);
                  moved = true;
                }
                continue;
              }
              const need = Math.max(pEnter + H, pExit + H - run);
              if (d < need) {
                d = need;
                moved = true;
              }
            }
          }
          if (!moved) break;
        }
        const exit = d + runTime(ctx, train, seg, d);
        if (tm.dep[k] !== d || tm.arr[k + 1] !== exit) {
          tm.dep[k] = d;
          tm.arr[k + 1] = exit;
          changed = true;
        }
      }
      tm.dep[n - 1] = tm.arr[n - 1];
      if (st.startK === 0) tm.arr[0] = tm.dep[0];
    }
  }
  const trains = {};
  for (const st of states) {
    const tm = times.get(st.train.id);
    const stops = [...st.prefix];
    for (let j = st.startK; j < st.route.length; j++) {
      if (st.done) break;
      stops.push({
        station: st.route[j],
        arr: tm.arr[j],
        dep: tm.dep[j],
        track: 0,
        planned: j === 0 || j === st.route.length - 1 || isPlannedStop(st.train, st.route[j]),
        ...(j === st.startK && st.stuck ? { stuck: st.stuck } : {}),
      });
    }
    trains[st.train.id] = { trainId: st.train.id, stops, unresolved: false };
  }
  const plan = { id: newPlanId(), createdAt: ctx.now, strategy, label, order: [...prev.order], trains };
  assignTracks(ctx.section, plan);
  return plan;
}
// ───────────────────────── Пути раздельных пунктов ─────────────────────────
/** Назначение путей: безостановочные — по главному (0), остановки и скрещения — на боковые. */
function assignTracks(section, plan) {
  section.stations.forEach((station, si) => {
    const visits = [];
    for (const tp of Object.values(plan.trains)) {
      for (const stop of tp.stops) {
        if (stop.station === si) {
          visits.push({
            stop,
            start: stop.arr,
            end: Math.max(stop.dep, stop.arr + MIN_OCC),
            through: stop.dep - stop.arr < 5,
          });
        }
      }
    }
    visits.sort((a, b) => a.start - b.start);
    const freeAt = new Array(station.tracks).fill(-Infinity);
    const sides = Array.from({ length: station.tracks - 1 }, (_, i) => i + 1);
    for (const v of visits) {
      const pref = v.through ? [0, ...sides] : [...sides, 0];
      let track = pref.find((i) => freeAt[i] <= v.start);
      if (track === undefined) track = freeAt.indexOf(Math.min(...freeAt));
      v.stop.track = track;
      freeAt[track] = v.end;
    }
  });
}
