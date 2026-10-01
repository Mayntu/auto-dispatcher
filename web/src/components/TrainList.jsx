/** Поезда на участке — простым списком под схемой: где сейчас и идёт ли по графику. */
import { locate } from '../core/positions';
import { destDelayMin } from '../core/qualityIndex';
import { CATEGORIES, SECTION } from '../core/section';
import { fmtHM } from '../core/time';
import { runningPlan } from '../engine/engine';
import { t as tr } from '../i18n';

export function TrainList({ s, selected, onSelect }) {
  const plan = runningPlan(s);
  const rows = s.trains
    .filter((t) => !t.cancelled && plan.trains[t.id])
    .map((t) => {
      const tp = plan.trains[t.id];
      const loc = locate(plan, t.id, s.now);
      const st = (i) => tr(SECTION.stations[tp.stops[i].station].short);
      const where =
        loc.state === 'before'
          ? tr('отправится в {t}', { t: fmtHM(tp.stops[0].dep) })
          : loc.state === 'done'
            ? tr('прибыл')
            : loc.state === 'at'
              ? tr('стоит: {st}', { st: st(loc.k) })
              : `${st(loc.k)} → ${st(loc.k + 1)}`;
      return { t, where, delay: destDelayMin(plan, s.baseline, t.id), done: loc.state === 'done', order: tp.stops[0].dep };
    })
    .sort((a, b) => a.done - b.done || a.order - b.order);

  return (
    <div className="train-list">
      {rows.map(({ t, where, delay, done }) => (
        <button key={t.id} className={`train-row ${selected === t.id ? 'on' : ''} ${done ? 'done' : ''}`} onClick={() => onSelect(t.id)}>
          <span className="tr-dot" style={{ background: CATEGORIES[t.category].color }} />
          <b className="mono">{t.number}</b>
          <span className="tr-where">{where}</span>
          <span className={`tr-status ${delay >= 1 ? 'late' : 'ok'}`}>{delay >= 1 ? tr('+{n} мин', { n: Math.round(delay) }) : tr('вовремя')}</span>
        </button>
      ))}
    </div>
  );
}
