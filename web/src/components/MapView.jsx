/**
 * Карта участка в стиле навигатора. Узлы (nodes) — по координатам pos_x/pos_y, станции — овалы с путями внутри.
 * Перегоны (edges) — по два пути со съездами (turnouts) между ними; каждый путь окрашен по «загруженности»:
 * зелёный — по графику, жёлтый — копятся задержки, красный — перекрытие или большие опоздания.
 * Помеху можно бросить на конкретный путь — поезда пойдут по соседнему. Поезда анимируются покадрово.
 */
import { Info, Locate, Minus, Plus, X } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { currentPositions } from '../core/positions';
import { blockSets, durationOf, isBlocking, ownTrack } from '../core/scheduler';
import { CATEGORIES, edgeFraction, SECTION, stationName } from '../core/section';
import { fmtHM } from '../core/time';
import { isActive, runningPlan } from '../engine/engine';
import { engine, useSmoothNow } from '../engine/store';
import { can } from '../engine/roles';
import { acceptsDrag, currentDragKind, EVENT_BY_KIND, readDragKind } from './EventPalette';
import { useDismiss, useSize } from './ui';
import { makeTrees, TreesLayer } from './MapScenery';
import { t as tr, useLang } from '../i18n';

const MIN_ZOOM = 0.8;
const MAX_ZOOM = 3;
const HIT = 30;
/** Сколько вагонов рисовать за локомотивом. */
const WAGONS = { express: 4, pass: 4, suburb: 3, freightFast: 5, freight: 6 };
/** Цвет пути по задержке, которая возникает на этом перегоне: красный оставлен только для перекрытий. */
const traffic = (delayMin) => (delayMin >= 10 ? 'orange' : delayMin >= 2 ? 'yellow' : 'green');

/** Координаты узлов из БД → «мир» карты (масштаб 1), участок вписан в экран. */
function layout(w, h) {
  const st = SECTION.stations;
  const xs = st.map((s) => s.x);
  const ys = st.map((s) => s.y);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const padX = 110;
  const top = 150;
  const bottom = 60;
  const k = (w - 2 * padX) / (maxX - minX || 1);
  const free = h - top - bottom;
  const ky = maxY > minY ? Math.min(k * 4, (free * 0.6) / (maxY - minY)) : 0;
  const cy = top + free / 2;
  const pts = st.map((s) => ({ x: padX + (s.x - minX) * k, y: cy + (s.y - (minY + maxY) / 2) * ky }));
  const box = {
    x0: Math.min(...pts.map((p) => p.x)),
    x1: Math.max(...pts.map((p) => p.x)),
    y0: Math.min(...pts.map((p) => p.y)),
    y1: Math.max(...pts.map((p) => p.y)),
  };
  return { pts, box };
}

/** Не даём участку уйти за край экрана: линия всегда пересекает центральную область карты. */
function clampView(v, box, w, h) {
  const left = w * 0.3;
  const right = w * 0.7;
  const top = Math.max(180, h * 0.3);
  const bottom = h * 0.75;
  let { x, y } = v;
  if (box.x1 * v.k + x < left) x = left - box.x1 * v.k;
  if (box.x0 * v.k + x > right) x = right - box.x0 * v.k;
  if (box.y1 * v.k + y < top) y = top - box.y1 * v.k;
  if (box.y0 * v.k + y > bottom) y = bottom - box.y0 * v.k;
  return { k: v.k, x, y };
}

/** Экранная геометрия: точки станций, направление и нормаль каждого перегона, размеры овалов станций. */
function geometry(L, view) {
  const z = Math.min(1.8, Math.max(1, view.k));
  const S = L.pts.map((p) => ({ x: p.x * view.k + view.x, y: p.y * view.k + view.y }));
  const segs = SECTION.segments.map((g) => {
    const a = S[g.from];
    const b = S[g.to];
    const len = Math.hypot(b.x - a.x, b.y - a.y) || 1;
    const u = { x: (b.x - a.x) / len, y: (b.y - a.y) / len };
    return { a, b, u, n: { x: -u.y, y: u.x }, angle: Math.atan2(u.y, u.x) };
  });
  const gap = 5 * z;
  const sp = 7 * z;
  const ovals = SECTION.stations.map((st) => {
    const half = ((st.tracks - 1) / 2) * sp;
    return { rx: 22 * z, ry: half + 10 * z, lines: Array.from({ length: st.tracks }, (_, i) => -half + i * sp) };
  });
  /** Точка на пути track перегона seg на доле f (0 — станция from, 1 — станция to). */
  const onTrack = (seg, track, f) => {
    const g = segs[seg];
    const off = track === undefined ? 0 : track === 0 ? -gap : gap;
    return { x: g.a.x + (g.b.x - g.a.x) * f + g.n.x * off, y: g.a.y + (g.b.y - g.a.y) * f + g.n.y * off };
  };
  return { z, S, segs, gap, ovals, onTrack };
}

/** Наклон карты в режиме 3D (рад) и «расстояние до камеры» для перспективы, px. */
const TILT_3D = 0.9;
const CAMERA = 1100;

/**
 * Перспектива как у наклонённой карты в навигаторе: дальний край плоскости уменьшается, ближний — растёт.
 * fwd — точка плоскости → экран (s — масштаб в этой точке), inv — обратно, для кликов и бросков событий.
 */
function projector(w, h, tilt) {
  const cx = w / 2;
  const cy = h * 0.5;
  const sin = Math.sin(tilt);
  const cos = Math.cos(tilt);
  const fwd = (p) => {
    const v = p.y - cy;
    const k = CAMERA / (CAMERA - v * sin);
    return { x: cx + (p.x - cx) * k, y: cy + v * cos * k, s: k };
  };
  const inv = (q) => {
    const V = q.y - cy;
    const v = (V * CAMERA) / (cos * CAMERA + V * sin);
    const k = CAMERA / (CAMERA - v * sin);
    return { x: cx + (q.x - cx) / k, y: cy + v };
  };
  return { fwd, inv, cx, cy, tilt, cos };
}

/** Та же геометрия, но в экранных координатах после перспективы. */
function projectGeo(flat, pr) {
  const P = pr.fwd;
  const S = flat.S.map(P);
  const segs = flat.segs.map((g) => {
    const a = P(g.a);
    const b = P(g.b);
    const len = Math.hypot(b.x - a.x, b.y - a.y) || 1;
    const u = { x: (b.x - a.x) / len, y: (b.y - a.y) / len };
    return { a, b, u, n: { x: -u.y, y: u.x }, angle: Math.atan2(u.y, u.x) };
  });
  const ovals = flat.ovals.map((o, i) => {
    const k = S[i].s;
    return { rx: o.rx * k, ry: o.ry * k * pr.cos, lines: o.lines.map((l) => l * k * pr.cos), k };
  });
  return { ...flat, S, segs, ovals, onTrack: (seg, track, f) => P(flat.onTrack(seg, track, f)), P, flat, pr };
}

