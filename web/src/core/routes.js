/** Утверждённый план → маршруты приёма/отправления, которые ДЦ выставляет на поле перед проследованием. */
const SET_BEFORE = 150;

export function computeRoutes(trains, plan, now) {
  const out = [];
  for (const t of trains) {
    const tp = plan.trains[t.id];
    if (!tp || t.cancelled) continue;
    const last = tp.stops.length - 1;
    tp.stops.forEach((s, j) => {
      const add = (kind, time) => {
        if (time < now - 30 || time > now + SET_BEFORE) return;
        out.push({ trainId: t.id, station: s.station, kind, track: s.track, time, dir: t.dir });
      };
      if (j > 0) add('in', s.arr);
      if (j < last) add('out', s.dep);
    });
  }
  return out;
}
