/** Карточка поезда: где он, опаздывает ли, и рекомендованный профиль скорости для машиниста (автоведение). */
import { useEffect, useMemo, useState } from 'react';
import { api } from '../api/backend';
import { locate } from '../core/positions';
import { trainProfile } from '../core/profile';
import { destDelayMin } from '../core/qualityIndex';
import { effectiveVmax } from '../core/scheduler';
import { CATEGORIES, SECTION, SECTION_KM, segmentName, stationName } from '../core/section';
import { fmtHM } from '../core/time';
import { runningPlan } from '../engine/engine';
import { Empty, useSize } from './ui';
import { t } from '../i18n';

const PHASE = {
  accel: { name: 'Разгон', cls: 'ph-accel' },
  cruise: { name: 'Движение', cls: 'ph-cruise' },
  coast: { name: 'Накат', cls: 'ph-coast' },
  brake: { name: 'Торможение', cls: 'ph-brake' },
};

export function TrainPanel({ s, selected, onSelect }) {
  const plan = runningPlan(s);
  const train = s.trains.find((t) => t.id === selected && !t.cancelled) ?? s.trains.find((t) => !t.cancelled);
  const tp = train ? plan.trains[train.id] : undefined;
  const local = useMemo(() => (tp && !s.liveMode ? trainProfile(SECTION, train, tp) : null), [tp, train, s.liveMode]);
  // С сервером профиль считает сервис автоведения (§13): текущий и следующий перегон по утверждённому плану.
  const [server, setServer] = useState(null);
  useEffect(() => {
    if (!s.liveMode || !train) return undefined;
    let alive = true;
    void api.ato(train.id).then((ps) => alive && setServer(profileFromServer(ps, train)));
    return () => {
      alive = false;
    };
  }, [s.liveMode, train?.id, s.plan.version]);
  const prof = s.liveMode ? server : local;
  const [ref, size] = useSize();
  if (!tp || !prof) return <Empty>{t('Поезд не в плане')}</Empty>;

  const loc = locate(plan, train.id, s.now);
  const status =
    loc.state === 'before'
      ? t('отправится в {t} со ст. {st}', { t: fmtHM(tp.stops[0].dep), st: t(SECTION.stations[tp.stops[0].station].short) })
      : loc.state === 'done'
        ? t('проследовал участок')
        : loc.state === 'at'
          ? t('стоит: {st}, отправление {t}', { st: stationName(tp.stops[loc.k].station), t: fmtHM(tp.stops[loc.k].dep) })
          : t('в пути: {seg}', { seg: segmentName(Math.min(tp.stops[loc.k].station, tp.stops[loc.k + 1].station)) });
  const next = tp.stops.find((st, i) => i > 0 && st.arr > s.now);
  const delay = destDelayMin(plan, s.baseline, train.id);

  const W = Math.max(300, size.w);
  const H = 180;
  const L = 30;
  const R = 8;
  const T = 10;
  const B = 22;
  const vmax = Math.max(effectiveVmax(train), 60) + 10;
  const x = (d) => L + (d / SECTION_KM) * (W - L - R);
  const y = (v) => T + (1 - v / vmax) * (H - T - B);
  const dist = (km) => (train.dir === 1 ? km : SECTION_KM - km);
  const segments = [];
  for (const p of prof.points) {
    const last = segments[segments.length - 1];
    if (last && last.phase === p.phase) last.pts.push(p);
    else segments.push({ phase: p.phase, pts: last ? [last.pts[last.pts.length - 1], p] : [p] });
  }
  const path = (pts) => pts.map((p) => `${x(p.dist).toFixed(1)},${y(p.v).toFixed(1)}`).join(' ');

  return (
    <div className="train-card">
      <div className="train-head">
        <span className="train-badge" style={{ background: CATEGORIES[train.category].color }}>
          {train.number}
        </span>
        <div>
          <b>{t(CATEGORIES[train.category].name)}</b>
          <div className="muted small">
            {t(train.dir === 1 ? 'нечётное' : 'чётное')} · {t('до {v} км/ч', { v: effectiveVmax(train) })}
          </div>
        </div>
        <select value={train.id} onChange={(e) => onSelect(e.target.value)} aria-label={t('Другой поезд')}>
          {s.trains
            .filter((t) => !t.cancelled)
            .map((t) => (
              <option key={t.id} value={t.id}>
                {t.number}
              </option>
            ))}
        </select>
      </div>
      {train.note && <div className="note">{t(train.note)}</div>}

      <div className="kpis">
        <div>
          <span className="muted small">{t('Сейчас')}</span>
          <b>{status}</b>
        </div>
        <div>
          <span className="muted small">{t('Дальше')}</span>
          <b>{next ? t('{st} в {t}', { st: t(SECTION.stations[next.station].short), t: fmtHM(next.arr) }) : '—'}</b>
        </div>
        <div>
          <span className="muted small">{t('Опоздание')}</span>
          <b className={delay >= 1 ? 'crit-text' : 'ok-text'}>{delay >= 1 ? t('+{n} мин', { n: Math.round(delay) }) : t('по графику')}</b>
        </div>
      </div>

      <div className="block-title">{t('Рекомендация машинисту')}</div>
      <div className="muted small">
        {t('Экономия энергии')} <b className="ok-text">{prof.savingPct.toFixed(1)}%</b>{' '}
        {t('против езды на максимальной скорости — за счёт наката перед остановками.')}
      </div>
      <div ref={ref}>
        <svg width={W} height={H} role="img" aria-label={t('Профиль скорости')}>
          {[0, 20, 40, 60, 80, 100]
            .filter((v) => v < vmax)
            .map((v) => (
              <g key={v}>
                <line x1={L} x2={W - R} y1={y(v)} y2={y(v)} className="grid" />
                <text x={L - 5} y={y(v) + 4} textAnchor="end" className="axis">
                  {v}
                </text>
              </g>
            ))}
          {SECTION.stations.map((st) => (
            <g key={st.id}>
              <line x1={x(dist(st.km))} x2={x(dist(st.km))} y1={T} y2={H - B} className="grid" />
              <text x={x(dist(st.km))} y={H - 6} textAnchor="middle" className="axis">
                {t(st.short).slice(0, 4)}
              </text>
            </g>
          ))}
          <polyline points={path(prof.refPoints)} className="ref-line" />
          {segments.map((g, i) => (
            <polyline key={i} points={path(g.pts)} className={`prof-line ${PHASE[g.phase].cls}`} />
          ))}
        </svg>
      </div>
      <div className="legend">
        {Object.entries(PHASE).map(([k, p]) => (
          <span key={k}>
            <i className={p.cls} />
            {t(p.name)}
          </span>
        ))}
        <span>
          <i className="dash" />
          {t('макс. скорость')}
        </span>
      </div>
    </div>
  );
}

/** Профили сервера по перегонам (SpeedProfile, §8) → одна линия вдоль участка для графика карточки. */
function profileFromServer(profiles, train) {
  if (!profiles?.length) return null;
  const dist = (km) => (train.dir === 1 ? km : SECTION_KM - km);
  const along = (p, pts) =>
    (pts ?? []).map((x) => {
      const sign = (p.km_to ?? 0) >= (p.km_from ?? 0) ? 1 : -1;
      return { dist: dist((p.km_from ?? 0) + (sign * x.s_m) / 1000), v: x.v_kmh, phase: x.regime };
    });
  const points = profiles.flatMap((p) => along(p, p.points));
  const refPoints = profiles.flatMap((p) => along(p, p.min_points));
  const e = profiles.reduce((a, p) => a + (p.energy_kwh ?? 0), 0);
  const e0 = profiles.reduce((a, p) => a + (p.energy_min_time_kwh ?? 0), 0);
  return { points, refPoints: refPoints.length ? refPoints : points, savingPct: e0 ? (1 - e / e0) * 100 : 0 };
}
