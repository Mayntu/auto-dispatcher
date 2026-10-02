/**
 * ГИД — график исполненного движения (§27.3 ТЗ): время по горизонтали (сейчас − 60 … + 180 мин),
 * раздельные пункты по вертикали в масштабе км. Сплошная нитка — факт, штриховая — план,
 * точечная — предлагаемый вариант или what-if с подсветкой изменённых скрещений, бледная — нормативный график.
 * Минуты прибытия и отправления — у осей пунктов, когда помещаются. Перетаскивание — сдвиг, клик — поезд.
 */
import { ChevronDown, ChevronUp, RotateCcw } from 'lucide-react';
import { useMemo, useRef, useState } from 'react';
import { computeMeets } from '../core/conflicts';
import { can } from '../engine/roles';
import { useManualDrag } from './ManualDrag';
import { TrainFilter } from './TrainFilter';
import { destDelayMin } from '../core/qualityIndex';
import { durationOf, isBlocking } from '../core/scheduler';
import { CATEGORIES, SECTION, SECTION_KM } from '../core/section';
import { fmtHM } from '../core/time';
import { runningPlan } from '../engine/engine';
import { EVENT_BY_KIND } from './EventPalette';
import { useSize } from './ui';
import { t } from '../i18n';

const SPANS = [
  { min: 120, h: 2 },
  { min: 240, h: 4 },
  { min: 480, h: 8 },
];

