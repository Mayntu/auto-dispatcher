/** Палитра событий: перетащите (или выберите и кликните) на перегон или поезд мнемосхемы. */
import { Ban, Clock, Dices, Gauge, PawPrint, Siren, Wrench } from 'lucide-react';
import { LIVE } from '../api/backend';
import { useLayoutEffect, useRef, useState } from 'react';
import { segmentName } from '../core/section';
import { fmtHM } from '../core/time';
import { engine } from '../engine/store';
import { t as tr } from '../i18n';

const DEMO_TYPES = [
  { kind: 'livestock', Icon: PawPrint, label: 'Препятствие (скот)', short: 'Препятствие', target: 'segment', preset: [20, 45] },
  { kind: 'closure', Icon: Ban, label: 'Окно (закрытие пути)', short: 'Окно', target: 'segment', preset: [30, 60] },
  { kind: 'signal', Icon: Siren, label: 'Отказ светофора', short: 'Светофор', target: 'segment', preset: [15, 30] },
  { kind: 'breakdown', Icon: Wrench, label: 'Неисправность поезда', short: 'Неисправность', target: 'train', preset: [15, 30] },
  { kind: 'delay', Icon: Clock, label: 'Опоздание', short: 'Опоздание', target: 'train', preset: [10, 20] },
];
/**
 * С сервером — только то, что принимает POST /api/incidents: ограничение скорости там — предупреждение
 * (speed_restriction, §27.4), опозданий в API нет.
 */
export const EVENT_TYPES = LIVE
  ? DEMO_TYPES.filter((e) => e.kind !== 'delay').map((e) =>
      e.kind === 'signal' ? { ...e, Icon: Gauge, label: 'Предупреждение (ограничение скорости)', short: 'Предупреждение', preset: [30, 60] } : e,
    )
  : DEMO_TYPES;

export const EVENT_BY_KIND = Object.fromEntries(EVENT_TYPES.map((e) => [e.kind, e]));

const MIME = 'application/x-autodispatcher-event';
let dragKind = null;

export const readDragKind = (e) => e.dataTransfer.getData(MIME) || dragKind;
export const acceptsDrag = (e) => e.dataTransfer.types.includes(MIME);
/** Во время перетаскивания браузер не отдаёт данные — тип события берём отсюда. */
export const currentDragKind = () => dragKind;

/**
 * Панель событий поверх карты. Клик — выбрать событие и кликнуть по карте; перетаскивание (после сдвига
 * больше 8 px) — за курсором едет метка события, отпустить над путём или поездом. Отпустили мимо — отмена.
 */
export function EventPalette({ armed, onArm }) {
  const [ghost, setGhost] = useState(null);

  const onPointerDown = (t, e) => {
    if (e.button !== 0) return;
    const sx = e.clientX;
    const sy = e.clientY;
    let dragging = false;
    const move = (ev) => {
      if (!dragging && Math.hypot(ev.clientX - sx, ev.clientY - sy) > 8) {
        dragging = true;
        onArm(t.kind);
      }
      if (dragging) setGhost({ t, x: ev.clientX, y: ev.clientY });
    };
    const up = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      if (dragging) {
        setGhost(null);
        // Над картой событие уже поставлено (карта обработала отпускание раньше) — здесь только снимаем выбор.
        setTimeout(() => onArm(null), 0);
      } else {
        onArm(armed === t.kind ? null : t.kind);
      }
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  };

  return (
    <div className="toolbar">
      {EVENT_TYPES.map((t) => (
        <button
          key={t.kind}
          className={`tool ${armed === t.kind ? 'on' : ''}`}
          title={tr(t.target === 'segment' ? '{kind}: нажмите и кликните по пути или перетащите на карту' : '{kind}: нажмите и кликните по поезду или перетащите на карту', { kind: tr(t.label) })}
          onPointerDown={(e) => onPointerDown(t, e)}
        >
          <t.Icon size={22} />
          <span>{tr(t.short)}</span>
        </button>
      ))}
      <span className="tool-sep" />
      <button className="tool" title={tr('Добавить 8 случайных сбоев одновременно')} onClick={() => engine.massDisruptions(8)}>
        <Dices size={22} />
        <span>{tr('8 сразу')}</span>
      </button>
      {ghost && (
        <div className="drag-ghost" style={{ left: ghost.x, top: ghost.y }}>
          <ghost.t.Icon size={16} /> {tr(ghost.t.label)}
        </div>
      )}
    </div>
  );
}

