import { m } from '../i18n/msg';
import { locate } from './positions';
import { ownTrack, segOf, routeOf } from './scheduler';
import { blockRange, segmentName } from './section';
export const KIND_LABEL = {
  livestock: 'Препятствие на перегоне (скот)',
  closure: 'Окно (закрытие перегона)',
  signal: 'Отказ светофора',
  breakdown: 'Неисправность подвижного состава',
  delay: 'Опоздание поезда',
};
export const KIND_TARGET = {
  livestock: 'segment',
  closure: 'segment',
  signal: 'segment',
  breakdown: 'train',
  delay: 'train',
};
let seq = 0;
/** Фиксирует событие сбоя и привязывает его к поездам по текущему плану. */
export function buildDisruption(section, trains, plan, now, spec) {
  const lo = Math.max(1, Math.min(spec.minMin, spec.maxMin));
  const hi = Math.max(lo, spec.maxMin);
  const anchors = [];
  // Сбой на перегоне можно запланировать на будущее время; сбой поезда — всегда «сейчас».
  const start = spec.segment !== undefined ? Math.max(now, spec.start ?? now) : now;
  if ((spec.kind === 'livestock' || spec.kind === 'closure') && spec.segment !== undefined && start === now) {
    for (const t of trains) {
      const loc = locate(plan, t.id, now);
      // Помеха на одном пути двухпутного перегона останавливает только поезда, идущие по этому пути.
      if (loc.state !== 'seg' || segOf(routeOf(section, t), loc.k) !== spec.segment) continue;
      const g = section.segments[spec.segment];
      if (spec.track !== undefined && g.tracks >= 2) {
        // По соседнему пути или вне закрытой части (успеет уйти через съезд) — поезд не останавливается.
        if (ownTrack(t.dir) !== spec.track) continue;
        const a = plan.trains[t.id].stops[loc.k];
        const b = plan.trains[t.id].stops[loc.k + 1];
        const run = (now - a.dep) / Math.max(1, b.arr - a.dep);
        const f = t.dir === 1 ? run : 1 - run;
        const at = spec.pos ?? g.length / 2;
        const [r0, r1] = blockRange(g, Math.max(0, at - 500), Math.min(g.length, at + 500));
        if (f < r0 || f > r1) continue;
      }
      anchors.push({ trainId: t.id, kind: 'seg', routeIdx: loc.k, base: plan.trains[t.id].stops[loc.k + 1].arr });
    }
  }
  if ((spec.kind === 'breakdown' || spec.kind === 'delay') && spec.trainId) {
    const t = trains.find((x) => x.id === spec.trainId);
    const loc = locate(plan, spec.trainId, now);
    if (t && loc.state === 'before')
      anchors.push({ trainId: t.id, kind: 'origin', routeIdx: 0, base: Math.max(t.departure, now) });
    if (t && loc.state === 'at') anchors.push({ trainId: t.id, kind: 'stop', routeIdx: loc.k, base: now });
    if (t && loc.state === 'seg')
      anchors.push({ trainId: t.id, kind: 'seg', routeIdx: loc.k, base: plan.trains[t.id].stops[loc.k + 1].arr });
  }
  // Сбой на перегоне — это перекрытие (blockages): edge_id + участок pos_start…pos_end в метрах от start_node.
  const seg = spec.segment !== undefined ? section.segments[spec.segment] : undefined;
  const half = 500;
  const at = spec.pos ?? (seg ? seg.length / 2 : 0);
  return {
    id: `D${Date.now().toString(36)}${(++seq).toString(36)}`,
    kind: spec.kind,
    segment: spec.segment,
    track: seg && seg.tracks >= 2 ? spec.track : undefined,
    edgeId: seg?.edgeId,
    posStart: seg ? Math.max(0, Math.round(at - half)) : undefined,
    posEnd: seg ? Math.min(seg.length, Math.round(at + half)) : undefined,
    trainId: spec.trainId,
    start,
    durMin: lo * 60,
    durMax: hi * 60,
    durExpected: Math.round(((lo + hi) / 2) * 60),
    speedLimit: spec.kind === 'signal' ? (spec.speedLimit ?? 40) : undefined,
    anchors,
    note: spec.note,
  };
}
export function describeDisruption(d, trains) {
  const target =
    d.segment === undefined
      ? m('поезд {n}', { n: trains.find((t) => t.id === d.trainId)?.number ?? d.trainId })
      : d.track !== undefined
        ? m('перегон {seg}, путь {track}', { seg: segmentName(d.segment), track: d.track + 1 })
        : m('перегон {seg}', { seg: segmentName(d.segment) });
  const extra = d.kind === 'signal' ? m(', предупреждение {v} км/ч', { v: d.speedLimit }) : '';
  return m('{kind}: {target}, {lo}–{hi} мин{extra}', {
    kind: m(KIND_LABEL[d.kind]),
    target,
    lo: Math.round(d.durMin / 60),
    hi: Math.round(d.durMax / 60),
    extra,
  });
}