export function TrainGraphView({ s, selected, onSelect, preview, open = true, onToggle }) {
  const [ref, size] = useSize();
  const [span, setSpan] = useState(240);
  const [shift, setShift] = useState(0);
  // Факт, план, вариант и нормативный график рисуются всегда; выбирать можно сами поезда.
  const layers = { fact: true, plan: true, base: true, ghost: true };
  const showBase = layers.base;
  const [shown, setShown] = useState(null);
  const [hover, setHover] = useState(null);
  const drag = useRef(null);
  const basePlan = runningPlan(s);
  const svgRef = useRef(null);
  const hoverTimer = useRef(null);

  const w = Math.max(600, size.w);
  const h = Math.max(250, size.h);
  const L = 150;
  const R = 24;
  const T = 44;
  const B = 24;
  // Окно ГИД: час назад и три часа вперёд (при масштабе 4 ч).
  const t0 = s.now - span * 60 * 0.25 + shift;
  const t1 = t0 + span * 60;
  const x = (t) => L + ((t - t0) / (t1 - t0)) * (w - L - R);
  const y = (km) => T + (km / SECTION_KM) * (h - T - B);
  // Ручное изменение времени: ручки у точек плановой нитки, указания-замки, прогноз и варианты.
  const md = useManualDrag({
    s,
    plan: basePlan,
    geo: { x, y, invX: (px) => t0 + ((px - L) / (w - L - R)) * (t1 - t0), L, R, T, B, w, h },
    svgRef,
    canEdit: can(s.user, 'section') && open,
    focus: hover ?? selected,
    onHandleEnter: (id) => {
      clearTimeout(hoverTimer.current);
      setHover(id);
    },
  });
  const plan = md.viewPlan;
  const conflicts = s.forecast ? s.forecastConflicts : s.conflicts;
  const trainById = new Map(s.trains.map((t) => [t.id, t]));

  const pts = (p, id) =>
    (p.trains[id]?.stops ?? []).flatMap((st, j, all) => [
      // стоянка на перегоне (перед закрытым местом или из-за поломки) — горизонтальный участок линии
      ...(st.stuck && j > 0
        ? (() => {
            const a = SECTION.stations[all[j - 1].station].km;
            const km = a + (SECTION.stations[st.station].km - a) * st.stuck.u;
            return [
              [x(st.stuck.from), y(km)],
              [x(st.stuck.to), y(km)],
            ];
          })()
        : []),
      [x(st.arr), y(SECTION.stations[st.station].km)],
      [x(st.dep), y(SECTION.stations[st.station].km)],
    ]);
  const poly = (arr) => arr.map(([a, b]) => `${a.toFixed(1)},${b.toFixed(1)}`).join(' ');
  const xNow = x(s.now);
  /** Нитка до «сейчас» и после — с точкой разреза на линии времени. */
  const splitAtNow = (arr) => {
    const left = [];
    const right = [];
    for (let i = 0; i < arr.length; i++) {
      const p = arr[i];
      if (p[0] <= xNow) left.push(p);
      else {
        const q = arr[i - 1];
        if (q && q[0] < xNow) {
          const cut = [xNow, q[1] + ((p[1] - q[1]) * (xNow - q[0])) / (p[0] - q[0] || 1)];
          left.push(cut);
          right.push(cut);
        }
        right.push(p);
      }
    }
    return [left, right];
  };
  /** Факт: с сервером — записанные положения поездов, в демо — исполненная часть плана. */
  const factPts = (id) => {
    const past = splitAtNow(pts(plan, id))[0];
    const rec = s.factTrack?.[id];
    if (!rec?.length) return past;
    // До подключения к серверу записей нет — это время берём из исполненной части плана.
    const x0 = x(rec[0][0]);
    return [...past.filter((p) => p[0] < x0), ...rec.map(([tt, km]) => [x(tt), y(km)])];
  };
  const pxPerMin = (w - L - R) / span;
  // Скрещения, которые появятся или переедут на другой пункт в предлагаемом варианте.
  const changedMeets = useMemo(() => {
    if (!preview) return [];
    const cur = computeMeets(plan, s.trains);
    const next = computeMeets(preview, s.trains);
    const out = [];
    for (const [key, station] of next) {
      if (cur.get(key) === station) continue;
      const [a, b] = key.split('|');
      const sa = preview.trains[a]?.stops.find((st) => st.station === station);
      const sb = preview.trains[b]?.stops.find((st) => st.station === station);
      if (sa && sb) out.push({ key, a, b, station, t: Math.max(sa.arr, sb.arr) });
    }
    return out;
  }, [preview, plan, s.trains]);

  /** Подпись номера — на середине первого видимого перегона, вдоль линии. */
  const labelOf = (id) => {
    const p = pts(plan, id);
    for (let i = 1; i + 1 < p.length; i += 2) {
      const [ax, ay] = p[i];
      const [bx, by] = p[i + 1];
      if (bx < L + 20 || ax > w - R - 20 || Math.abs(by - ay) < 20) continue;
      const mx = Math.min(Math.max((ax + bx) / 2, L + 30), w - R - 30);
      const f = (mx - ax) / (bx - ax || 1);
      return { x: mx, y: ay + (by - ay) * f, deg: (Math.atan2(by - ay, bx - ax) * 180) / Math.PI };
    }
    return null;
  };

  const ticks = [];
  const minor = span > 240 ? 1800 : 600;
  for (let t = Math.ceil(t0 / minor) * minor; t <= t1; t += minor) ticks.push(t);

  const onPointerDown = (e) => {
    drag.current = { sx: e.clientX, shift, moved: false };
  };
  const onPointerMove = (e) => {
    const d = drag.current;
    if (!d) return;
    if (!d.moved && Math.abs(e.clientX - d.sx) > 4) {
      d.moved = true;
      e.currentTarget.setPointerCapture(e.pointerId);
    }
    if (d.moved) setShift(d.shift - ((e.clientX - d.sx) / (w - L - R)) * (t1 - t0));
  };
  const onPointerUp = () => (drag.current = null);

  const focus = md.drag?.trainId ?? hover ?? selected;
  // Остальные поезда бледнеют только на время наведения или перетаскивания: выбранный поезд и так выделен
  // жирной линией, а постоянное затенение после нажатия выглядело так, будто линии пропали.
  const dimFor = md.drag?.trainId ?? hover;

  return (
    <div className={`graph-view ${open ? 'open' : ''}`}>
      <div className="gv-head">
        <button className="gv-toggle" onClick={onToggle} title={t(open ? 'Свернуть' : 'Развернуть')}>
          {open ? <ChevronDown size={18} /> : <ChevronUp size={18} />}
          {t('ГИД (поездограмма)')}
        </button>
        {open && <div className="seg-ctl">
          {SPANS.map((o) => (
            <button key={o.min} className={span === o.min ? 'on' : ''} onClick={() => setSpan(o.min)}>
              {t('{n} ч', { n: o.h })}
            </button>
          ))}
        </div>}
        {open && <TrainFilter trains={s.trains} plan={plan} shown={shown} onChange={setShown} />}
        {shift !== 0 && (
          <button className="gv-now" onClick={() => setShift(0)}>
            <RotateCcw size={14} /> {t('К текущему времени')}
          </button>
        )}
        <span className="gv-legend">
          {Object.values(CATEGORIES).map((c) => (
            <span key={c.id}>
              <i style={{ background: c.color }} />
              {t(c.name)}
            </span>
          ))}
        </span>
      </div>
      {open && (
      <div className="gv-body" ref={ref}>
        <svg
          ref={svgRef}
          width={w}
          height={h}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerLeave={() => {
            drag.current = null;
            setHover(null);
          }}
        >
          <defs>
            <clipPath id="gv-plot">
              <rect x={L} y={T - 6} width={w - L - R} height={h - T - B + 12} />
            </clipPath>
            <pattern id="gv-hatch" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
              <rect width="3" height="8" fill="#e5322d" opacity=".35" />
            </pattern>
            <pattern id="gv-hatch-one" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
              <rect width="3" height="8" fill="#e8a10c" opacity=".35" />
            </pattern>
            <linearGradient id="gv-past" x1="0" x2="1">
              <stop offset="0" stopColor="#eef0f3" />
              <stop offset="1" stopColor="#f6f7f9" />
            </linearGradient>
          </defs>

          {/* перегоны — полосы, станции — линии */}
          {SECTION.segments.map((g, i) => (
            <rect
              key={g.edgeId}
              x={L}
              y={y(SECTION.stations[g.from].km)}
              width={w - L - R}
              height={y(SECTION.stations[g.to].km) - y(SECTION.stations[g.from].km)}
              className={i % 2 ? 'gv-band odd' : 'gv-band'}
            />
          ))}
          <rect x={L} y={T} width={Math.max(0, Math.min(w - R, x(s.now)) - L)} height={h - T - B} fill="url(#gv-past)" opacity=".8" />

          {ticks.map((t) => {
            const hour = t % 3600 === 0;
            const half = t % 1800 === 0;
            return (
              <g key={t}>
                <line x1={x(t)} x2={x(t)} y1={T} y2={h - B} className={hour ? 'gv-grid hour' : half ? 'gv-grid half' : 'gv-grid'} />
                {(half || span <= 120) && (
                  <text x={x(t)} y={T - 14} textAnchor="middle" className={hour ? 'gv-time hour' : 'gv-time'}>
                    {fmtHM(t)}
                  </text>
                )}
              </g>
            );
          })}

          {SECTION.stations.map((st) => (
            <g key={st.id}>
              <line x1={L} x2={w - R} y1={y(st.km)} y2={y(st.km)} className="gv-station-line" />
              <text x={L - 14} y={y(st.km) - 1} textAnchor="end" className="gv-st-name">
                {t(st.short)}
              </text>
              <text x={L - 14} y={y(st.km) + 13} textAnchor="end" className="gv-st-km">
                {t('{km} км · {n} пути', { km: st.km, n: st.tracks })}
              </text>
            </g>
          ))}

          <g clipPath="url(#gv-plot)">
            {s.disruptions
              .filter((d) => d.segment !== undefined)
              .map((d) => {
                const g = SECTION.segments[d.segment];
                const y1 = y(SECTION.stations[g.from].km);
                const y2 = y(SECTION.stations[g.to].km);
                const one = isBlocking(d) && d.track !== undefined && g.tracks >= 2;
                const x1 = x(d.start);
                const x2 = x(d.start + durationOf(d, 'expected'));
                const xm = x(d.start + d.durMax);
                const type = EVENT_BY_KIND[d.kind];
                return (
                  <g key={d.id} className="gv-block">
                    <rect x={x1} y={y1} width={Math.max(2, xm - x1)} height={y2 - y1} className="gv-block-range" />
                    <rect
                      x={x1}
                      y={y1}
                      width={Math.max(2, x2 - x1)}
                      height={y2 - y1}
                      fill={isBlocking(d) && !one ? 'url(#gv-hatch)' : 'url(#gv-hatch-one)'}
                      className={isBlocking(d) && !one ? 'gv-block-core' : 'gv-block-core one'}
                    />
                    <text x={x1 + 6} y={y1 + 16} className="gv-block-text">
                      {t(type.label)}
                      {one ? ` · ${t('путь {n}', { n: d.track + 1 })}` : ''}
                    </text>
                  </g>
                );
              })}

            {showBase &&
              s.trains.filter((tr) => !shown || shown.has(tr.id) || tr.id === selected).map((t) =>
                s.baseline.trains[t.id] && !t.cancelled ? (
                  <polyline key={t.id} points={poly(pts(s.baseline, t.id))} stroke={CATEGORIES[t.category].color} className="gv-base" />
                ) : null,
              )}

            {s.trains.filter((tr) => !shown || shown.has(tr.id) || tr.id === selected).map((t) => {
              if (t.cancelled || !plan.trains[t.id]) return null;
              const all = pts(plan, t.id);
              const p = poly(all);
              const fact = factPts(t.id);
              let future = splitAtNow(all)[1];
              // План сервера начинается со следующей станции — тянем пунктир от того места, где поезд сейчас.
              const here = fact[fact.length - 1];
              if (layers.fact && here && future.length && here[0] >= xNow - 2 && future[0][0] > here[0] + 0.5) future = [here, ...future];
              const dim = dimFor && dimFor !== t.id;
              return (
                <g
                  key={t.id}
                  data-train={t.id}
                  className={`gv-train ${focus === t.id ? 'focus' : ''} ${dim ? 'dim' : ''}`}
                  onPointerEnter={() => {
                    clearTimeout(hoverTimer.current);
                    setHover(t.id);
                  }}
                  onPointerLeave={() => {
                    // Небольшая задержка — чтобы успеть навести на ручку точки.
                    clearTimeout(hoverTimer.current);
                    hoverTimer.current = setTimeout(() => setHover((v) => (v === t.id ? null : v)), 400);
                  }}
                  onClick={() => onSelect(t.id)}
                >
                  <polyline points={p} className="gv-hit" />
                  {layers.fact && fact.length > 1 && (
                    <>
                      <polyline points={poly(fact)} className="gv-halo" />
                      <polyline points={poly(fact)} stroke={CATEGORIES[t.category].color} className="gv-line" />
                    </>
                  )}
                  {layers.plan && future.length > 1 && <polyline points={poly(future)} stroke={CATEGORIES[t.category].color} className="gv-line plan" />}
                  {pxPerMin >= 4 &&
                    plan.trains[t.id].stops.flatMap((st, j, all2) => {
                      const yy = y(SECTION.stations[st.station].km);
                      const out = [];
                      const through = st.dep - st.arr <= 30;
                      if (j > 0) out.push({ k: 'a', tt: st.arr, anchor: through ? 'middle' : 'end', dx: through ? 0 : -3 });
                      if (j < all2.length - 1 && !through) out.push({ k: 'd', tt: st.dep, anchor: 'start', dx: 3 });
                      return out
                        .filter((o) => x(o.tt) > L && x(o.tt) < w - R)
                        .map((o) => (
                          <text key={`${st.station}${o.k}`} x={x(o.tt) + o.dx} y={yy + 12} textAnchor={o.anchor} className="gv-mm">
                            {fmtHM(o.tt).slice(3)}
                          </text>
                        ));
                    })}
                  {plan.trains[t.id].stops
                    .filter((st, j) => j > 0 && st.dep - st.arr > 30)
                    .map((st) => (
                      <circle key={st.station} cx={x(st.arr)} cy={y(SECTION.stations[st.station].km)} r={3.5} fill={CATEGORIES[t.category].color} className="gv-stop" />
                    ))}
                </g>
              );
            })}

            {/* Поезда без плана сервера: дальше горизонта планирования плана для них ещё нет — ведём по расписанию,
                тем же пунктиром плана, но бледнее. Когда поезд войдёт в горизонт, линию заменит план. */}
            {s.trains.filter((tr) => !shown || shown.has(tr.id) || tr.id === selected).map((t) => {
              if (t.cancelled || plan.trains[t.id] || !s.baseline.trains[t.id]) return null;
              // Доехавший поезд сервер из плана убирает — его фактическая линия всё равно остаётся на графике.
              const future = splitAtNow(pts(s.baseline, t.id))[1];
              const fact = factPts(t.id);
              if (future.length < 2 && fact.length < 2) return null;
              return (
                <g key={t.id} data-train={t.id} className="gv-train" onClick={() => onSelect(t.id)}>
                  {layers.fact && fact.length > 1 && <polyline points={poly(fact)} stroke={CATEGORIES[t.category].color} className="gv-line" />}
                  {future.length > 1 && <polyline points={poly(future)} className="gv-hit" />}
                  {future.length > 1 && <polyline points={poly(future)} stroke={CATEGORIES[t.category].color} className="gv-line plan beyond" />}
                </g>
              );
            })}

            {plan.horizonEnd !== undefined && x(plan.horizonEnd) < w - R && (
              <g className="gv-horizon">
                <line x1={x(plan.horizonEnd)} x2={x(plan.horizonEnd)} y1={T} y2={h - B} />
                <text x={x(plan.horizonEnd) + 6} y={h - B - 6}>
                  {t('горизонт плана')}
                </text>
              </g>
            )}

            {/* выбранный вариант — только для поездов, у которых он что-то меняет */}
            {preview &&
              layers.ghost &&
              s.trains.filter((tr) => !shown || shown.has(tr.id) || tr.id === selected).map((t) => {
                const a = preview.trains[t.id]?.stops;
                const b = plan.trains[t.id]?.stops;
                if (!a || !b || a.every((st, j) => Math.abs(st.arr - (b[j]?.arr ?? st.arr)) < 30)) return null;
                return <polyline key={t.id} points={poly(pts(preview, t.id))} className="gv-preview" />;
              })}

            {preview &&
              layers.ghost &&
              changedMeets.map((mt) => (
                <g key={mt.key} transform={`translate(${x(mt.t)} ${y(SECTION.stations[mt.station].km)})`} className="gv-meet">
                  <circle r={10} />
                  <text x={13} y={-10}>
                    {t('скрещение {a}/{b}', { a: mt.a, b: mt.b })}
                  </text>
                </g>
              ))}

            {s.trains.filter((tr) => !shown || shown.has(tr.id) || tr.id === selected).map((t) => {
              if (t.cancelled || !plan.trains[t.id]) return null;
              const lb = labelOf(t.id);
              if (!lb) return null;
              return (
                <text
                  key={t.id}
                  transform={`translate(${lb.x} ${lb.y}) rotate(${lb.deg})`}
                  y={-6}
                  textAnchor="middle"
                  className={`gv-num ${dimFor && dimFor !== t.id ? 'dim' : ''}`}
                  fill={CATEGORIES[t.category].color}
                >
                  {t.number}
                </text>
              );
            })}

            {conflicts.map((c) => (
              <g key={c.id} transform={`translate(${x(c.time)} ${y(c.km)})`} className="gv-conflict">
                <title>{t(c.text)}</title>
                <circle r={11} className="pulse" />
                <circle r={8} />
                <text y={4} textAnchor="middle">
                  !
                </text>
              </g>
            ))}
            {md.svgLayer}
          </g>

          {x(s.now) >= L && x(s.now) <= w - R && (
            <g className="gv-nowline">
              <line x1={x(s.now)} x2={x(s.now)} y1={T - 4} y2={h - B} />
              <rect x={x(s.now) - 26} y={T - 30} width={52} height={20} rx={10} />
              <text x={x(s.now)} y={T - 16} textAnchor="middle">
                {fmtHM(s.now)}
              </text>
            </g>
          )}
        </svg>
        {md.overlay}

        {!md.drag && !md.popupOpen && hover && trainById.get(hover) && plan.trains[hover] && (
          <div className="gv-tip">
            <span className="tr-dot" style={{ background: CATEGORIES[trainById.get(hover).category].color }} />
            <b>{trainById.get(hover).number}</b> · {t(CATEGORIES[trainById.get(hover).category].name)}
            <span className="muted">
              {' '}
              · {fmtHM(plan.trains[hover].stops[0].dep)} → {fmtHM(plan.trains[hover].stops.at(-1).arr)}
            </span>
            {destDelayMin(plan, s.baseline, hover) >= 1 && (
              <span className="crit-text"> · {t('+{n} мин', { n: Math.round(destDelayMin(plan, s.baseline, hover)) })}</span>
            )}
            <NextStop s={s} plan={plan} id={hover} />
          </div>
        )}
      </div>
      )}
    </div>
  );
}

/** Ближайший пункт впереди: план и график, отклонение от графика (§27.3). */
function NextStop({ s, plan, id }) {
  const stops = plan.trains[id]?.stops ?? [];
  const st = stops.find((x) => x.arr >= s.now);
  if (!st) return null;
  const norm = s.baseline.trains[id]?.stops.find((b) => b.station === st.station);
  const dev = norm ? Math.round((st.arr - norm.arr) / 60) : 0;
  return (
    <span className="muted">
      {' '}
      · {t('{st}: прибытие {t}', { st: t(SECTION.stations[st.station].short), t: fmtHM(st.arr) })}
      {norm && ` (${t('по графику {t}', { t: fmtHM(norm.arr) })}${dev ? `, ${dev > 0 ? '+' : '−'}${Math.abs(dev)} ${t('мин')}` : ''})`}
    </span>
  );
}