/**
 * «Пробки» по путям: сколько задержки поезда набирают именно на этом перегоне (ожидание перед ним
 * и замедление на нём) в ближайший час. Опоздание, которое поезд просто везёт с собой, не учитывается —
 * иначе краснели бы все перегоны после места сбоя.
 */
function trackLoad(plan, baseline, trains, now) {
  const dir = new Map(trains.map((t) => [t.id, t.dir]));
  const load = SECTION.segments.map(() => [0, 0]);
  for (const tp of Object.values(plan.trains)) {
    const bp = baseline.trains[tp.trainId];
    if (!bp) continue;
    const tr = ownTrack(dir.get(tp.trainId));
    for (let j = 0; j + 1 < tp.stops.length; j++) {
      const a = tp.stops[j];
      const b = tp.stops[j + 1];
      if (b.arr < now || a.dep > now + 3600) continue;
      const seg = Math.min(a.station, b.station);
      const delayIn = j === 0 ? 0 : a.arr - bp.stops[j].arr;
      const delayOut = b.arr - bp.stops[j + 1].arr;
      load[seg][tr] = Math.max(load[seg][tr], (delayOut - delayIn) / 60);
    }
  }
  return load;
}

/** Закрытые в момент t части путей перегона: [{ track, f0, f1 }], f — доля от станции from. */
function closuresAt(disruptions, seg, t) {
  const sets = blockSets(SECTION, disruptions, seg);
  const out = [];
  for (const p of sets.partial) if (t >= p.w[0] && t < p.w[1]) out.push({ track: p.track, f0: p.range[0], f1: p.range[1] });
  for (const w of sets.full) if (t >= w[0] && t < w[1]) out.push({ track: 0, f0: 0, f1: 1 }, { track: 1, f0: 0, f1: 1 });
  return out;
}

const closedAt = (cl, track, f) => cl.some((c) => c.track === track && f > c.f0 - 1e-6 && f < c.f1 + 1e-6);

/** Путь поезда в точке f: свой, а на закрытой части своего — соседний (уходит туда по съезду и возвращается). */
const trackAt = (cl, own, f) => (closedAt(cl, own, f) && !closedAt(cl, 1 - own, f) ? 1 - own : own);

/** Положение поезда на экране: точка, угол и путь. */
function trainPlace(p, geo, disruptions, t) {
  if (p.station !== undefined) {
    const st = SECTION.stations[p.station];
    const o = geo.ovals[p.station];
    const tr = Math.min(p.track ?? 0, st.tracks - 1);
    const c = geo.S[p.station];
    const seg = geo.segs[Math.min(p.station, geo.segs.length - 1)];
    return { x: c.x, y: c.y + o.lines[tr], angle: seg.angle, atStation: true, s: c.s ?? 1 };
  }
  const st = SECTION.stations;
  const seg = Math.max(0, Math.min(st.length - 2, st.findIndex((s, j) => j < st.length - 1 && p.km <= st[j + 1].km)));
  const f = (p.km - st[seg].km) / (st[seg + 1].km - st[seg].km || 1);
  const own = ownTrack(p.dir);
  const track = SECTION.segments[seg].tracks >= 2 ? trackAt(closuresAt(disruptions, seg, t), own, f) : undefined;
  const pt = geo.onTrack(seg, track, f);
  return { ...pt, s: pt.s ?? 1, angle: geo.segs[seg].angle, atStation: false, seg, track, f, wrong: track !== undefined && track !== own };
}

const CAR_W = 11;
const CAR_GAP = 2;

/**
 * Где стоит каждый вагон: путь от локомотива назад по рельсам — до станции, через неё и по предыдущему перегону.
 * Возвращает смещение центра вагона от локомотива и его поворот, чтобы состав плавно повторял изгиб пути.
 */
function tailPoses(pl, dir, geo, scale, wagons) {
  const own = ownTrack(dir);
  const pts = [{ x: pl.x, y: pl.y }];
  pts.push(geo.onTrack(pl.seg, pl.track, dir === 1 ? 0 : 1));
  pts.push(geo.S[dir === 1 ? pl.seg : pl.seg + 1]);
  const prev = dir === 1 ? pl.seg - 1 : pl.seg + 1;
  if (prev >= 0 && prev < geo.segs.length) {
    const tr = SECTION.segments[prev].tracks >= 2 ? own : undefined;
    pts.push(geo.onTrack(prev, tr, dir === 1 ? 1 : 0));
    pts.push(geo.onTrack(prev, tr, dir === 1 ? 0 : 1));
  }
  // Точка на расстоянии d назад от головы и направление движения в ней.
  const at = (d) => {
    let left = d;
    for (let i = 0; i + 1 < pts.length; i++) {
      const a = pts[i];
      const b = pts[i + 1];
      const len = Math.hypot(b.x - a.x, b.y - a.y);
      if (len < 1e-6) continue;
      if (left <= len || i + 2 === pts.length) {
        const k = Math.min(1, left / len);
        return { x: a.x + (b.x - a.x) * k, y: a.y + (b.y - a.y) * k, deg: (Math.atan2(a.y - b.y, a.x - b.x) * 180) / Math.PI };
      }
      left -= len;
    }
    return { x: pl.x, y: pl.y, deg: 0 };
  };
  return Array.from({ length: wagons }, (_, i) => {
    const q = at((17.5 + i * (CAR_W + CAR_GAP)) * scale);
    return { dx: q.x - pl.x, dy: q.y - pl.y, deg: q.deg };
  });
}

