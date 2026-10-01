import { Bot, Check, Route, TriangleAlert } from 'lucide-react';
import { can } from '../engine/roles';
import { describeDisruption } from '../core/disruptions';
import { locate } from '../core/positions';
import { destDelayMin } from '../core/qualityIndex';
import { durationOf } from '../core/scheduler';
import { fmtHM } from '../core/time';
import { runningPlan } from '../engine/engine';
import { engine } from '../engine/store';
import { EVENT_BY_KIND } from './EventPalette';
import { Variants } from './Variants';
import { t } from '../i18n';

export function SituationPanel({ s, selectedVariant, onSelectVariant, editable = false }) {
  return (
    <div className="situation">
      {s.pending ? <Variants s={s} selectedId={selectedVariant} onSelect={onSelectVariant} /> : <Calm s={s} />}
      <ActiveEvents s={s} editable={editable} />
      <details className="log-box">
        <summary>{t('Журнал событий')}</summary>
        <div className="log">
          {s.log.slice(0, 40).map((e) => (
            <div key={e.id} className={`log-row ${e.level}`}>
              <span className="mono">{fmtHM(e.t)}</span>
              <span>{t(e.text)}</span>
            </div>
          ))}
        </div>
      </details>
    </div>
  );
}

function Calm({ s }) {
  const plan = runningPlan(s);
  const onLine = s.trains.filter((t) => !t.cancelled && ['at', 'seg'].includes(locate(plan, t.id, s.now).state)).length;
  const late = s.trains.filter((t) => !t.cancelled && destDelayMin(plan, s.baseline, t.id) >= 1).length;
  // Система сама выбрала вариант, потому что диспетчер не успел, — об этом надо сказать, а не молча писать «всё по плану».
  const auto = s.decisions[0]?.auto && s.now - s.decisions[0].t < 15 * 60 ? s.decisions[0] : null;
  return (
    <>
      {auto && (
        <section className="auto-note">
          <Bot size={20} />
          <div>
            <b>{t('Система применила «{v}» сама', { v: t(auto.title) })}</b>
            <div className="small">{t('Решение не было выбрано вовремя ({t}). Следующее событие — снова ваш выбор.', { t: fmtHM(auto.t) })}</div>
          </div>
        </section>
      )}
      <section className={`calm ${s.conflicts.length ? 'warn' : ''}`}>
        <div className="calm-title">
          {s.conflicts.length ? <TriangleAlert size={20} /> : <Check size={20} />}{' '}
          {s.conflicts.length
            ? t('В плане остались конфликты: {n}', { n: s.conflicts.length })
            : late
              ? t('Работаем по плану, есть опоздания')
              : t('Всё идёт по плану')}
        </div>
        {s.conflicts.length > 0 && (
          <>
            <ul className="conflict-list">
              {[...s.conflicts]
                .sort((a, b) => a.time - b.time)
                .slice(0, 3)
                .map((c) => (
                  <li key={c.id}>
                    <span className="mono">{fmtHM(c.time)}</span> {t(c.text)}
                  </li>
                ))}
              {s.conflicts.length > 3 && <li className="muted">{t('и ещё {n}', { n: s.conflicts.length - 3 })}</li>}
            </ul>
            {can(s.user, 'section') && (
              <button className="apply" onClick={() => engine.askVariants()}>
                <Route size={18} /> {t('Подобрать варианты')}
              </button>
            )}
          </>
        )}
        <div className="stats">
          <div>
            <b>{onLine}</b>
            <span>{t('на участке')}</span>
          </div>
          <div>
            <b>{late}</b>
            <span>{t('опаздывают')}</span>
          </div>
          <div>
            <b>{s.conflicts.length}</b>
            <span>{t('конфликтов')}</span>
          </div>
        </div>
      </section>
    </>
  );
}

export function ActiveEvents({ s, editable }) {
  // С сервером в списке ровно активные сбои поля (field.state.incidents), даже если затянулись дольше оценки.
  const list = s.disruptions.filter((d) => d.resolvedAt === undefined && (s.liveMode || s.now < d.start + d.durMax)).reverse();
  if (!list.length) return null;
  return (
    <section className="events">
      <div className="q">{t('Активные события')}</div>
      {list.map((d) => {
        const lo = Math.round(d.durMin / 60);
        const hi = Math.round(d.durMax / 60);
        const future = d.start > s.now;
        const left = d.start + durationOf(d, 'expected') - s.now;
        return (
          <div key={d.id} className="event">
            <span className="event-icon">{icon(d.kind)}</span>
            <div className="event-main">
              <span>{t(describeDisruption(d, s.trains))}</span>
              <span className="muted small">
                {future ? t('начнётся в {t}', { t: fmtHM(d.start) }) : left > 0 ? t('ещё примерно {n} мин', { n: Math.ceil(left / 60) }) : t('должно было закончиться')}
              </span>
              {editable && <span className="event-actions">
                <button className="link" onClick={() => engine.refine(d.id, lo + 10, hi + 10)}>
                  {t('дольше на 10 мин')}
                </button>
                <button className="link" onClick={() => engine.refine(d.id, Math.max(1, lo - 10), Math.max(1, hi - 10))}>
                  {t('короче на 10 мин')}
                </button>
                {!future && (
                  <button className="link ok" onClick={() => engine.resolve(d.id)}>
                    <Check size={14} /> {t('устранено')}
                  </button>
                )}
              </span>}
            </div>
          </div>
        );
      })}
    </section>
  );
}

function icon(kind) {
  const { Icon } = EVENT_BY_KIND[kind];
  return <Icon size={20} />;
}
