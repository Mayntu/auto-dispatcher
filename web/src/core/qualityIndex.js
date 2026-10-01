import { m } from '../i18n/msg';
/**
 * Индекс качества движения: I = 100 · Σ wᵢ·sᵢ, Σ wᵢ = 1, sᵢ ∈ [0, 1].
 * Веса и пороги берутся из настроек (без перекомпиляции).
 */
import { forcedWaits } from './conflicts';
import { planEnergy } from './profile';
import { FACTOR_NAMES, normalizedWeights } from './settings';
const clamp01 = (x) => Math.min(1, Math.max(0, Number.isFinite(x) ? x : 0));
function categoryOf(value, s) {
  return value >= s.thresholds.norm ? 'norm' : value >= s.thresholds.warn ? 'warn' : 'crit';
}
export const CATEGORY_LABEL = { norm: 'Норма', warn: 'Внимание', crit: 'Критично' };
const lastArr = (plan, id) => plan.trains[id]?.stops.at(-1)?.arr;
/** Опоздание прибытия на конечный пункт участка относительно нормативного графика, мин. */
export function destDelayMin(plan, baseline, id) {
  const p = lastArr(plan, id);
  const b = lastArr(baseline, id);
  return p === undefined || b === undefined ? 0 : Math.max(0, (p - b) / 60);
}
export function weightedDelay(plan, baseline, trains, s) {
  return trains
    .filter((t) => !t.cancelled)
    .reduce((sum, t) => sum + s.categoryWeight[t.category] * destDelayMin(plan, baseline, t.id), 0);
}
export function computeIndex({ section, settings: s, trains, plan, baseline, conflicts }) {
  const active = trains.filter((t) => !t.cancelled && plan.trains[t.id]);
  // 1. Соблюдение расписания (взвешенное по категориям опоздание)
  let wSum = 0;
  let wd = 0;
  for (const t of active) {
    const w = s.categoryWeight[t.category];
    wSum += w;
    wd += w * destDelayMin(plan, baseline, t.id);
  }
  const avgDelay = wSum ? wd / wSum : 0;
  const s1 = clamp01(1 - avgDelay / s.delayNormMin);
  // 2. Пропускная способность: нормативное время хода / плановое, отменённые поезда — потеря
  let ratioSum = 0;
  for (const t of active) {
    const p = plan.trains[t.id];
    const b = baseline.trains[t.id];
    const pr = p.stops.at(-1).arr - p.stops[0].dep;
    const br = b ? b.stops.at(-1).arr - b.stops[0].dep : pr;
    ratioSum += clamp01(br / pr);
  }
  const s2 = trains.length ? ratioSum / trains.length : 1;
  // 3. Энергоэффективность: энергия нормативного графика / энергия плана
  const ep = planEnergy(section, active, plan);
  const eb = planEnergy(section, active, baseline);
  const s3 = ep > 0 ? clamp01(eb / ep) : 1;
  // 4. Конфликты и вынужденные остановки сверх нормативного графика
  const extraStops = Math.max(0, forcedWaits(plan, active).length - forcedWaits(baseline, active).length);
  const s4 = clamp01(1 - 0.25 * conflicts.length - 0.04 * extraStops);
  // 5. Точность прибытия: доля плановых прибытий в пределах ±3 мин
  let total = 0;
  let ok = 0;
  for (const t of active) {
    const p = plan.trains[t.id];
    const b = baseline.trains[t.id];
    p.stops.forEach((st, j) => {
      if (j === 0 || !st.planned) return;
      total++;
      if (!b || Math.abs(st.arr - b.stops[j].arr) <= 180) ok++;
    });
  }
  const s5 = total ? ok / total : 1;
  const w = normalizedWeights(s);
  const scores = {
    punctuality: { score: s1, detail: m('ср. взвеш. опоздание {v} мин', { v: avgDelay.toFixed(1) }) },
    throughput: {
      score: s2,
      detail: m('{a}/{b} поездов, ход {p}% от норматива', { a: active.length, b: trains.length, p: (s2 * 100).toFixed(0) }),
    },
    energy: {
      score: s3,
      detail: m('{e} кВт·ч (норматив {b})', { e: Math.round(ep), b: Math.round(eb) }),
    },
    conflicts: { score: s4, detail: m('{c} конфликт., +{s} вынужд. стоянок', { c: conflicts.length, s: extraStops }) },
    accuracy: { score: s5, detail: m('{ok}/{total} прибытий в пределах ±3 мин', { ok, total }) },
  };
  const factors = Object.keys(scores).map((key) => ({
    key,
    name: FACTOR_NAMES[key],
    weight: w[key],
    ...scores[key],
  }));
  const value = Math.round(100 * factors.reduce((sum, f) => sum + f.weight * f.score, 0) * 10) / 10;
  return { value, category: categoryOf(value, s), factors };
}
