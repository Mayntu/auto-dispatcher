/** Где находится поезд по плану в момент t (k — индекс по маршруту). */
export function locate(plan, trainId, t) {
  const tp = plan.trains[trainId];
  if (!tp) return { state: 'done' };
  const st = tp.stops;
  if (t < st[0].dep) return { state: 'before' };
  if (t >= st[st.length - 1].arr) return { state: 'done' };
  for (let k = 0; k < st.length; k++) {
    if (st[k].arr <= t && t < st[k].dep) return { state: 'at', k };
    if (k + 1 < st.length && st[k].dep <= t && t < st[k + 1].arr) return { state: 'seg', k };
  }
  return { state: 'done' };
}
/**
 * Доля пройденного перегона в момент t. Если поезд стоял на перегоне (n.stuck: u — где, from…to — когда),
 * до остановки и после неё он идёт равномерно, а между — стоит на месте.
 */
export function travelShare(dep, n, t) {
  const s = n.stuck;
  if (!s || !(s.from < s.to)) return (t - dep) / Math.max(1, n.arr - dep);
  if (t < s.from) return s.u * ((t - dep) / Math.max(1, s.from - dep));
  if (t < s.to) return s.u;
  return s.u + (1 - s.u) * ((t - s.to) / Math.max(1, n.arr - s.to));
}
export function positionsAt(section, trains, plan, t) {
  const out = [];
  for (const train of trains) {
    const tp = plan.trains[train.id];
    if (!tp || train.cancelled) continue;
    const st = tp.stops;
    const first = st[0];
    const last = st[st.length - 1];
    const base = { trainId: train.id, dir: train.dir };
    if (t < first.dep) {
      if (t >= first.dep - 600)
        out.push({
          ...base,
          km: section.stations[first.station].km,
          station: first.station,
          track: first.track,
          moving: false,
        });
      continue;
    }
    if (t >= last.arr) {
      if (t < last.arr + 300)
        out.push({
          ...base,
          km: section.stations[last.station].km,
          station: last.station,
          track: last.track,
          moving: false,
        });
      continue;
    }
    for (let k = 0; k < st.length; k++) {
      const s = st[k];
      if (s.arr <= t && t < s.dep) {
        out.push({ ...base, km: section.stations[s.station].km, station: s.station, track: s.track, moving: false });
        break;
      }
      const n = st[k + 1];
      if (n && s.dep <= t && t < n.arr) {
        const a = section.stations[s.station].km;
        const b = section.stations[n.station].km;
        const u = travelShare(s.dep, n, t);
        out.push({ ...base, km: a + (b - a) * u, moving: !(n.stuck && t >= n.stuck.from && t < n.stuck.to) });
        break;
      }
    }
  }
  return out;
}

/**
 * Положение поездов по данным поля с сервера (снимок 2 раза в секунду). Между снимками движущийся поезд
 * продлевается по своей скорости, чтобы ехал плавно, но не дальше конца перегона.
 */
export function livePositions(section, live, trains, t) {
  const dir = new Map(trains.map((x) => [x.id, x.dir]));
  const dt = Math.max(0, Math.min(5, t - live.t));
  return live.trains
    .filter((p) => dir.has(p.trainId))
    .map((p) => {
      const d = dir.get(p.trainId);
      const base = { trainId: p.trainId, dir: d, status: p.status };
      if (p.station !== undefined) return { ...base, km: p.km, station: p.station, track: p.track, moving: false };
      const g = section.segments[p.segment];
      const lo = g ? section.stations[g.from].km : -Infinity;
      const hi = g ? section.stations[g.to].km : Infinity;
      const km = p.moving ? Math.min(hi - 0.05, Math.max(lo + 0.05, p.km + (d * p.speed * dt) / 3600)) : p.km;
      return { ...base, km, moving: p.moving };
    });
}

/** Где поезда в момент t: с сервером — по полю, в демо — по действующему плану. */
export const currentPositions = (section, s, plan, t) => (s.live ? livePositions(section, s.live, s.trains, t) : positionsAt(section, s.trains, plan, t));