export function MapView({ s, snapshot, selected, onSelect, armed, onPlace, allowEvents = false }) {
  const [ref, size] = useSize();
  const [view, setView] = useState({ k: 1, x: 0, y: 0 });
  const [hover, setHover] = useState(null);
  const [grabbing, setGrabbing] = useState(false);
  const [legend, setLegend] = useState(false);
  const [is3d, setIs3d] = useState(false);
  const [tilt, setTilt] = useState(0);
  const [stationCard, setStationCard] = useState(null);
  const legendRef = useDismiss(legend, () => setLegend(false));
  const drag = useRef(null);
  const w = Math.max(360, size.w);
  const h = Math.max(260, size.h);
  const L = useMemo(() => layout(w, h), [w, h]);
  /** На узкой карте подписи станций короткие: только название, подробности — во всплывающей подсказке. */
  const compact = w < 900;
  const flat = useMemo(() => geometry(L, view), [L, view]);
  const pr = useMemo(() => projector(w, h, tilt), [w, h, tilt]);
  const geo = useMemo(() => projectGeo(flat, pr), [flat, pr]);
  const trees = useMemo(() => makeTrees(L, w, h), [L, w, h]);

  // Плавный переход 2D ↔ 3D.
  useEffect(() => {
    const target = is3d ? TILT_3D : 0;
    const from = tilt;
    const t0 = performance.now();
    let id;
    const step = () => {
      const k = Math.min(1, (performance.now() - t0) / 450);
      const e = 1 - (1 - k) ** 3;
      setTilt(from + (target - from) * e);
      if (k < 1) id = requestAnimationFrame(step);
    };
    id = requestAnimationFrame(step);
    return () => cancelAnimationFrame(id);
  }, [is3d]);
  const plan = runningPlan(s);
  const t = snapshot?.t ?? s.now;
  const live = !snapshot && allowEvents;
  const active = s.disruptions.filter((d) => isActive(d, t));
  const blocks = snapshot ? snapshot.blocks : active.filter(isBlocking);
  const slow = new Set(snapshot ? snapshot.limited : active.filter((d) => d.kind === 'signal').map((d) => d.segment));
  const load = useMemo(() => trackLoad(plan, s.baseline, s.trains, s.now), [plan, s.baseline, s.trains, s.now]);
  const conflicts = !snapshot
    ? (s.forecast ? s.forecastConflicts : s.conflicts).filter((c) => c.time >= s.now && c.time < s.now + 3600).slice(0, 6)
    : [];

  const update = (fn) => setView((v) => clampView(fn(v), L.box, w, h));
  const zoomAt = (sx, sy, factor) => {
    // В 3D точка под курсором берётся на плоскости карты.
    const { x: mx, y: my } = pr.inv({ x: sx, y: sy });
    update((v) => {
      const k = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, v.k * factor));
      return { k, x: mx - ((mx - v.x) * k) / v.k, y: my - ((my - v.y) * k) / v.k };
    });
  };

  // Колесо мыши — масштаб карты, а не страницы (нужен непассивный обработчик).
  useEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    const onWheel = (e) => {
      e.preventDefault();
      const r = el.getBoundingClientRect();
      zoomAt(e.clientX - r.left, e.clientY - r.top, Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0015)));
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => el.removeEventListener('wheel', onWheel);
  });

  useEffect(() => setView({ k: 1, x: 0, y: 0 }), [w, h]);

  // ── станции: кто на каком пути стоит и кто прибудет следующим ──
  const { occupancy, nextArrival } = useMemo(() => {
    const occ = SECTION.stations.map(() => new Map());
    const color = new Map(s.trains.map((x) => [x.id, CATEGORIES[x.category].color]));
    for (const p of currentPositions(SECTION, s, plan, t)) {
      if (p.station !== undefined) occ[p.station].set(Math.min(p.track ?? 0, SECTION.stations[p.station].tracks - 1), color.get(p.trainId));
    }
    const next = SECTION.stations.map(() => null);
    for (const tr of s.trains) {
      const tp = plan.trains[tr.id];
      if (!tp || tr.cancelled) continue;
      tp.stops.forEach((st, j) => {
        if (j === 0 || st.arr <= t) return;
        if (!next[st.station] || st.arr < next[st.station].arr) next[st.station] = { arr: st.arr, number: tr.number };
      });
    }
    return { occupancy: occ, nextArrival: next };
  }, [s.trains, plan, t]);

  // ── подписи станций: над станцией, а если наезжает на соседнюю — под ней; всегда в пределах карты ──
  const lang = useLang();
  const labels = useMemo(() => {
    const placed = [];
    return SECTION.stations.map((st, i) => {
      const c = geo.S[i];
      const o = geo.ovals[i];
      const next = nextArrival[i];
      const name = tr(st.short);
      const sub =
        tr('занято {a}/{b}', { a: occupancy[i].size, b: st.tracks }) +
        (next ? ` · → ${tr('{n} в {t}', { n: next.number, t: fmtHM(next.arr) })}` : '');
      const wpx = compact ? name.length * 8.4 + 24 : Math.max(name.length * 9 + 30, sub.length * 6.2 + 24);
      // Справа — колонка масштаба: подпись не заходит под неё.
      const x = Math.min(w - wpx / 2 - 64, Math.max(wpx / 2 + 8, c.x));
      const rectAt = (y) => ({ x0: x - wpx / 2 - 6, x1: x + wpx / 2 + 6, y0: y - (compact ? 13 : 18), y1: y + (compact ? 13 : 20) });
      const hits = (r) => placed.some((p) => r.x0 < p.x1 && r.x1 > p.x0 && r.y0 < p.y1 && r.y1 > p.y0);
      const above = c.y - o.ry - (compact ? 22 : 34);
      const under = c.y + o.ry + (compact ? 24 : 38);
      const below = hits(rectAt(above)) && !hits(rectAt(under));
      const y = below ? under : above;
      placed.push(rectAt(y));
      return { st, i, x, y, below, sub, wpx, name };
    });
  }, [geo, occupancy, nextArrival, w, compact, lang]);

  // ── маршрут выбранного поезда (остановки впереди) ──
  const selPlan = plan.trains[selected];
  const ahead = selPlan ? selPlan.stops.filter((st) => st.arr > t) : [];

  // ── куда упадёт событие ──
  const local = (e) => {
    const r = ref.current.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  };
  const targetAt = (kind, sx, sy) => {
    const { x, y } = pr.inv({ x: sx, y: sy });
    if (EVENT_BY_KIND[kind].target === 'segment') {
      let best = null;
      flat.segs.forEach((g, i) => {
        const vx = g.b.x - g.a.x;
        const vy = g.b.y - g.a.y;
        const f = Math.min(0.95, Math.max(0.05, ((x - g.a.x) * vx + (y - g.a.y) * vy) / (vx * vx + vy * vy)));
        const px = g.a.x + vx * f;
        const py = g.a.y + vy * f;
        const d = Math.hypot(px - x, py - y);
        if (d < HIT * 2 && (!best || d < best.d)) {
          const side = (x - px) * g.n.x + (y - py) * g.n.y;
          best = { d, i, f, track: side < 0 ? 0 : 1 };
        }
      });
      if (!best) return null;
      const seg = SECTION.segments[best.i];
      const pos = Math.round(seg.reversed ? (1 - best.f) * seg.length : best.f * seg.length);
      const track = seg.tracks >= 2 ? best.track : undefined;
      return { segment: best.i, track, pos, point: geo.onTrack(best.i, track, best.f) };
    }
    let best = null;
    for (const p of currentPositions(SECTION, s, plan, s.now)) {
      const q = trainPlace(p, flat, s.disruptions, s.now);
      const d = Math.hypot(q.x - x, q.y - y);
      if (d < HIT + 14 && (!best || d < best.d)) best = { d, p, q };
    }
    return best ? { trainId: best.p.trainId, point: geo.P(best.q) } : null;
  };
  const place = (kind, e) => {
    const { x, y } = local(e);
    const tg = targetAt(kind, x, y);
    setHover(null);
    onPlace(kind, tg && { segment: tg.segment, track: tg.track, trainId: tg.trainId, pos: tg.pos }, e.clientX, e.clientY);
  };

  const onPointerDown = (e) => {
    if (e.button !== 0) return;
    drag.current = { sx: e.clientX, sy: e.clientY, vx: view.x, vy: view.y, moved: false };
  };
  const onPointerMove = (e) => {
    const d = drag.current;
    if (d) {
      const dx = e.clientX - d.sx;
      const dy = e.clientY - d.sy;
      if (!d.moved && Math.abs(dx) + Math.abs(dy) > 4) {
        d.moved = true;
        setGrabbing(true);
      }
      if (d.moved) update((v) => ({ ...v, x: d.vx + dx, y: d.vy + dy }));
    } else if (armed && live) {
      const p = local(e);
      setHover({ target: targetAt(armed, p.x, p.y) });
    }
  };
  const onPointerUp = (e) => {
    const d = drag.current;
    drag.current = null;
    setGrabbing(false);
    // Клик по карте после выбора события или отпускание перетаскиваемой с панели метки.
    if ((!d || !d.moved) && armed && live) place(armed, e);
  };

  const hl = hover?.target;
  const line = (p, q, cls, key) => <line key={key} x1={p.x} y1={p.y} x2={q.x} y2={q.y} className={cls} />;

  return (
    <div
      className={`mapview ${armed ? 'armed' : ''} ${grabbing ? 'grabbing' : ''}`}
      ref={ref}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerLeave={() => {
        drag.current = null;
        setGrabbing(false);
        setHover(null);
      }}
      onDragOver={(e) => {
        if (!live || !acceptsDrag(e)) return;
        e.preventDefault();
        const p = local(e);
        setHover({ target: targetAt(currentDragKind() ?? 'livestock', p.x, p.y) });
      }}
      onDragLeave={() => setHover(null)}
      onDrop={(e) => {
        e.preventDefault();
        const kind = readDragKind(e);
        if (kind && live) place(kind, e);
      }}
    >
      <div
        className="map-ground"
        style={{
          backgroundSize: `${260 * view.k}px ${260 * view.k}px`,
          backgroundPosition: `${view.x + w}px ${view.y + h}px`,
          transformOrigin: `${pr.cx + w}px ${pr.cy + h}px`,
          transform: `perspective(${CAMERA}px) rotateX(${tilt}rad)`,
        }}
      />
      {tilt > 0.01 && <div className="map-fog" style={{ opacity: tilt / TILT_3D }} />}
      <svg width={w} height={h} className="map-svg" role="img" aria-label={tr('Карта участка')}>

        {/* степь и рощи: в 3D деревья встают */}
        <TreesLayer trees={trees} view={view} project={pr.fwd} lift={tilt / TILT_3D} w={w} h={h} />

        {/* перегоны: два пути, съезды между ними */}
        {SECTION.segments.map((g, i) => {
          const tracks = g.tracks >= 2 ? [0, 1] : [undefined];
          return (
            <g key={g.edgeId}>
              {tracks.map((tr) => {
                const cl = tr !== undefined ? closuresAt(s.disruptions, i, s.now) : [];
                const bounds = [0, ...g.cuts, 1];
                return (
                  <g key={tr ?? 'single'}>
                    {bounds.slice(0, -1).map((f0, k) => {
                      const f1 = bounds[k + 1];
                      const mid = (f0 + f1) / 2;
                      const p0 = geo.onTrack(i, tr, f0);
                      const p1 = geo.onTrack(i, tr, f1);
                      // Закрытая часть пути — серый пунктир; та же часть соседнего пути работает за двоих.
                      const state =
                        tr !== undefined && closedAt(cl, tr, mid)
                          ? 'closed'
                          : tr !== undefined && closedAt(cl, 1 - tr, mid)
                            ? 'single'
                            : slow.has(i)
                              ? 'yellow'
                              : traffic(load[i][tr ?? 0]);
                      return (
                        <g key={k}>
                          {line(p0, p1, 'rail-case')}
                          {line(p0, p1, `rail ${state}`)}
                        </g>
                      );
                    })}
                  </g>
                );
              })}
              {g.tracks >= 2 &&
                g.turnouts.map((tn) => {
                  const f = edgeFraction(g, tn.pos);
                  const c = flat.onTrack(i, undefined, f);
                  const { u, n } = flat.segs[i];
                  const hx = 11 * flat.z;
                  const gp = flat.gap;
                  const P = (a, b) => geo.P({ x: c.x + u.x * a + n.x * b, y: c.y + u.y * a + n.y * b });
                  return (
                    <g key={tn.id} className="crossover">
                      <title>{tr('Съезд (стрелки №{n})', { n: tn.id })}</title>
                      {line(P(-hx, -gp), P(hx, gp), '', 'a')}
                      {line(P(-hx, gp), P(hx, -gp), '', 'b')}
                    </g>
                  );
                })}
            </g>
          );
        })}

        {/* автоблокировка: блок-участки, проходные светофоры, установленное направление */}
        <AutoBlockLayer s={s} plan={plan} t={t} geo={geo} flat={flat} />

        {/* цель броска */}
        {hl?.segment !== undefined && line(geo.onTrack(hl.segment, hl.track, 0), geo.onTrack(hl.segment, hl.track, 1), 'rail-target')}

        {/* перекрытия: на одном пути или на обоих */}
        {blocks.map((d, i) => {
          const g = SECTION.segments[d.segment];
          const f0 = edgeFraction(g, d.posStart ?? 0);
          const f1 = edgeFraction(g, d.posEnd ?? g.length);
          const tracks = g.tracks >= 2 && d.track !== undefined ? [d.track] : g.tracks >= 2 ? [0, 1] : [undefined];
          return (
            <g key={d.id ?? i}>
              {tracks.map((tr) =>
                line(geo.onTrack(d.segment, tr, Math.min(f0, f1)), geo.onTrack(d.segment, tr, Math.max(f0, f1)), 'rail-blocked', tr ?? 's'),
              )}
            </g>
          );
        })}

        {/* составы — под станциями: въезжая, поезд «скрывается» в станции */}
        <TrainsLayer
          part="body"
          s={s}
          plan={plan}
          snapshot={snapshot}
          geo={geo}
          selected={selected}
          ahead={ahead}
          armed={armed}
          targetId={hl?.trainId}
          onSelect={onSelect}
        />

        {/* станции: пути внутри светятся цветом стоящего поезда, кольцо — заполненность, ореол — скорое прибытие */}
        {SECTION.stations.map((st, i) => {
          const c = geo.S[i];
          const o = geo.ovals[i];
          const busy = occupancy[i];
          const util = busy.size / st.tracks;
          const soon = nextArrival[i] && nextArrival[i].arr - s.now < 180;
          return (
            <g
              key={st.id}
              transform={`translate(${c.x} ${c.y})`}
              className={`station-oval ${st.kind}`}
              onClick={(e) => {
                e.stopPropagation();
                setStationCard((v) => (v === i ? null : i));
              }}
            >
              {soon && <ellipse rx={o.rx + 6} ry={o.ry + 6} className="st-halo" />}
              {tilt > 0.01 && <ellipse cy={7 * o.k * Math.sin(tilt)} rx={o.rx} ry={o.ry} className="st-side" />}
              <ellipse rx={o.rx + 5} ry={o.ry + 5} className="st-ring-bg" />
              {util > 0 && (
                <path
                  d={ringArc(o.rx + 5, o.ry + 5, util)}
                  className={`st-ring ${util >= 1 ? 'full' : util >= 0.5 ? 'half' : 'low'}`}
                />
              )}
              <ellipse rx={o.rx} ry={o.ry} className="st-body" />
              {st.terminal && <ellipse rx={o.rx - 4} ry={o.ry - 4} className="st-inner" />}
              {o.lines.map((y, k) => {
                const half = o.rx * Math.sqrt(Math.max(0, 1 - (y / o.ry) ** 2)) - 6;
                const color = busy.get(k);
                return <line key={k} x1={-half} x2={half} y1={y} y2={y} className={color ? 'st-line busy' : 'st-line'} style={color ? { stroke: color } : undefined} />;
              })}
            </g>
          );
        })}

        {/* подписи станций и время прибытия выбранного поезда */}
        {labels.map(({ st, i, x: lx, y: ly, below, sub, wpx, name }) => {
          const eta = ahead.find((x) => x.station === i);
          return (
            <g key={st.id} transform={`translate(${lx} ${ly})`} className="label">
              <title>{`${tr(stationName(i))}: ${sub}`}</title>
              {compact ? (
                <>
                  <rect x={-wpx / 2} y={-13} width={wpx} height={26} rx={13} className={`label-bg ${st.kind}`} />
                  <text y={5} textAnchor="middle" className={`label-text ${st.kind}`}>
                    {name}
                  </text>
                </>
              ) : (
                <>
                  <rect x={-wpx / 2} y={-18} width={wpx} height={38} rx={12} className={`label-bg ${st.kind}`} />
                  <text y={-1} textAnchor="middle" className={`label-text ${st.kind}`}>
                    {name}
                  </text>
                  <text y={13} textAnchor="middle" className={`label-sub ${st.kind}`}>
                    {sub}
                  </text>
                </>
              )}
              {eta && (
                <g transform={`translate(0 ${below ? 36 : -34})`} className="eta">
                  <rect x={-30} y={-12} width={60} height={22} rx={11} />
                  <text y={4} textAnchor="middle">
                    {fmtHM(eta.arr)}
                  </text>
                </g>
              )}
            </g>
          );
        })}

        {blocks.map((d, i) => {
          const g = SECTION.segments[d.segment];
          const f = (edgeFraction(g, d.posStart ?? 0) + edgeFraction(g, d.posEnd ?? g.length)) / 2;
          const p = geo.onTrack(d.segment, g.tracks >= 2 ? d.track : undefined, f);
          const type = EVENT_BY_KIND[d.kind];
          const until = d.start !== undefined ? fmtHM(d.start + durationOf(d, 'expected')) : '';
          const dy = d.track === 1 ? 50 : 0;
          const label = (d.track !== undefined ? `${tr('путь {n}', { n: d.track + 1 })} · ` : '') + tr('до {t}', { t: until });
          // ширина метки — по длине подписи (на разных языках она разная)
          const pw = Math.max(120, label.length * 7.6 + 46);
          return (
            <g key={d.id ?? i} transform={`translate(${p.x} ${p.y})`} className="pin">
              <path d={d.track === 1 ? 'M0 4 L-7 16 L7 16 Z' : 'M0 -4 L-7 -16 L7 -16 Z'} />
              <rect x={-pw / 2} y={-48 + dy + (d.track === 1 ? 14 : 0)} width={pw} height={32} rx={16} />
              <type.Icon x={-pw / 2 + 10} y={-41 + dy + (d.track === 1 ? 14 : 0)} width={18} height={18} color="#fff" strokeWidth={2.4} />
              <text x={11} y={-27 + dy + (d.track === 1 ? 14 : 0)} textAnchor="middle">
                {label}
              </text>
            </g>
          );
        })}

        {conflicts.map((c) => {
          const st = SECTION.stations;
          const seg = Math.max(0, Math.min(st.length - 2, st.findIndex((x, j) => j < st.length - 1 && c.km <= st[j + 1].km)));
          const p = geo.onTrack(seg, undefined, (c.km - st[seg].km) / (st[seg + 1].km - st[seg].km || 1));
          return (
            <g key={c.id} transform={`translate(${p.x} ${p.y})`} className="conflict">
              <title>{tr(c.text)}</title>
              <circle r={18} className="pulse" />
              <circle r={11} className="dot" />
              <text y={5} textAnchor="middle" className="mark">
                !
              </text>
              <text y={32} textAnchor="middle" className="when">
                {fmtHM(c.time)}
              </text>
            </g>
          );
        })}

        <TrainsLayer
          part="top"
          s={s}
          plan={plan}
          snapshot={snapshot}
          geo={geo}
          selected={selected}
          ahead={ahead}
          armed={armed}
          targetId={hl?.trainId}
          onSelect={onSelect}
        />

        {hl?.point && <circle cx={hl.point.x} cy={hl.point.y} r={22} className="drop-ring" />}
      </svg>

      {stationCard !== null && <StationCard i={stationCard} s={s} plan={plan} t={t} at={geo.S[stationCard]} w={w} onClose={() => setStationCard(null)} />}

      <div className="zoom" onPointerDown={(e) => e.stopPropagation()}>
        <div className="legend-wrap" ref={legendRef} onMouseEnter={() => setLegend(true)} onMouseLeave={() => setLegend(false)}>
          <button title={tr('Обозначения на карте')} className={legend ? 'on' : ''} onClick={() => setLegend(true)}>
            <Info size={18} />
          </button>
          {legend && (
            <div className="map-legend-box" role="note" aria-label={tr('Обозначения')}>
              <span><i className="green" />{tr('по графику')}</span>
              <span><i className="yellow" />{tr('копятся опоздания')}</span>
              <span><i className="orange" />{tr('большие опоздания')}</span>
              <span><i className="single" />{tr('движение по одному пути')}</span>
              <span><i className="closed" />{tr('путь закрыт')}</span>
              <span><i className="blocked" />{tr('место перекрытия')}</span>
            </div>
          )}
        </div>
        <button title={tr(is3d ? 'Вид сверху (2D)' : 'Наклонить карту (3D)')} className={`mode3d ${is3d ? 'on' : ''}`} onClick={() => setIs3d((v) => !v)}>
          {is3d ? '2D' : '3D'}
        </button>
        <button title={tr('Приблизить')} onClick={() => zoomAt(w / 2, h / 2, 1.3)}>
          <Plus size={18} />
        </button>
        <button title={tr('Отдалить')} onClick={() => zoomAt(w / 2, h / 2, 1 / 1.3)}>
          <Minus size={18} />
        </button>
        <button title={tr('Показать весь участок')} onClick={() => setView({ k: 1, x: 0, y: 0 })}>
          <Locate size={18} />
        </button>
      </div>
    </div>
  );
}

