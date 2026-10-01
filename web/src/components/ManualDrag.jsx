/**
 * Ручное изменение времени прибытия и отправления на ГИД (ТЗ «02-manual-drag-frontend»).
 * Диспетчер тянет точку плановой нитки по времени; границы, прогноз для остальных поездов и варианты
 * считает планировщик (в демо — движок в браузере, с сервером — /plan/manual/*). Итог — указание диспетчера:
 * закреплённое время с замком на графике, которое план соблюдает, пока его не снимут.
 */
import { Lock, LockOpen, Sparkles, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { CATEGORIES, SECTION } from '../core/section';
import { fmtHM } from '../core/time';
import { shownIndex } from '../engine/engine';
import { engine } from '../engine/store';
import { t } from '../i18n';

const STEP = 60;
const STEP_SHIFT = 300;
const PREVIEW_EVERY = 150;

const fmtDelta = (sec) => {
  const n = Math.round(sec / 60);
  return n === 0 ? '±0' : `${n > 0 ? '+' : '−'}${Math.abs(n)}`;
};
const stName = (i) => t(SECTION.stations[i]?.short ?? '');

/** Нитка перетаскиваемого поезда, пересчитанная на месте по правилам §4 — до прихода прогноза. */
function localStops(stops, j, kind, time, minDwell) {
  const out = stops.map((x) => ({ ...x }));
  const shiftFrom = (i, d) => {
    for (let k = i; k < out.length; k++) {
      out[k].arr += d;
      out[k].dep += d;
    }
  };
  const st = out[j];
  if (kind === 'dep') {
    const d = time - st.dep;
    st.dep = time;
    shiftFrom(j + 1, d);
  } else {
    st.arr = time;
    if (j === out.length - 1) st.dep = time;
    else if (st.dep < time + minDwell) {
      const d = time + minDwell - st.dep;
      st.dep += d;
      shiftFrom(j + 1, d);
    }
  }
  return out;
}

/**
 * Состояние и слои ручного изменения. geo — геометрия графика: x(t), y(km), invX(px), прямоугольник области.
 * Возвращает план для отрисовки (с локально сдвинутой ниткой), слой внутри SVG и слой подсказок поверх.
 */
export function useManualDrag({ s, plan, geo, svgRef, canEdit, focus, onHandleEnter }) {
  const [drag, setDrag] = useState(null);
  const [popup, setPopup] = useState(null);
  const [ghost, setGhost] = useState(null);
  const [lockOpen, setLockOpen] = useState(null);
  const dragRef = useRef(null);
  dragRef.current = drag;
  const previewTimer = useRef(null);
  const previewSeq = useRef(0);

  const cancel = useCallback(() => {
    clearTimeout(previewTimer.current);
    setDrag(null);
    setGhost(null);
  }, []);

  // Прогноз от планировщика — не чаще раза в 150 мс, устаревшие ответы отбрасываются.
  const requestPreview = useCallback((d, time) => {
    clearTimeout(previewTimer.current);
    previewTimer.current = setTimeout(async () => {
      const seq = ++previewSeq.current;
      try {
        const r = await Promise.resolve(engine.manualPreview({ trainId: d.trainId, station: d.station, kind: d.kindNow, time }));
        if (seq !== previewSeq.current) return;
        setDrag((cur) => (cur && cur.time === time ? { ...cur, preview: { ...r, reqTime: time }, previewError: null } : cur));
      } catch {
        setDrag((cur) => (cur ? { ...cur, previewError: t('Нет связи с планировщиком') } : cur));
      }
    }, PREVIEW_EVERY);
  }, []);

  /** Новое время точки: шаг, границы, вид точки у прохода (влево — прибытие, вправо — отправление). */
  const moveTo = useCallback(
    (d, raw, coarse) => {
      const step = coarse ? STEP_SHIFT : STEP;
      let kind = d.kind;
      if (kind === 'pass') kind = raw < d.orig.arr ? 'arr' : 'dep';
      const b = d.bounds[kind] ?? d.bounds.dep ?? d.bounds.arr;
      let time = Math.round(raw / step) * step;
      let clamped = null;
      if (time <= b.min) {
        time = b.min;
        clamped = 'min';
      }
      if (time >= b.max) {
        time = b.max;
        clamped = 'max';
      }
      if (time === d.time && kind === d.kindNow) return d;
      const next = { ...d, time, kindNow: kind, clamped, bound: b, preview: d.preview };
      requestPreview(next, time);
      return next;
    },
    [requestPreview],
  );

  const commit = useCallback(async () => {
    const d = dragRef.current;
    if (!d) return;
    clearTimeout(previewTimer.current);
    const from = d.kindNow === 'dep' ? d.orig.dep : d.orig.arr;
    if (d.time === from) return cancel();
    setDrag({ ...d, phase: 'committing' });
    try {
      // Решение принимает диспетчер — отпустил точку, изменение применено. Ошибся — «Отменить» в уведомлении.
      await Promise.resolve(engine.manualApply({ trainId: d.trainId, station: d.station, kind: d.kindNow, time: d.time }));
    } catch {
      engine.toast(t('Не удалось пересчитать — повторите'), 'crit');
    }
    setDrag(null);
  }, [cancel]);

  /** Нажали на ручку: границы с планировщика, дальше точка едет за мышью или стрелками. */
  const begin = useCallback(
    async (e, trainId, station, kind) => {
      e.stopPropagation();
      e.preventDefault();
      // Фокус уходит с кнопок: иначе Enter для точки нажал бы ещё и последнюю кнопку (например, свернул ГИД).
      document.activeElement?.blur?.();
      setPopup(null);
      if (s.pending?.source === 'manual') engine.cancelManual();
      const tp = plan.trains[trainId];
      const j = tp.stops.findIndex((x) => x.station === station);
      setDrag({ trainId, station, j, kind, kindNow: kind === 'pass' ? 'dep' : kind, phase: 'loading', orig: { ...tp.stops[j] }, stops: tp.stops.map((x) => ({ ...x })), time: kind === 'arr' ? tp.stops[j].arr : tp.stops[j].dep });
      let bounds;
      try {
        bounds = await Promise.resolve(engine.manualBounds(trainId, station));
      } catch {
        bounds = null;
      }
      if (!bounds) {
        engine.toast(t('Поезд или пункт не найден в плане'), 'warn');
        return cancel();
      }
      const startX = e.clientX;
      let moved = false;
      setDrag((d) => (d ? { ...d, bounds, phase: 'dragging', info: bounds.info } : d));
      const onMove = (ev) => {
        if (!moved && Math.abs(ev.clientX - startX) < 3) return;
        moved = true;
        const rect = svgRef.current.getBoundingClientRect();
        const raw = geo.invX(ev.clientX - rect.left);
        setDrag((d) => (d && d.phase === 'dragging' ? moveTo(d, raw, ev.shiftKey) : d));
      };
      const onUp = () => {
        window.removeEventListener('pointermove', onMove);
        window.removeEventListener('pointerup', onUp);
        // Клик без движения — точка выбрана, дальше стрелками (§8).
        if (!moved) setDrag((d) => (d ? { ...d, keyboard: true } : d));
        else void commit();
      };
      window.addEventListener('pointermove', onMove);
      window.addEventListener('pointerup', onUp);
    },
    [plan, geo, svgRef, moveTo, commit, cancel, s.pending?.source],
  );

  // Клавиатура: стрелки — на минуту (Shift — на 5), Enter — зафиксировать, Esc — отмена; в попапе 1/2 и Enter.
  useEffect(() => {
    const onKey = (e) => {
      if (e.target.closest?.('input, textarea, select')) return;
      const d = dragRef.current;
      if (e.key === 'Escape') {
        if (d) cancel();
        if (popup) {
          engine.cancelManual();
          setPopup(null);
        }
        setLockOpen(null);
        return;
      }
      if (d?.phase === 'dragging' && d.keyboard && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) {
        e.preventDefault();
        const step = (e.shiftKey ? STEP_SHIFT : STEP) * (e.key === 'ArrowLeft' ? -1 : 1);
        setDrag((cur) => (cur ? moveTo(cur, cur.time + step, false) : cur));
      }
      if (d?.phase === 'dragging' && e.key === 'Enter') {
        e.preventDefault();
        void commit();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [cancel, commit, moveTo, popup]);

  // Варианты ручного изменения ушли (применили или отменили) — попап закрывается.
  useEffect(() => {
    if (popup && s.pending?.source !== 'manual') {
      setPopup(null);
      setGhost(null);
    }
  }, [s.pending, popup]);

  const pend = s.pending?.source === 'manual' ? s.pending : null;
  const best = pend?.variants.length ? pend.variants.reduce((a, b) => (b.index.value > a.index.value ? b : a)) : null;
  // Пока диспетчер выбирает вариант, нитка остаётся там, куда её отпустили, — по выбранному варианту.
  const chosen = popup && pend ? (pend.variants.find((v) => v.id === popup.sel) ?? best) : null;

  // План для отрисовки: перетаскиваемая нитка — локально по правилам, затем по прогнозу планировщика.
  const viewPlan = useMemo(() => {
    if (chosen && popup) {
      const stops = chosen.plan.trains[popup.trainId]?.stops;
      if (stops) return { ...plan, trains: { ...plan.trains, [popup.trainId]: { ...plan.trains[popup.trainId], stops } } };
    }
    if (!drag || drag.phase === 'loading' || !drag.bounds) return plan;
    const fromPreview = drag.preview?.plan?.trains?.[drag.trainId]?.stops;
    const stops = fromPreview && drag.preview.reqTime === drag.time ? fromPreview : localStops(drag.stops, drag.j, drag.kindNow, drag.time, drag.info?.minDwell ?? 0);
    return { ...plan, trains: { ...plan.trains, [drag.trainId]: { ...plan.trains[drag.trainId], stops } } };
  }, [plan, drag, chosen, popup]);

  const ghostPlan = ghost ?? (drag?.preview?.plan ?? null);
  const changedIds = new Set(drag?.preview?.changed?.map((c) => c.trainId).filter((id) => id !== drag?.trainId) ?? []);

  // ───────────── слой в SVG: ручки, замки, превью, граница ─────────────
  const { x, y, L, R, T, B, w, h } = geo;
  const svgLayer = (
    <g className="md-layer">
      {/* превью: затронутые поезда пунктиром, конфликты красными кружками */}
      {drag?.preview &&
        [...changedIds].map((id) => {
          const stops = drag.preview.plan.trains[id]?.stops;
          if (!stops) return null;
          const pts = stops.flatMap((st) => [`${x(st.arr).toFixed(1)},${y(SECTION.stations[st.station].km).toFixed(1)}`, `${x(st.dep).toFixed(1)},${y(SECTION.stations[st.station].km).toFixed(1)}`]).join(' ');
          return <polyline key={id} points={pts} className="md-preview" />;
        })}
      {(ghost ?? chosen?.plan) &&
        Object.values((ghost ?? chosen.plan).trains).map((tr) => {
          if (tr.trainId === popup?.trainId) return null;
          const cur = plan.trains[tr.trainId]?.stops;
          if (!cur || tr.stops.every((st, i) => Math.abs(st.arr - (cur[i]?.arr ?? st.arr)) < 30)) return null;
          const pts = tr.stops.flatMap((st) => [`${x(st.arr).toFixed(1)},${y(SECTION.stations[st.station].km).toFixed(1)}`, `${x(st.dep).toFixed(1)},${y(SECTION.stations[st.station].km).toFixed(1)}`]).join(' ');
          return <polyline key={tr.trainId} points={pts} className="gv-preview" />;
        })}
      {drag?.preview?.conflicts?.map((c, i) => (
        <g key={i} transform={`translate(${x(c.time)} ${y(c.km)})`} className="gv-conflict">
          {c.text && <title>{t(c.text)}</title>}
          <circle r={8} />
          <text y={4} textAnchor="middle">
            !
          </text>
        </g>
      ))}

      {/* упор в границу — красная вертикаль */}
      {drag?.clamped && drag.bound && (
        <line x1={x(drag.clamped === 'min' ? drag.bound.min : drag.bound.max)} x2={x(drag.clamped === 'min' ? drag.bound.min : drag.bound.max)} y1={T} y2={h - B} className="md-bound" />
      )}

      {/* ручки: только у плановой нитки, только у точек в будущем */}
      {canEdit &&
        !popup &&
        (drag ? [drag.trainId] : focus ? [focus] : []).map((id) => {
          const tp = viewPlan.trains[id];
          if (!tp) return null;
          const train = s.trains.find((tr) => tr.id === id);
          const color = CATEGORIES[train?.category]?.color ?? '#888';
          return tp.stops.flatMap((st, j, all) => {
            const yy = y(SECTION.stations[st.station].km);
            const last = j === all.length - 1;
            const planned = j === 0 || last || train?.stops.includes(st.station);
            const pass = !planned && st.dep - st.arr <= 60;
            const pts = j === 0 ? [['dep', st.dep]] : last ? [['arr', st.arr]] : pass ? [['pass', st.arr]] : [['arr', st.arr], ['dep', st.dep]];
            return pts
              .filter(([, tt]) => tt >= s.now && x(tt) >= L && x(tt) <= w - R)
              .map(([kind, tt]) => {
                const active = drag && drag.station === st.station && (drag.kind === kind || (drag.kind === 'pass' && kind === 'pass'));
                return (
                  <circle
                    key={`${st.station}${kind}`}
                    cx={x(active ? drag.time : tt)}
                    cy={yy}
                    r={active ? 5.5 : 4.5}
                    className={`md-handle ${active ? 'on' : ''} ${drag?.phase === 'committing' && active ? 'busy' : ''}`}
                    style={{ stroke: color, fill: active ? color : undefined }}
                    onPointerDown={(e) => begin(e, id, st.station, kind)}
                    onPointerEnter={() => onHandleEnter?.(id)}
                  >
                    <title>{t(kind === 'dep' ? 'Отправление: потяните, чтобы изменить' : kind === 'arr' ? 'Прибытие: потяните, чтобы изменить' : 'Проход: влево — раньше прибытие, вправо — стоянка')}</title>
                  </circle>
                );
              });
          });
        })}

      {/* замки указаний */}
      {(s.pins ?? [])
        .filter((p) => p.status === 'active' || p.status === 'violated')
        .map((p) => {
          const st = viewPlan.trains[p.trainId]?.stops.find((x0) => x0.station === p.station);
          const tt = st ? (p.kind === 'dep' ? st.dep : st.arr) : p.time;
          const px = x(tt);
          if (px < L || px > w - R) return null;
          const py = y(SECTION.stations[p.station].km) - 18;
          return (
            <g key={p.id} transform={`translate(${px - 7} ${py})`} className={`md-lock ${p.status}`} onPointerDown={(e) => e.stopPropagation()} onClick={() => setLockOpen({ id: p.id, x: px, y: py })}>
              <rect x={-2} y={-2} width={18} height={18} rx={5} />
              <Lock x={1} y={1} width={12} height={12} strokeWidth={2.4} />
            </g>
          );
        })}
    </g>
  );

  // ───────────── слой поверх: подсказка, «Влияние», попап вариантов, карточка замка ─────────────
  let tip = null;
  if (drag && drag.phase !== 'loading' && drag.bounds) {
    const kind = drag.kindNow;
    const from = kind === 'dep' ? drag.orig.dep : drag.orig.arr;
    const station = drag.station;
    const d = drag.preview?.reqTime === drag.time ? drag.preview.dragged : null;
    const local = viewPlan.trains[drag.trainId].stops[drag.j];
    const dwell = (d?.dwell ?? local.dep - local.arr) / 60;
    const run = drag.info?.run;
    const prevDep = drag.info?.prevDep;
    const runSec = d?.prevRun ?? (prevDep != null ? local.arr - prevDep : null);
    const reason = drag.clamped ? (drag.clamped === 'min' ? drag.bound.minReason : drag.bound.maxReason) : null;
    tip = (
      <div className="md-tip" style={{ left: Math.min(x(drag.time) + 14, w - 300), top: Math.max(4, y(SECTION.stations[station].km) - 70) }}>
        <div>
          <b>{t(kind === 'dep' ? 'Отправление' : 'Прибытие')}</b> <span className="mono">{fmtHM(drag.time)}</span> <span className="muted">({fmtDelta(drag.time - from)} {t('мин')})</span>
        </div>
        {kind === 'arr' && run && runSec != null && (
          <div className="muted">
            {t('Ход {a} → {b}: {n} мин, средняя {v} км/ч (макс. {m})', {
              a: stName(drag.info.prevStation),
              b: stName(station),
              n: Math.round(runSec / 60),
              v: Math.round((run.lengthKm / Math.max(60, runSec)) * 3600),
              m: Math.round(run.vmax),
            })}
          </div>
        )}
        {drag.j > 0 && drag.j < viewPlan.trains[drag.trainId].stops.length - 1 && <div className="muted">{t('Стоянка на {st}: {n} мин', { st: stName(station), n: Math.max(0, Math.round(dwell)) })}</div>}
        {reason && <div className="crit-text">{t(reason.text)}</div>}
        {drag.keyboard && <div className="muted small">{t('← → — на минуту, Shift — на 5, Enter — готово, Esc — отмена')}</div>}
      </div>
    );
  }

  const impact = drag && drag.phase !== 'loading' && drag.bounds && (
    <div className="md-impact" onPointerDown={(e) => e.stopPropagation()}>
      <div className="md-impact-head">
        {t('Изменение: {n} {what} {st} {d} мин', {
          n: drag.trainId,
          what: t(drag.kindNow === 'dep' ? 'отправление со станции' : 'прибытие на'),
          st: stName(drag.station),
          d: fmtDelta(drag.time - (drag.kindNow === 'dep' ? drag.orig.dep : drag.orig.arr)),
        })}
      </div>
      {drag.previewError && <div className="crit-text">{drag.previewError}</div>}
      {drag.preview ? (
        <>
          <div>
            {t('Индекс: {a} → {b}', { a: shownIndex(s).value.toFixed(0), b: drag.preview.index.value.toFixed(0) })}{' '}
            <b className={drag.preview.deltaIndex >= 0 ? 'ok-text' : 'crit-text'}>
              ({drag.preview.deltaIndex >= 0 ? '+' : '−'}
              {Math.abs(drag.preview.deltaIndex).toFixed(1)})
            </b>
          </div>
          <div>{t('Суммарное опоздание: {d} мин', { d: fmtDelta(drag.preview.totalDelayDelta * 60) })}</div>
          <div className="md-affected">
            <span className="muted">{t('Затронуты:')}</span>
            {drag.preview.changed.slice(0, 6).map((c) => (
              <span key={c.trainId}>
                <b className="mono">{c.number}</b> {fmtDelta(c.deltaFinal)} {t('мин')}
              </span>
            ))}
            {drag.preview.changed.length > 6 && <span className="muted">{t('и ещё {n}', { n: drag.preview.changed.length - 6 })}</span>}
            {!drag.preview.changed.length && <span className="muted">{t('никто')}</span>}
          </div>
          <div className={drag.preview.conflicts.length ? 'crit-text' : 'muted'}>
            {drag.preview.conflicts.length ? t('Конфликты: {n}', { n: drag.preview.conflicts.length }) : t('Конфликты: нет')}
          </div>
        </>
      ) : (
        <div className="muted">{t('Считаю последствия…')}</div>
      )}
      {drag.phase === 'committing' && (
        <div className="computing small">
          <span className="spinner" /> {t('Считаем варианты…')}
        </div>
      )}
    </div>
  );

  const popupEl = popup && pend && (
    <ManualPopup
      s={s}
      pend={pend}
      best={best}
      at={{ x: x(popup.time), y: y(SECTION.stations[popup.station].km), w, h }}
      onGhost={setGhost}
      sel={chosen?.id}
      onSelect={(id) => setPopup((pp) => (pp ? { ...pp, sel: id } : pp))}
      onClose={() => {
        engine.cancelManual();
        setPopup(null);
        setGhost(null);
      }}
    />
  );

  const lock = lockOpen && (s.pins ?? []).find((p) => p.id === lockOpen.id);
  const lockEl = lock && (
    <div className="md-lockcard" style={{ left: Math.min(lockOpen.x + 12, w - 280), top: Math.max(4, lockOpen.y - 8) }} onPointerDown={(e) => e.stopPropagation()}>
      <div className="md-lockcard-head">
        <Lock size={14} /> <b>{t('Указание диспетчера')}</b>
        <button className="icon-btn" onClick={() => setLockOpen(null)}>
          <X size={14} />
        </button>
      </div>
      <div>{t(lock.description)}</div>
      <div className="muted small">{t('Установлено в {t}', { t: fmtHM(lock.createdAt ?? s.now) })}</div>
      {lock.status === 'violated' && <div className="crit-text small">{t('Невыполнимо: {r}', { r: t(lock.reason ?? '') })}</div>}
      {canEdit && (
        <button
          className="small-btn"
          onClick={() => {
            engine.removePin(lock.id);
            setLockOpen(null);
          }}
        >
          <LockOpen size={14} /> {t('Снять указание')}
        </button>
      )}
    </div>
  );

  return {
    drag,
    viewPlan,
    ghostPlan,
    dimOthers: !!drag,
    popupOpen: !!popup,
    svgLayer,
    overlay: (
      <>
        {tip}
        {impact}
        {popupEl}
        {lockEl}
      </>
    ),
  };
}

/** Карточки вариантов у точки после отпускания (§7.4): те же — в «Ситуации». */
function ManualPopup({ s, pend, best, at, onGhost, onClose, sel, onSelect }) {
  const setSel = onSelect;
  const current = shownIndex(s).value;
  useEffect(() => {
    const onKey = (e) => {
      const i = ['1', '2'].indexOf(e.key);
      if (i >= 0 && pend.variants[i]) setSel(pend.variants[i].id);
      if (e.key === 'Enter' && sel) {
        e.preventDefault();
        engine.applyVariant(sel);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [pend, sel, setSel]);
  const left = at.x + 20 + 320 > at.w ? Math.max(8, at.x - 340) : at.x + 20;
  return (
    <div className="md-popup" style={{ left, top: 4, maxHeight: Math.max(220, at.h - 8) }} onPointerDown={(e) => e.stopPropagation()}>
      <div className="md-popup-head">
        <b>{t('Ручное изменение')}</b>
        <span className="muted small">{t(pend.manual.description)}</span>
        <button className="icon-btn" onClick={onClose} title={t('Отмена')}>
          <X size={16} />
        </button>
      </div>
      {pend.variants.map((v, i) => {
        const dIdx = v.index.value - current;
        const dDelay = v.impacts.reduce((a, x) => a + x.deltaMin, 0);
        const top = [...v.impacts].filter((x) => Math.abs(x.deltaMin) >= 1).sort((a, b) => b.deltaMin - a.deltaMin).slice(0, 3);
        return (
          <div
            key={v.id}
            className={`md-card ${sel === v.id ? 'on' : ''}`}
            onMouseEnter={() => onGhost(v.plan)}
            onMouseLeave={() => onGhost(null)}
            onClick={() => setSel(v.id)}
          >
            <div className="md-card-head">
              <span className="md-num">{i + 1}</span>
              <b>{t(v.title)}</b>
              {v.id === best?.id && pend.variants.length > 1 && (
                <span className="best">
                  <Sparkles size={12} /> {t('рекомендуем')}
                </span>
              )}
            </div>
            <div className="md-card-nums">
              <span>
                {t('Индекс')} <b className={dIdx >= 0 ? 'ok-text' : 'crit-text'}>{dIdx >= 0 ? '+' : '−'}{Math.abs(dIdx).toFixed(1)}</b>
              </span>
              <span>
                {t('Опоздание')} <b>{fmtDelta(dDelay * 60)} {t('мин')}</b>
              </span>
            </div>
            {top.length > 0 && (
              <div className="vf-chips">
                {top.map((x) => (
                  <b key={x.trainId} className="vf-chip">
                    {x.number} {fmtDelta(x.deltaMin * 60)} {t('мин')}
                  </b>
                ))}
              </div>
            )}
            <ul className="why">
              {v.explanation.slice(1).map((e, k) => (
                <li key={k}>{t(e)}</li>
              ))}
            </ul>
            <div className="md-card-actions">
              <button className="ghost small-btn" onClick={() => onGhost(v.plan)}>
                {t('Показать на графике')}
              </button>
              <button className="apply" onClick={() => engine.applyVariant(v.id)}>
                {t('Применить')}
              </button>
            </div>
          </div>
        );
      })}
      <div className="muted small center">{t('1 / 2 — выбрать, Enter — применить, Esc — отмена')}</div>
    </div>
  );
}
