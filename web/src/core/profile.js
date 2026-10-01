/**
 * Автоведение (advisory, Connected DAS): рекомендованный профиль скорости.
 *
 * Оптимальное по энергии ведение на перегоне состоит из режимов «разгон — движение с постоянной
 * скоростью — накат — торможение» (TU Delft, теория оптимального управления). Задача сводится к
 * подбору скорости движения vc и скорости начала торможения vb так, чтобы время хода совпало с
 * графиком, а работа тяги была минимальной.
 */
import { CATEGORIES, SECTION_KM } from './section';
import { effectiveVmax } from './scheduler';
const ETA = 0.85; // КПД тягового привода
const C_RES = 0.012; // удельное основное сопротивление движению, м/с²
const B_BRAKE = 0.35; // рабочее замедление при служебном торможении, м/с²
const accelOf = (massT) => (massT >= 2000 ? 0.18 : 0.4);
function evalProfile(D, vc, vb, a) {
  const da = (vc * vc) / (2 * a);
  const dc = (vc * vc - vb * vb) / (2 * C_RES);
  const db = (vb * vb) / (2 * B_BRAKE);
  const dcr = D - da - dc - db;
  const time = vc / a + (vc - vb) / C_RES + vb / B_BRAKE + (dcr > 0 ? dcr / vc : 0);
  const e = (vc * vc) / 2 + C_RES * (da + Math.max(0, dcr));
  return { vc, vb, time, e, feasible: dcr >= -0.5, da, dc, db, dcr: Math.max(0, dcr) };
}
function minTimeProfile(D, cap, a) {
  const reach = Math.sqrt(D / (1 / (2 * a) + 1 / (2 * B_BRAKE)));
  const vc = Math.min(cap, reach);
  return evalProfile(D, vc, vc, a);
}
function optimizeRun(D, T, cap, a) {
  const ref = minTimeProfile(D, cap, a);
  if (T <= ref.time + 1) return { eco: ref, ref, late: T < ref.time - 30 };
  let best = null;
  let slowest = null;
  const den = 1 / (2 * C_RES) - 1 / (2 * B_BRAKE);
  for (let vc = ref.vc; vc >= 3; vc -= 0.25) {
    const noCoast = evalProfile(D, vc, vc, a);
    if (!noCoast.feasible) continue;
    if (noCoast.time > T) break;
    const num = (vc * vc) / (2 * a) + (vc * vc) / (2 * C_RES) - D;
    const vbMin = Math.max(0.5, Math.min(vc, num > 0 ? Math.sqrt(num / den) : 0.5));
    const maxCoast = evalProfile(D, vc, vbMin, a);
    slowest = maxCoast;
    if (maxCoast.time < T) continue; // даже с максимальным накатом приходит раньше — ниже скорость
    let lo = vbMin;
    let hi = vc;
    for (let i = 0; i < 40; i++) {
      const mid = (lo + hi) / 2;
      if (evalProfile(D, vc, mid, a).time > T) lo = mid;
      else hi = mid;
    }
    const cand = evalProfile(D, vc, hi, a);
    if (!best || cand.e < best.e) best = cand;
  }
  return { eco: best ?? slowest ?? ref, ref, late: false };
}
function sample(rc, a, startDist) {
  const pts = [];
  const push = (s, v, phase) => pts.push({ dist: startDist + s / 1000, v: v * 3.6, phase });
  const N = 10;
  for (let i = 0; i <= N; i++) {
    const v = (rc.vc * i) / N;
    push((v * v) / (2 * a), v, 'accel');
  }
  push(rc.da + rc.dcr, rc.vc, 'cruise');
  const s1 = rc.da + rc.dcr;
  for (let i = 1; i <= N; i++) {
    const v = rc.vc - ((rc.vc - rc.vb) * i) / N;
    push(s1 + (rc.vc * rc.vc - v * v) / (2 * C_RES), v, 'coast');
  }
  const s2 = s1 + rc.dc;
  for (let i = 1; i <= N; i++) {
    const v = rc.vb * (1 - i / N);
    push(s2 + (rc.vb * rc.vb - v * v) / (2 * B_BRAKE), v, 'brake');
  }
  return pts;
}
export function trainProfile(section, train, tp) {
  const cat = CATEGORIES[train.category];
  const a = accelOf(cat.massT);
  const toKWh = (e) => (cat.massT * 1000 * e) / ETA / 3.6e6;
  const dist = (km) => (train.dir === 1 ? km : SECTION_KM - km);
  const stops = tp.stops;
  const last = stops.length - 1;
  const halts = stops
    .map((s, j) => ({ s, j }))
    .filter(({ s, j }) => j === 0 || j === last || s.planned || s.dep - s.arr > 20)
    .map(({ j }) => j);
  const runs = [];
  const points = [];
  const refPoints = [];
  for (let h = 0; h + 1 < halts.length; h++) {
    const p = stops[halts[h]];
    const q = stops[halts[h + 1]];
    const kmP = section.stations[p.station].km;
    const kmQ = section.stations[q.station].km;
    const D = Math.abs(kmQ - kmP) * 1000;
    const T = q.arr - p.dep;
    const lo = Math.min(p.station, q.station);
    const hi = Math.max(p.station, q.station);
    let capKmh = effectiveVmax(train);
    for (let sg = lo; sg < hi; sg++) capKmh = Math.min(capKmh, section.segments[sg].vmax);
    const { eco, ref, late } = optimizeRun(D, T, capKmh / 3.6, a);
    const d0 = dist(kmP);
    points.push(...sample(eco, a, d0));
    refPoints.push(...sample(ref, a, d0));
    runs.push({
      fromStation: p.station,
      toStation: q.station,
      dep: p.dep,
      arr: q.arr,
      cruiseKmh: eco.vc * 3.6,
      coastFromDist: d0 + (eco.da + eco.dcr) / 1000,
      brakeFromDist: d0 + (eco.da + eco.dcr + eco.dc) / 1000,
      energyKWh: toKWh(eco.e),
      refEnergyKWh: toKWh(ref.e),
      late,
    });
  }
  const energyKWh = runs.reduce((s, r) => s + r.energyKWh, 0);
  const refEnergyKWh = runs.reduce((s, r) => s + r.refEnergyKWh, 0);
  return {
    runs,
    points,
    refPoints,
    energyKWh,
    refEnergyKWh,
    savingPct: refEnergyKWh > 0 ? (1 - energyKWh / refEnergyKWh) * 100 : 0,
  };
}
const energyCache = new WeakMap();
/** Энергия на тягу по плану при ведении по рекомендациям автоведения, кВт·ч. */
export function planEnergy(section, trains, plan) {
  const key = trains.map((t) => `${t.id}:${t.vmaxOverride ?? ''}`).join(',');
  let m = energyCache.get(plan);
  if (!m) energyCache.set(plan, (m = new Map()));
  const hit = m.get(key);
  if (hit !== undefined) return hit;
  let sum = 0;
  for (const t of trains) {
    const tp = plan.trains[t.id];
    if (tp && !t.cancelled) sum += trainProfile(section, t, tp).energyKWh;
  }
  m.set(key, sum);
  return sum;
}