/** Слой поездов: перерисовывается каждый кадр по плавному модельному времени. */
function TrainsLayer({ part, s, plan, snapshot, geo, selected, ahead, armed, targetId, onSelect }) {
  const smooth = useSmoothNow();
  const t = snapshot ? snapshot.t : smooth;
  const positions = snapshot ? snapshot.positions : currentPositions(SECTION, s, plan, t);
  const trainById = new Map(s.trains.map((x) => [x.id, x]));
  const broken = new Set(s.disruptions.filter((d) => d.kind === 'breakdown' && isActive(d, t)).map((d) => d.trainId));
  const placed = positions.filter((p) => trainById.has(p.trainId)).map((p) => ({ p, ...trainPlace(p, geo, s.disruptions, t) }));
  const sel = placed.find((x) => x.p.trainId === selected);
  const routePts = sel && ahead.length ? routeAlongTracks(sel, plan.trains[selected], trainById.get(selected), geo, s.disruptions, t) : null;
  const dest = ahead.length ? geo.S[ahead.at(-1).station] : null;

  return (
    <g>
      {part === 'body' && routePts && (
        <g>
          <polyline points={routePts} className="route-case" />
          <polyline points={routePts} className="route-line" />
          <g transform={`translate(${dest.x} ${dest.y})`} className="badge">
            <circle r={13} />
            <text y={5} textAnchor="middle">
              {tr('Б')}
            </text>
          </g>
        </g>
      )}
      {placed.map(({ p, x, y, angle, atStation, wrong, s: k, seg, track }) => {
        const train = trainById.get(p.trainId);
        if (atStation) {
          if (part !== 'top') return null;
          const o = geo.ovals[p.station];
          const side = p.dir === 1 ? 1 : -1;
          const cx = x + side * (o.rx + 30);
          return (
            <g
              key={p.trainId}
              className={`train standing ${selected === p.trainId ? 'selected' : ''} ${targetId === p.trainId ? 'target' : ''}`}
              transform={`translate(${cx} ${y})`}
              onPointerDown={(e) => e.stopPropagation()}
              onClick={(e) => {
                if (armed) return;
                e.stopPropagation();
                onSelect(p.trainId);
              }}
            >
              <title>{tr('{n} стоит на станции', { n: train.number })}</title>
              <line x1={-side * 24} x2={-side * (o.rx + 30 - o.rx)} y1={0} y2={0} stroke={CATEGORIES[train.category].color} className="standing-link" />
              <rect x={-24} y={-10} width={48} height={20} rx={10} fill={CATEGORIES[train.category].color} className="standing-chip" />
              <text y={4} textAnchor="middle" className="train-no">
                {train.number}
              </text>
            </g>
          );
        }
        const deg = (angle * 180) / Math.PI + (p.dir === 1 ? 0 : 180);
        const color = CATEGORIES[train.category].color;
        const cls = `train ${selected === p.trainId ? 'selected' : ''} ${broken.has(p.trainId) ? 'broken' : ''} ${targetId === p.trainId ? 'target' : ''}`;
        const labelDy = p.dir === 1 ? -20 : 22;
        return (
          <g
            key={p.trainId}
            className={cls}
            transform={`translate(${x} ${y})`}
            onPointerDown={(e) => e.stopPropagation()}
            onClick={(e) => {
              if (armed) return;
              e.stopPropagation();
              onSelect(p.trainId);
            }}
          >
            <title>{`${train.number} · ${tr(CATEGORIES[train.category].name)}${wrong ? ` · ${tr('идёт по соседнему пути')}` : ''}`}</title>
            {part === 'body' ? (
              <Consist
                deg={deg}
                color={color}
                wagons={WAGONS[train.category] ?? 4}
                scale={1.2 * k * (1 + 0.55 * (geo.pr.tilt / TILT_3D))}
                lift={geo.pr.tilt / TILT_3D}
                cars={seg !== undefined ? tailPoses({ x, y, seg, track }, p.dir, geo, 1.2 * k * (1 + 0.55 * (geo.pr.tilt / TILT_3D)), WAGONS[train.category] ?? 4) : null}
              />
            ) : (
              <g transform={`translate(0 ${labelDy})`}>
                <rect x={-23} y={-10} width={46} height={20} rx={10} className="train-label" />
                <text y={4} textAnchor="middle" className="train-no">
                  {train.number}
                </text>
              </g>
            )}
          </g>
        );
      })}
    </g>
  );
}