const RANGES = [
  [10, 20],
  [20, 45],
  [45, 90],
];
const STARTS = [0, 15, 30, 60];

/** Окно подтверждения в точке сброса: когда начнётся и сколько продлится. */
export function DropPopover({ draft, trains, onDone }) {
  const type = EVENT_BY_KIND[draft.kind];
  const [range, setRange] = useState(type.preset);
  const [delay, setDelay] = useState(0);
  const now = engine.getState().now;
  const onSegment = draft.segment !== undefined;
  const target = onSegment
    ? tr(draft.track !== undefined ? '{seg}, путь {track}, {km} км от начала перегона' : '{seg}, {km} км от начала перегона', {
        seg: segmentName(draft.segment),
        track: (draft.track ?? 0) + 1,
        km: (draft.pos / 1000).toFixed(1),
      })
    : tr('поезд {n}', { n: trains.find((t) => t.id === draft.trainId)?.number });
  // Окно целиком в кадре: по реальному размеру — под курсором, если не помещается — над ним, иначе прижимаем к краю.
  const box = useRef(null);
  const [pos, setPos] = useState({ left: draft.x + 12, top: draft.y + 12, ready: false });
  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const { width, height } = el.getBoundingClientRect();
    const m = 8;
    const left = Math.min(Math.max(m, draft.x + 12), window.innerWidth - width - m);
    let top = draft.y + 12;
    if (top + height > window.innerHeight - m) top = draft.y - height - 12;
    top = Math.min(Math.max(m, top), Math.max(m, window.innerHeight - height - m));
    setPos({ left, top, ready: true });
  }, [draft.x, draft.y, delay]);
  const submit = () => {
    engine.inject([
      {
        kind: draft.kind,
        segment: draft.segment,
        track: draft.track,
        pos: draft.pos,
        trainId: draft.trainId,
        start: now + delay * 60,
        minMin: range[0],
        maxMin: range[1],
      },
    ]);
    onDone();
  };
  return (
    <div
      ref={box}
      className="popover"
      onClick={(e) => e.stopPropagation()}
      style={{ left: pos.left, top: pos.top, visibility: pos.ready ? 'visible' : 'hidden' }}
    >
      <div className="pop-title">
        <type.Icon size={18} /> {tr(type.label)}
      </div>
      <div className="muted small">{target}</div>
      {onSegment && (
        <>
          <div className="pop-label">{tr('Начало')}</div>
          <div className="pop-ranges">
            {STARTS.map((m) => (
              <button key={m} className={`chip ${delay === m ? 'on' : ''}`} onClick={() => setDelay(m)}>
                {m === 0 ? tr('сейчас') : tr('+{n} мин', { n: m })}
              </button>
            ))}
          </div>
          {delay > 0 && <div className="muted small">{tr('в {t}', { t: fmtHM(now + delay * 60) })}</div>}
        </>
      )}
      <div className="pop-label">{tr('Длительность, мин')}</div>
      <div className="pop-ranges">
        {RANGES.map((r) => (
          <button key={r.join()} className={`chip ${range.join() === r.join() ? 'on' : ''}`} onClick={() => setRange(r)}>
            {r[0]}–{r[1]}
          </button>
        ))}
      </div>
      <div className="pop-custom">
        <input type="number" min={1} value={range[0]} onChange={(e) => setRange([Number(e.target.value), range[1]])} />
        <span>–</span>
        <input type="number" min={1} value={range[1]} onChange={(e) => setRange([range[0], Number(e.target.value)])} />
      </div>
      <div className="pop-actions">
        <button className="ghost" onClick={onDone}>
          {tr('Отмена')}
        </button>
        <button className="primary" onClick={submit}>
          {tr('Добавить')}
        </button>
      </div>
    </div>
  );
}
