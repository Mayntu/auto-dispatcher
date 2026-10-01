/** Пульт инструктора: события на участке, реакция диспетчера, управление учением. */
import { Dices, RotateCcw } from 'lucide-react';
import { fmtHM } from '../core/time';
import { engine } from '../engine/store';
import { ActiveEvents } from './SituationPanel';
import { t } from '../i18n';

export function InstructorPanel({ s }) {
  const p = s.pending;
  return (
    <div className="situation">
      <section className={`dispatcher-state ${p ? 'waiting' : 'calm-state'}`}>
        <div className="ds-title">{t('Диспетчер')}</div>
        {p ? (
          <div>
            {p.status === 'computing' ? t('Система считает варианты…') : t('Выбирает вариант исхода: {n} на выбор', { n: p.variants.length })}
            {p.firstConflictAt !== undefined && (
              <div className="small">{t('Решение нужно до {t}', { t: fmtHM(p.firstConflictAt - s.settings.autoApplyLeadSec) })}</div>
            )}
          </div>
        ) : (
          <div>{t('Ждёт событий — участок работает по плану')}</div>
        )}
      </section>

      <div className="row-actions">
        <button className="small-btn" onClick={() => engine.massDisruptions(8)}>
          <Dices size={16} /> {t('8 случайных событий')}
        </button>
        <button className="small-btn danger" onClick={() => engine.reset()}>
          <RotateCcw size={16} /> {t('Начать учение заново')}
        </button>
      </div>

      <ActiveEvents s={s} editable />

      {s.decisions.length > 0 && (
        <section className="events">
          <div className="q">{t('Решения диспетчера')}</div>
          {s.decisions.slice(0, 8).map((d) => (
            <div key={d.id} className="decision-item">
              <span className="mono">{fmtHM(d.t)}</span>
              <span>
                <b>{t(d.title)}</b>
                <span className="muted small">
                  {' '}
                  · {t('качество')} {d.indexBefore.toFixed(0)} → {d.indexAfter.toFixed(0)} · {t(d.by)}
                </span>
              </span>
            </div>
          ))}
        </section>
      )}

      <details className="log-box" open>
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