/** Состав: локомотив с обтекаемой кабиной впереди и вагоны за ним, развёрнутый по направлению движения. */
function Consist({ deg, color, wagons, scale, lift = 0, cars = null }) {
  const carW = CAR_W;
  const gap = CAR_GAP;
  // Вагоны: по рельсам (cars — смещение и поворот каждого) или прямой линией за локомотивом.
  const shapes = (roof) => (
    <g>
      {cars
        ? cars.map((c, i) => (
            <g key={i} transform={`translate(${c.dx} ${c.dy}) rotate(${c.deg}) scale(${scale})`}>
              <rect x={-carW / 2} y={-4} width={carW} height={8} rx={2} fill={color} className={roof ? 'car' : 'car-side'} />
            </g>
          ))
        : Array.from({ length: wagons }, (_, i) => (
            <g key={i} transform={`rotate(${deg}) scale(${scale})`}>
              <rect x={-10 - (i + 1) * (carW + gap)} y={-4} width={carW} height={8} rx={2} fill={color} className={roof ? 'car' : 'car-side'} />
            </g>
          ))}
      <g transform={`rotate(${deg}) scale(${scale})`}>
        <path d="M-10 -5 H6 Q12 -5 12 0 Q12 5 6 5 H-10 Z" fill={color} className={roof ? 'loco' : 'car-side'} />
        {roof && <rect x={4} y={-3} width={3.5} height={6} rx={1.5} className="cab" />}
      </g>
    </g>
  );
  if (lift < 0.05) return <g className="consist">{shapes(true)}</g>;
  // 3D: стенки вагонов — несколько затемнённых слоёв, крыша поднята над рельсами.
  const H = 7 * scale * lift;
  return (
    <g className="consist c3d">
      <g className="car-base">{shapes(false)}</g>
      {[1, 2, 3].map((i) => (
        <g key={i} transform={`translate(0 ${(-H * i) / 4})`} style={{ filter: `brightness(${0.6 + i * 0.07})` }}>
          {shapes(false)}
        </g>
      ))}
      <g transform={`translate(0 ${-H})`}>{shapes(true)}</g>
    </g>
  );
}

