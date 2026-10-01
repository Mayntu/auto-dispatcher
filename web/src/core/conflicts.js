import { m } from '../i18n/msg';
/** Обнаружение конфликтов (CDR) и разбор плана: скрещения, обгоны, вынужденные стоянки. */
import { MIN_OCC, oppositeClash } from './scheduler';
import { segmentName, stationName } from './section';
function segmentIntervals(section, plan, trains) {
  const out = section.segments.map(() => []);
  for (const t of trains) {
    const tp = plan.trains[t.id];
    if (!tp || t.cancelled) continue;
    for (let j = 0; j + 1 < tp.stops.length; j++) {
      const a = tp.stops[j];
      const b = tp.stops[j + 1];
      out[Math.min(a.station, b.station)].push({
        trainId: t.id,
        dir: t.dir,
        enter: a.dep,
        exit: b.arr,
        stuck: b.stuck,
        kmFrom: section.stations[a.station].km,
        kmTo: section.stations[b.station].km,
      });
    }
  }
  return out;
}
function kmAt(iv, t) {
  const f = iv.exit > iv.enter ? (t - iv.enter) / (iv.exit - iv.enter) : 0;
  return iv.kmFrom + (iv.kmTo - iv.kmFrom) * Math.min(1, Math.max(0, f));
}
function intersect(a, b) {
  const t0 = Math.max(a.enter, b.enter);
  const t1 = Math.min(a.exit, b.exit);
  const f0 = kmAt(a, t0) - kmAt(b, t0);
  const f1 = kmAt(a, t1) - kmAt(b, t1);
  const t = f0 !== f1 && Math.sign(f0) !== Math.sign(f1) ? t0 + ((t1 - t0) * f0) / (f0 - f1) : (t0 + t1) / 2;
  return { t, km: kmAt(a, t) };
}
export function detectConflicts(section, settings, plan, trains, disruptions = []) {
  const num = new Map(trains.map((t) => [t.id, t.number]));
  const out = [];
  let id = 0;
  const H = settings.headwaySec;
  segmentIntervals(section, plan, trains).forEach((list, seg) => {
    for (let i = 0; i < list.length; i++) {
      for (let j = i + 1; j < list.length; j++) {
        const a = list[i];
        const b = list[j];
        if (a.dir !== b.dir) {
          if (oppositeClash(section, disruptions, seg, a, b, 0) > 1) {
            const p = intersect(a, b);
            out.push({
              id: `C${++id}`,
              kind: 'meet',
              trains: [a.trainId, b.trainId],
              segment: seg,
              time: p.t,
              km: p.km,
              text: m('Встречные {a} и {b} на перегоне {seg}', { a: num.get(a.trainId), b: num.get(b.trainId), seg: segmentName(seg) }),
            });
          }
        } else {
          const [f, s] = a.enter <= b.enter ? [a, b] : [b, a];
          if (s.exit < f.exit + 1 || s.enter - f.enter < H * 0.5) {
            out.push({
              id: `C${++id}`,
              kind: 'follow',
              trains: [f.trainId, s.trainId],
              segment: seg,
              time: s.enter,
              km: s.kmFrom,
              text: m('Попутные {a} и {b} ближе межпоездного интервала ({seg})', { a: num.get(f.trainId), b: num.get(s.trainId), seg: segmentName(seg) }),
            });
          }
        }
      }
    }
  });
  section.stations.forEach((station, si) => {
    if (station.terminal) return;
    const visits = [];
    for (const t of trains) {
      if (t.cancelled) continue;
      for (const s of plan.trains[t.id]?.stops ?? []) {
        if (s.station === si) visits.push({ id: t.id, from: s.arr, to: Math.max(s.dep, s.arr + MIN_OCC) });
      }
    }
    const seen = new Set();
    for (const v of visits) {
      const here = visits.filter((w) => w.from <= v.from && v.from < w.to);
      if (here.length > station.tracks) {
        const key = here
          .map((w) => w.id)
          .sort()
          .join('|');
        if (seen.has(key)) continue;
        seen.add(key);
        out.push({
          id: `C${++id}`,
          kind: 'capacity',
          trains: here.map((w) => w.id),
          station: si,
          time: v.from,
          km: station.km,
          text: m('{st}: {n} поезда на {tracks} путях ({list})', { st: stationName(si), n: here.length, tracks: station.tracks, list: here.map((w) => num.get(w.id)).join(', ') }),
        });
      }
    }
  });
  return out.sort((a, b) => a.time - b.time);
}
/** Где скрещиваются встречные пары: ключ "a|b" → индекс раздельного пункта. */
export function computeMeets(plan, trains) {
  const meets = new Map();
  const tol = 30;
  for (let i = 0; i < trains.length; i++) {
    for (let j = i + 1; j < trains.length; j++) {
      const a = trains[i];
      const b = trains[j];
      if (a.dir === b.dir) continue;
      const pa = plan.trains[a.id];
      const pb = plan.trains[b.id];
      if (!pa || !pb) continue;
      for (const sa of pa.stops) {
        const sb = pb.stops.find((s) => s.station === sa.station);
        if (sb && sa.arr <= sb.dep + tol && sb.arr <= sa.dep + tol) {
          meets.set([a.id, b.id].sort().join('|'), sa.station);
          break;
        }
      }
    }
  }
  return meets;
}
/** Внеплановые стоянки (ожидание скрещения, обгона, окончания сбоя) длиннее минуты. */
export function forcedWaits(plan, trains) {
  const out = [];
  for (const t of trains) {
    const tp = plan.trains[t.id];
    if (!tp || t.cancelled) continue;
    const last = tp.stops.length - 1;
    tp.stops.forEach((s, j) => {
      if (j === last) return;
      const dwell = j > 0 && s.planned ? t.dwell : 0;
      const from = j === 0 ? t.departure : s.arr + dwell;
      const extra = s.dep - from;
      if (extra > 60) out.push({ trainId: t.id, station: s.station, from, to: s.dep, min: extra / 60 });
    });
  }
  return out;
}
