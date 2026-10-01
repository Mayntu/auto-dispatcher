/**
 * Пейзаж карты: степь с рощами и одиночными деревьями. Деревья стоят в координатах мира карты,
 * поэтому двигаются и масштабируются вместе с путями. Сверху (2D) — кроны с тенью; при наклоне (3D)
 * деревья «встают»: ствол и крона поднимаются над землёй, дальние — меньше и бледнее.
 */
import { memo, useMemo } from 'react';
import { SECTION } from '../core/section';

/** Детерминированный генератор — одинаковые рощи при каждом открытии. */
function rng(seed) {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 4294967296;
  };
}

/** Расстояние от точки до отрезка. */
function distSeg(px, py, a, b) {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const t = Math.max(0, Math.min(1, ((px - a.x) * dx + (py - a.y) * dy) / (dx * dx + dy * dy || 1)));
  return Math.hypot(px - (a.x + dx * t), py - (a.y + dy * t));
}

/** Где растут деревья: рощи вокруг участка, но не на путях, не на станциях и не под их подписями. */
export function makeTrees(L, w, h) {
  const r = rng(20261002);
  const pts = L.pts;
  const segs = SECTION.segments.map((g) => [pts[g.from], pts[g.to]]);
  const free = (x, y) => {
    for (const [a, b] of segs) if (distSeg(x, y, a, b) < 34) return false;
    for (const p of pts) {
      if (Math.hypot(x - p.x, y - p.y) < 62) return false;
      if (Math.abs(x - p.x) < 120 && y > p.y - 105 && y < p.y + 55) return false; // подписи станций
    }
    return true;
  };
  const out = [];
  const x0 = -w * 0.6;
  const x1 = w * 1.6;
  const y0 = -h * 0.8;
  const y1 = h * 1.8;
  const groves = 46;
  for (let gi = 0; gi < groves; gi++) {
    // Рощи гуще у самой линии — вдоль путей обычно лесополосы.
    const near = gi < groves * 0.55;
    let cx;
    let cy;
    if (near) {
      const [a, b] = segs[Math.floor(r() * segs.length)];
      const f = r();
      const side = r() < 0.5 ? -1 : 1;
      const len = Math.hypot(b.x - a.x, b.y - a.y) || 1;
      const off = 55 + r() * 120;
      cx = a.x + (b.x - a.x) * f + (-(b.y - a.y) / len) * off * side;
      cy = a.y + (b.y - a.y) * f + ((b.x - a.x) / len) * off * side;
    } else {
      cx = x0 + r() * (x1 - x0);
      cy = y0 + r() * (y1 - y0);
    }
    const n = 4 + Math.floor(r() * 10);
    const spread = 18 + r() * 34;
    const pine = r() < 0.4;
    for (let i = 0; i < n; i++) {
      const x = cx + (r() - 0.5) * 2 * spread;
      const y = cy + (r() - 0.5) * 1.4 * spread;
      if (!free(x, y)) continue;
      out.push({ x, y, size: 5 + r() * 4.5, pine: pine ? r() < 0.85 : r() < 0.15, tone: Math.floor(r() * 3) });
    }
  }
  // Одиночные деревья в степи.
  for (let i = 0; i < 70; i++) {
    const x = x0 + r() * (x1 - x0);
    const y = y0 + r() * (y1 - y0);
    if (free(x, y)) out.push({ x, y, size: 4.5 + r() * 3.5, pine: r() < 0.3, tone: Math.floor(r() * 3) });
  }
  return out;
}

const CROWN = [
  ['#5f8f3e', '#76a64f'],
  ['#4f7f36', '#68984a'],
  ['#6c9a45', '#86b45c'],
];
const PINE = [
  ['#3d6b3a', '#4f7f48'],
  ['#355f33', '#477542'],
  ['#44743f', '#58884f'],
];

/**
 * Деревья в экранных координатах. lift — 0 сверху, 1 при полном наклоне: от него зависит высота ствола
 * и «стоячая» форма кроны. Сортировка по экранному y — дальние рисуются первыми.
 */
export const TreesLayer = memo(function TreesLayer({ trees, view, project, lift, w, h }) {
  const items = useMemo(() => {
    const z = Math.min(1.8, Math.max(0.9, view.k));
    const list = [];
    for (const tr of trees) {
      const fp = { x: tr.x * view.k + view.x, y: tr.y * view.k + view.y };
      const p = project(fp);
      if (p.x < -60 || p.x > w + 60 || p.y < -80 || p.y > h + 60) continue;
      list.push({ tr, x: p.x, y: p.y, r: tr.size * z * p.s, s: p.s });
    }
    return list.sort((a, b) => a.y - b.y);
  }, [trees, view, project, w, h]);

  return (
    <g className="scenery" pointerEvents="none">
      {items.map(({ tr, x, y, r, s }, i) => {
        const [dark, light] = (tr.pine ? PINE : CROWN)[tr.tone];
        // Дальние деревья в 3D бледнее — воздушная перспектива.
        const fade = lift > 0 ? Math.max(0.55, Math.min(1, 0.4 + s * 0.6)) : 1;
        const trunk = r * 1.5 * lift;
        const shadow = <ellipse cx={x + r * 0.35 * (1 - lift)} cy={y + r * 0.3 * (1 - lift)} rx={r * (1 - 0.15 * lift)} ry={r * (1 - 0.55 * lift)} className="tree-shadow" />;
        if (tr.pine) {
          // Ель: сверху — звёздчатая крона, в 3D — два яруса конуса на стволе.
          const top = y - trunk - r * 2.1 * lift;
          return (
            <g key={i} opacity={fade}>
              {shadow}
              {lift > 0.05 && <rect x={x - r * 0.12} y={y - trunk} width={r * 0.24} height={trunk} className="tree-trunk" />}
              {lift > 0.05 ? (
                <>
                  <path d={`M${x - r} ${y - trunk} L${x} ${top + r * 0.7} L${x + r} ${y - trunk} Z`} fill={dark} />
                  <path d={`M${x - r * 0.75} ${y - trunk - r * 0.8 * lift} L${x} ${top} L${x + r * 0.75} ${y - trunk - r * 0.8 * lift} Z`} fill={light} />
                </>
              ) : (
                <>
                  <circle cx={x} cy={y} r={r} fill={dark} />
                  <circle cx={x - r * 0.25} cy={y - r * 0.25} r={r * 0.55} fill={light} />
                </>
              )}
            </g>
          );
        }
        // Лиственное: круглая крона; в 3D поднимается на стволе и чуть вытягивается вверх.
        const cy = y - trunk - r * 0.85 * lift;
        return (
          <g key={i} opacity={fade}>
            {shadow}
            {lift > 0.05 && <rect x={x - r * 0.13} y={y - trunk - r * 0.2} width={r * 0.26} height={trunk + r * 0.2} className="tree-trunk" />}
            <ellipse cx={x} cy={cy} rx={r} ry={r * (1 + 0.15 * lift)} fill={dark} />
            <ellipse cx={x - r * 0.28} cy={cy - r * 0.3} rx={r * 0.55} ry={r * 0.5} fill={light} />
          </g>
        );
      })}
    </g>
  );
});