/** Дуга по овалу от верхней точки по часовой стрелке на долю frac (0…1) — кольцо заполненности станции. */
function ringArc(rx, ry, frac) {
  if (frac >= 0.999) return `M 0 ${-ry} A ${rx} ${ry} 0 1 1 0 ${ry} A ${rx} ${ry} 0 1 1 0 ${-ry}`;
  const a = -Math.PI / 2 + frac * 2 * Math.PI;
  const x = rx * Math.cos(a);
  const y = ry * Math.sin(a);
  return `M 0 ${-ry} A ${rx} ${ry} 0 ${frac > 0.5 ? 1 : 0} 1 ${x.toFixed(2)} ${y.toFixed(2)}`;
}

/**
 * Маршрут выбранного поезда по реальным путям: от текущего положения по своему пути (нечётные — путь 1,
 * чётные — путь 2), а там, где свой путь в момент проследования закрыт, — по соседнему, как пойдёт поезд.
 */
function routeAlongTracks(sel, tp, train, geo, disruptions, t) {
  const pts = [{ x: sel.x, y: sel.y }];
  const own = ownTrack(train.dir);
  let fNow = sel.atStation ? null : sel.f;
  for (let j = 0; j + 1 < tp.stops.length; j++) {
    const a = tp.stops[j];
    const b = tp.stops[j + 1];
    if (b.arr <= t) continue;
    const seg = Math.min(a.station, b.station);
    const g = SECTION.segments[seg];
    const cl = closuresAt(disruptions, seg, Math.max(t, a.dep));
    const forward = a.station < b.station;
    const sign = forward ? 1 : -1;
    // Части пути между съездами — в порядке движения поезда.
    const bounds = forward ? [0, ...g.cuts, 1] : [1, ...[...g.cuts].reverse(), 0];
    if (a.dep > t) fNow = null;
    let prev = null;
    for (let k = 0; k + 1 < bounds.length; k++) {
      const fa = bounds[k];
      const fb = bounds[k + 1];
      if (fNow !== null && (forward ? fb <= fNow : fb >= fNow)) continue;
      const tr = g.tracks >= 2 ? trackAt(cl, own, (fa + fb) / 2) : undefined;
      const inside = fNow !== null && (forward ? fa < fNow : fa > fNow);
      // Переход на соседний путь — наискосок по съезду (X).
      const start = (inside ? fNow : fa) + (prev !== null && tr !== prev ? sign * 0.03 : 0);
      if (!inside) pts.push(geo.onTrack(seg, tr, start));
      pts.push(geo.onTrack(seg, tr, fb));
      prev = tr;
    }
    fNow = null;
  }
  return pts.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ');
}

