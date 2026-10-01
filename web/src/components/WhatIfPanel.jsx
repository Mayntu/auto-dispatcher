/** What-if: копия текущей обстановки, действующий план не меняется до «Перенести в работу». */
import { describeDisruption } from '../core/disruptions';
import { locate } from '../core/positions';
import { effectiveVmax } from '../core/scheduler';
import { CATEGORIES } from '../core/section';
import { fmtDelta } from '../core/time';
import { runningPlan, shownIndex } from '../engine/engine';
import { engine } from '../engine/store';
import { CAT_CLASS, Delta } from './ui';
import { t as tr } from '../i18n';

export function WhatIfPanel({ s }) {
  const wi = s.whatIf;
  if (!wi) {
    return (
      <div className="whatif">
        <p className="muted">
          {tr('Песочница на копии текущей обстановки: «а если поезд пойдёт 60 км/ч вместо 30?», «а если ремонт займёт 40 минут?». Прогноз рисуется фиолетовым поверх действующего плана.')}
        </p>
        <button className="primary" onClick={() => engine.whatIfOpen()}>
          {tr('Открыть песочницу')}
        </button>
      </div>
    );
  }
  const mods = wi.mods;
  const set = (m) => engine.whatIfSetMods({ ...mods, ...m });
  const plan = runningPlan(s);
  const r = wi.result;
  const cur = shownIndex(s);
  const trains = s.trains.filter((t) => !t.cancelled && locate(plan, t.id, s.now).state !== 'done');
  const events = s.disruptions.filter((d) => d.resolvedAt === undefined && s.now < d.start + d.durMax);

  return (
    <div className="whatif">
      {r && (
        <div className="wi-result">
          <div className="wi-compare">
            <span className="muted">{tr('Индекс')}</span>
            <b className={`mono ${CAT_CLASS[cur.category]}-text`}>{cur.value.toFixed(0)}</b>
            <span className="muted">→</span>
            <b className={`mono ${CAT_CLASS[r.index.category]}-text`}>{r.index.value.toFixed(0)}</b>
            <Delta value={r.index.value - cur.value} digits={1} />
          </div>
          <div className="impacts">
            {r.affected.length === 0 && <span className="muted small">{tr('Поезда не затронуты')}</span>}
            {r.affected.map((x) => (
              <span key={x.trainId} className={`impact ${x.deltaMin > 0 ? 'bad' : 'good'}`}>
                {x.number} {fmtDelta(x.deltaMin)} {tr('мин')}
              </span>
            ))}
          </div>
          <div className="row-actions">
            <button className="primary" onClick={() => engine.whatIfApply()}>
              {tr('Перенести в работу')}
            </button>
            <button className="ghost" onClick={() => engine.whatIfClose()}>
              {tr('Закрыть')}
            </button>
          </div>
        </div>
      )}

      <table className="wi-table">
        <thead>
          <tr>
            <th>{tr('Поезд')}</th>
            <th>{tr('Скорость, км/ч')}</th>
            <th>{tr('Приоритет')}</th>
            <th>{tr('Отменить')}</th>
          </tr>
        </thead>
        <tbody>
          {trains.map((t) => (
            <tr key={t.id}>
              <td>
                <i className="swatch" style={{ background: CATEGORIES[t.category].color }} /> {t.number}
              </td>
              <td>
                <input
                  type="number"
                  min={10}
                  max={160}
                  placeholder={String(effectiveVmax(t))}
                  value={mods.speed[t.id] ?? ''}
                  onChange={(e) => set({ speed: patch(mods.speed, t.id, e.target.value === '' ? undefined : Number(e.target.value)) })}
                />
              </td>
              <td>
                <select
                  value={mods.priority[t.id] ?? ''}
                  onChange={(e) =>
                    set({ priority: patch(mods.priority, t.id, e.target.value === '' ? undefined : Number(e.target.value)) })
                  }
                >
                  <option value="">—</option>
                  {[1, 2, 3, 4].map((p) => (
                    <option key={p} value={p}>
                      {p}
                    </option>
                  ))}
                </select>
              </td>
              <td>
                <input
                  type="checkbox"
                  disabled={locate(plan, t.id, s.now).state !== 'before'}
                  checked={!!mods.cancelled[t.id]}
                  onChange={(e) => set({ cancelled: patch(mods.cancelled, t.id, e.target.checked || undefined) })}
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {events.map((d) => (
        <label key={d.id} className="wi-event">
          <span>{tr(describeDisruption(d, s.trains))}</span>
          <input
            type="number"
            min={1}
            placeholder={String(Math.round(d.durExpected / 60))}
            value={mods.durationMin[d.id] ?? ''}
            onChange={(e) =>
              set({ durationMin: patch(mods.durationMin, d.id, e.target.value === '' ? undefined : Number(e.target.value)) })
            }
          />
          <span className="muted small">{tr('мин')}</span>
        </label>
      ))}
    </div>
  );
}

function patch(rec, key, value) {
  const next = { ...rec };
  if (value === undefined || Number.isNaN(value)) delete next[key];
  else next[key] = value;
  return next;
}
