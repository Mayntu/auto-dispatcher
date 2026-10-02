/**
 * Выбор поездов на ГИД: показать все или только нужные — по номеру, по категории или галочками в списке.
 * Пустой выбор (null) — показываются все поезда.
 */
import { Search, TrainFront } from 'lucide-react';
import { useMemo, useState } from 'react';
import { useDismiss } from './ui';
import { CATEGORIES, SECTION } from '../core/section';
import { fmtHM } from '../core/time';
import { t } from '../i18n';

export function TrainFilter({ trains, plan, shown, onChange }) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState('');
  // Закрывается по клику мимо (в том числе по карте) и по Esc.
  const box = useDismiss(open, () => setOpen(false));

  const list = useMemo(
    () =>
      trains
        .filter((tr) => !tr.cancelled && plan.trains[tr.id])
        .map((tr) => {
          const stops = plan.trains[tr.id].stops;
          return { tr, from: stops[0], to: stops.at(-1) };
        })
        .sort((a, b) => a.from.dep - b.from.dep),
    [trains, plan],
  );
  const all = list.map((x) => x.tr.id);
  const isOn = (id) => !shown || shown.has(id);
  const count = shown ? all.filter((id) => shown.has(id)).length : all.length;
  const cats = [...new Set(list.map((x) => x.tr.category))].filter((c) => CATEGORIES[c]);

  const setIds = (ids) => onChange(ids.length === all.length ? null : new Set(ids));
  const toggle = (id) => setIds(all.filter((x) => (x === id ? !isOn(x) : isOn(x))));
  /** Категория: если все её поезда уже выбраны — снять их, иначе показать только её. */
  const toggleCat = (c) => {
    const ids = list.filter((x) => x.tr.category === c).map((x) => x.tr.id);
    const allOn = shown && ids.every((id) => shown.has(id)) && count === ids.length;
    setIds(allOn ? all : ids);
  };
  const catOn = (c) => shown && count > 0 && list.filter((x) => isOn(x.tr.id)).every((x) => x.tr.category === c);

  const needle = q.trim();
  const rows = needle ? list.filter((x) => x.tr.number.includes(needle)) : list;

  return (
    <div className="tf" ref={box}>
      <button className={`tf-btn ${shown ? 'on' : ''}`} onClick={() => setOpen((v) => !v)}>
        <TrainFront size={15} />
        {shown ? t('Поезда: {n} из {m}', { n: count, m: all.length }) : t('Поезда: все')}
      </button>
      {open && (
        <div className="tf-pop">
          <label className="tf-search">
            <Search size={15} />
            <input autoFocus placeholder={t('Номер поезда')} value={q} onChange={(e) => setQ(e.target.value)} />
          </label>
          <div className="tf-cats">
            <button className={`chip ${!shown ? 'on' : ''}`} onClick={() => onChange(null)}>
              {t('Все')}
            </button>
            {cats.map((c) => (
              <button key={c} className={`chip ${catOn(c) ? 'on' : ''}`} onClick={() => toggleCat(c)}>
                <i className="tf-dot" style={{ background: CATEGORIES[c].color }} /> {t(CATEGORIES[c].name)}
              </button>
            ))}
          </div>
          <div className="tf-list">
            {rows.map(({ tr, from, to }) => (
              <label key={tr.id} className="tf-row">
                <input type="checkbox" checked={isOn(tr.id)} onChange={() => toggle(tr.id)} />
                <i className="tf-dot" style={{ background: CATEGORIES[tr.category]?.color }} />
                <b className="mono">{tr.number}</b>
                <span className="muted">
                  {t(SECTION.stations[from.station].short)} → {t(SECTION.stations[to.station].short)}
                </span>
                <span className="mono muted tf-time">{fmtHM(from.dep)}</span>
              </label>
            ))}
            {!rows.length && <div className="muted small">{t('Таких поездов нет')}</div>}
          </div>
          <div className="tf-foot">
            <button className="link" onClick={() => onChange(null)}>
              {t('Показать все')}
            </button>
            <button className="link" onClick={() => onChange(new Set())}>
              {t('Снять все')}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