// ───────────── автоблокировка ─────────────
/**
 * Блок-участки перегона в долях от станции from. С сервера — из инфраструктуры (§9.1),
 * в демо — равные участки ~2.5 км, как требует ТЗ.
 */
function blocksOf(g) {
  if (g.blocks?.length) {
    return g.blocks.map((b) => {
      const a = b.from_m / g.length;
      const c = b.to_m / g.length;
      return { id: b.id, f0: g.reversed ? 1 - c : a, f1: g.reversed ? 1 - a : c };
    }).sort((x, y) => x.f0 - y.f0);
  }
  const n = Math.max(3, Math.round(g.length / 2500));
  return Array.from({ length: n }, (_, i) => ({ id: `${g.edgeId}-B${i + 1}`, f0: i / n, f1: (i + 1) / n }));
}

const ASPECT = { green: '#1fa34a', yellow: '#e8a10c', red: '#e5322d', invitation: '#ffffff', dark: 'none' };

function AutoBlockLayer({ s, plan, t, geo, flat }) {
  const positions = currentPositions(SECTION, s, plan, t);
  const z = Math.min(1.6, flat.z);
  return (
    <g className="ab-layer">
      {SECTION.segments.map((g, i) => {
        const blocks = blocksOf(g);
        const single = g.tracks < 2;
        const from = SECTION.stations[g.from].km;
        const to = SECTION.stations[g.to].km;
        // Занятость: с сервера — по данным поля, в демо — по положению поездов и перекрытиям.
        const occ = new Set();
        if (s.live?.blocks) {
          for (const b of blocks) {
            const lb = s.live.blocks[b.id];
            if (lb?.occupied_by || lb?.obstacle) occ.add(`${b.id}:s`);
          }
        } else {
          for (const p of positions) {
            if (p.station !== undefined || p.km <= from || p.km >= to) continue;
            const fr = (p.km - from) / (to - from || 1);
            const tail = fr - (p.dir === 1 ? 1 : -1) * (0.6 / (g.lengthKm || 1));
            const tr = single ? 's' : p.dir === 1 ? 0 : 1;
            for (const b of blocks) if (Math.max(fr, tail) > b.f0 && Math.min(fr, tail) < b.f1) occ.add(`${b.id}:${tr}`);
          }
          for (const d of s.disruptions) {
            if (d.segment !== i || (d.kind !== 'livestock' && d.kind !== 'closure') || d.resolvedAt !== undefined || t < d.start || t >= d.start + d.durExpected) continue;
            const a = edgeFraction(g, d.posStart ?? 0);
            const c = edgeFraction(g, d.posEnd ?? g.length);
            const trs = single ? ['s'] : d.track !== undefined ? [d.track] : [0, 1];
            for (const b of blocks) if (Math.max(a, c) > b.f0 && Math.min(a, c) < b.f1) for (const tr of trs) occ.add(`${b.id}:${tr}`);
          }
        }
        const dirInfo = s.live?.directions?.[g.specId];
        const dirLive = dirInfo?.direction ?? null;
        // С сервером диспетчер меняет направление кликом по стрелке (POST /api/dc/direction, только свободный перегон).
        const canTurn = s.liveMode && can(s.user, 'section') && !dirInfo?.changing;
        const { u, n } = flat.segs[i];
        const at = (fr, track, off) => {
          const c = flat.onTrack(i, track, fr);
          return geo.P({ x: c.x + n.x * off, y: c.y + n.y * off });
        };
        // Нечётные светофоры над путём, чётные под ним; на двухпутке — каждый у своего пути.
        const sigOff = (dir) => (dir === 'odd' ? -1 : 1) * (single ? 7 * z : flat.gap + 6 * z);
        const trackOf = (dir) => (single ? undefined : dir === 'odd' ? 0 : 1);
        const occKey = (b, dir) => `${b.id}:${single ? 's' : dir === 'odd' ? 0 : 1}`;
        const aspect = (k, dir) => {
          const id = `${g.specId}-P${k}${dir === 'odd' ? 'N' : 'C'}`;
          if (s.live?.signals?.[id]) return s.live.signals[id];
          if (single && dirLive && dirLive !== dir) return 'dark';
          // Нечётный светофор на границе k охраняет блок k (по ходу), чётный — блок k − 1.
          const ord = dir === 'odd' ? blocks.slice(k) : blocks.slice(0, k).reverse();
          if (occ.has(occKey(ord[0], dir))) return 'red';
          if (ord[1] && occ.has(occKey(ord[1], dir))) return 'yellow';
          return 'green';
        };
        return (
          <g key={g.edgeId}>
            {blocks.map((b) =>
              ['s', 0, 1]
                .filter((tr) => occ.has(`${b.id}:${tr}`))
                .map((tr) => {
                  const track = tr === 's' ? undefined : tr;
                  const p0 = geo.onTrack(i, track, b.f0 + 0.01);
                  const p1 = geo.onTrack(i, track, b.f1 - 0.01);
                  return <line key={`${b.id}${tr}`} x1={p0.x} y1={p0.y} x2={p1.x} y2={p1.y} className="ab-occ" />;
                }),
            )}
            {blocks.slice(1).map((b, j) => {
              const k = j + 1;
              const half = single ? 4 * z : flat.gap + 3 * z;
              const a = at(b.f0, undefined, -half);
              const c = at(b.f0, undefined, half);
              return (
                <g key={b.id}>
                  <line x1={a.x} y1={a.y} x2={c.x} y2={c.y} className="ab-tick" />
                  {['odd', 'even'].map((dir) => {
                    const asp = aspect(k, dir);
                    const p = at(b.f0, trackOf(dir), sigOff(dir));
                    return (
                      <circle key={dir} cx={p.x} cy={p.y} r={2.4 * z} fill={ASPECT[asp] ?? 'none'} className={`ab-sig ${asp}`}>
                        <title>{`${tr(dir === 'odd' ? 'Проходной светофор, нечётное' : 'Проходной светофор, чётное')}: ${tr({ green: 'зелёный', yellow: 'жёлтый', red: 'красный', invitation: 'пригласительный', dark: 'не для установленного направления' }[asp] ?? asp)}`}</title>
                      </circle>
                    );
                  })}
                </g>
              );
            })}
            {single && dirLive && (() => {
              const c = at(0.5, undefined, -16 * z);
              const sgn = (dirLive === 'odd') !== g.reversed ? 1 : -1;
              const L = 9 * z;
              const tip = { x: c.x + u.x * L * sgn, y: c.y + u.y * L * sgn };
              const tail = { x: c.x - u.x * L * sgn, y: c.y - u.y * L * sgn };
              const w1 = { x: tip.x - u.x * 5 * z * sgn + n.x * 3.5 * z, y: tip.y - u.y * 5 * z * sgn + n.y * 3.5 * z };
              const w2 = { x: tip.x - u.x * 5 * z * sgn - n.x * 3.5 * z, y: tip.y - u.y * 5 * z * sgn - n.y * 3.5 * z };
              return (
                <g
                  className={`ab-dir ${canTurn ? 'turnable' : ''} ${dirInfo?.changing ? 'changing' : ''} ${dirInfo?.manual ? 'manual' : ''}`}
                  onPointerDown={canTurn ? (e) => e.stopPropagation() : undefined}
                  onClick={canTurn ? (e) => { e.stopPropagation(); engine.setDirection(i, dirLive === 'odd' ? 'even' : 'odd'); } : undefined}
                >
                  <title>
                    {tr('Установленное направление автоблокировки')}
                    {dirInfo?.changing ? ` · ${tr('меняется')}` : dirInfo?.manual ? ` · ${tr('задано диспетчером')}` : ''}
                    {canTurn ? ` · ${tr('клик — сменить направление')}` : ''}
                  </title>
                  <circle cx={c.x} cy={c.y} r={L + 4} className="ab-dir-hit" />
                  <line x1={tail.x} y1={tail.y} x2={tip.x} y2={tip.y} />
                  <path d={`M${tip.x} ${tip.y} L${w1.x} ${w1.y} L${w2.x} ${w2.y} Z`} />
                </g>
              );
            })()}
          </g>
        );
      })}
    </g>
  );
}

/** Карточка раздельного пункта: пути, полезная длина, кто на каком пути (§27.2). */
function StationCard({ i, s, plan, t, at, w, onClose }) {
  const st = SECTION.stations[i];
  const positions = currentPositions(SECTION, s, plan, t).filter((p) => p.station === i);
  const tracks = st.trackInfo ?? Array.from({ length: st.tracks }, (_, k) => ({ id: String(k + 1), main: k === 0 }));
  const left = Math.min(Math.max(8, at.x + 24), w - 300);
  const ref = useDismiss(true, onClose, '.station-oval');
  return (
    <div ref={ref} className="station-card" style={{ left, top: Math.max(8, at.y - 40) }} onPointerDown={(e) => e.stopPropagation()}>
      <div className="sc-head">
        <b>{tr(st.name)}</b>
        <span className="muted small">{tr('км {km}', { km: st.km })}</span>
        <button className="icon-btn" onClick={onClose} title={tr('Закрыть')}>
          <X size={16} />
        </button>
      </div>
      <table>
        <thead>
          <tr>
            <th>{tr('Путь')}</th>
            <th>{tr('Полезная длина')}</th>
            <th>{tr('Занят')}</th>
          </tr>
        </thead>
        <tbody>
          {tracks.map((tk, k) => {
            const p = positions.find((x) => (x.track ?? 0) === k);
            return (
              <tr key={tk.id}>
                <td>
                  {tk.id}
                  {tk.main ? ` (${tr('главный')})` : ''}
                  {tk.platform ? `, ${tr('платф.')}` : ''}
                </td>
                <td>{tk.length_m ? `${tk.length_m} ${tr('м')}` : '—'}</td>
                <td className={p ? 'crit-text' : 'muted'}>{p ? p.trainId : tr('свободен')}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
