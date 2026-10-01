/** Выбор плана как выбор маршрута в навигаторе: что важнее → список вариантов → «Применить план». */
import { AlertTriangle, Check, ChevronRight, Hourglass, Route, Sparkles, ThumbsDown, ThumbsUp, X } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { describeDisruption } from '../core/disruptions';
import { bestFor, CRITERIA } from '../core/planner';
import { fmtDelta, fmtHM } from '../core/time';
import { DECISION_GRACE_SEC, decisionHold, shownIndex } from '../engine/engine';
import { engine } from '../engine/store';
import { EVENT_BY_KIND } from './EventPalette';
import { CATEGORIES } from '../core/section';
import { t } from '../i18n';

export function Variants({ s, selectedId, onSelect }) {
  const p = s.pending;
  const [criterion, setCriterion] = useState('optimal');
  const variants = p?.variants ?? [];
  // С сервером «рекомендуем» ставит планировщик (поле recommended), по умолчанию выбрана эта карточка.
  const serverBest = variants.find((v) => v.recommended);
  const best = variants.length ? (serverBest && criterion === 'optimal' ? serverBest : bestFor(variants, criterion)) : null;

  useEffect(() => {
    if (best) onSelect(best.id);
  }, [criterion, p?.id, p?.status]);

  const [confirm, setConfirm] = useState(null);
  const apply = (id) => {
    if (confirm !== id) return setConfirm(id);
    setConfirm(null);
    engine.applyVariant(id);
  };

  const sorted = useMemo(() => {
    const c = CRITERIA.find((x) => x.id === criterion);
    return [...variants].sort((a, b) => a.conflicts - b.conflicts || c.score(a) - c.score(b));
  }, [variants, criterion]);

  // Клавиши (§18.2): 1/2/3 — выбрать вариант, Enter — применить (с тем же подтверждением), Esc — снять выбор.
  useEffect(() => {
    const onKey = (e) => {
      if (e.target.closest?.('input, textarea, select')) return;
      const i = ['1', '2', '3'].indexOf(e.key);
      if (i >= 0 && sorted[i]) onSelect(sorted[i].id);
      if (e.key === 'Escape') setConfirm(null);
      if (e.key === 'Enter' && selectedId && sorted.some((v) => v.id === selectedId)) apply(selectedId);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  if (!p) return null;
  const sel = variants.find((v) => v.id === selectedId) ?? best;
  const ranged = s.disruptions.filter((d) => d.resolvedAt === undefined && s.now < d.start + d.durMax && d.durMax > d.durMin);
  const longest = ranged.sort((a, b) => b.durMax - a.durMax)[0];
  const recommended = s.liveMode ? serverBest?.id : variants.length > 1 ? bestFor(variants, 'optimal')?.id : null;
  const events = s.disruptions.filter((d) => p.disruptionIds.includes(d.id));
  const deadline = p.firstConflictAt !== undefined ? p.firstConflictAt - s.settings.autoApplyLeadSec : null;
  const forecast = shownIndex(s);

  return (
    <div className="decide">
      <section className="problem">
        {events.slice(-3).map((d) => {
          const { Icon } = EVENT_BY_KIND[d.kind];
          return (
            <div key={d.id} className="problem-row">
              <span className="problem-icon">
                <Icon size={18} />
              </span>
              {t(describeDisruption(d, s.trains))}
            </div>
          );
        })}
        {events.length > 3 && <div className="small">{t('и ещё {n}', { n: events.length - 3 })}</div>}
        {!events.length && <div className="problem-row">{t('В действующем плане остались конфликты — система подбирает варианты их разрешить')}</div>}
        <div className="problem-forecast">
          {t(s.forecastConflicts.length ? 'Без вмешательства: конфликтов — {n}, качество {a} → {b}' : 'Без вмешательства: конфликтов не будет, качество {a} → {b}', {
            n: s.forecastConflicts.length,
            a: s.index.value.toFixed(0),
            b: forecast.value.toFixed(0),
          })}
        </div>
      </section>

      {deadline !== null && <DecisionTimer s={s} deadline={deadline} />}
      {p.status === 'ready' && variants.length > 0 && variants.every((x) => x.conflicts > 0) && (
        <div className="stale-note">
          <AlertTriangle size={16} />
          {t('Ни один вариант не убирает все конфликты: поезда уже в пути, сменой порядка это не решить. Выберите вариант с наименьшим числом конфликтов и задержите поезд у входного светофора вручную.')}
        </div>
      )}
      {p.notice && (
        <div className="stale-note">
          <AlertTriangle size={16} /> {t(p.notice)}
        </div>
      )}

      <div className="chips">
        {CRITERIA.map((c) => (
          <button key={c.id} className={`chip ${criterion === c.id ? 'on' : ''}`} title={t(c.hint)} onClick={() => setCriterion(c.id)}>
            {t(c.label)}
          </button>
        ))}
      </div>

      {p.status === 'computing' ? (
        <div className="computing">
          <span className="spinner" /> {t('Подбираю варианты…')}
        </div>
      ) : (
        <div className="routes-list">
          {sorted.map((v) => {
            const total = v.impacts.reduce((sum, x) => sum + x.delayMin, 0);
            const on = sel?.id === v.id;
            return (
              <div key={v.id} className={`route-opt ${on ? 'on' : ''}`}>
                <button className="route-head" onClick={() => onSelect(v.id)}>
                  <Route size={22} className="route-icon" />
                  <span className="route-main">
                    <span className={`route-time ${total >= 1 ? '' : 'zero'}`}>
                      {total >= 1 ? t('+{n} мин', { n: Math.round(total) }) : t('без опозданий')}
                    </span>
                    <span className="route-name">{t(v.title)}</span>
                  </span>
                  <span className="route-side">
                    {v.id === recommended && (
                      <span className="best">
                        <Sparkles size={12} /> {t('рекомендуем')}
                      </span>
                    )}
                    {v.conflicts > 0 && <span className="danger-tag">{t('{n} конфл.', { n: v.conflicts })}</span>}
                    <IndexForecast v={v} current={forecast.value} />
                    <span>{t('пасс. {v}', { v: v.passengerDelayMin >= 1 ? t('+{n} мин', { n: Math.round(v.passengerDelayMin) }) : t('вовремя') })}</span>
                  </span>
                </button>
                {on && (
                  <div className="route-detail">
                    {v.when && <div className="when">{t(v.when)}</div>}
                    <div className="pc">
                      <div className="pc-col plus">
                        <div className="pc-title">
                          <ThumbsUp size={15} /> {t('Плюсы')}
                        </div>
                        {v.pros.length ? v.pros.map((p, i) => <div key={i} className="pc-item">{t(p)}</div>) : <div className="pc-item muted">—</div>}
                      </div>
                      <div className="pc-col minus">
                        <div className="pc-title">
                          <ThumbsDown size={15} /> {t('Минусы')}
                        </div>
                        {v.cons.length ? v.cons.map((p, i) => <div key={i} className="pc-item">{t(p)}</div>) : <div className="pc-item muted">{t('Явных минусов нет')}</div>}
                      </div>
                    </div>
                    <VariantFacts v={v} trains={s.trains} longest={longest} />
                    {v.explanation.length > 1 && (
                      <details className="why-box">
                        <summary>
                          <ChevronRight size={14} /> {t('Почему')}
                        </summary>
                        <ul className="why">
                          {v.explanation.slice(1).map((e, i) => (
                            <li key={i}>{t(e)}</li>
                          ))}
                        </ul>
                      </details>
                    )}
                    {confirm === v.id ? (
                      <div className="apply-confirm">
                        <button className="ghost" onClick={() => setConfirm(null)} title={t('Отмена')}>
                          <X size={18} />
                        </button>
                        <button className="apply sure" title={t('Подтвердить: применить «{v}»', { v: t(v.title) })} onClick={() => apply(v.id)}>
                          <Check size={18} /> {t('Подтвердить')}
                        </button>
                      </div>
                    ) : (
                      <button className="apply" onClick={() => apply(v.id)}>
                        <Check size={18} /> {t('Применить план')}
                      </button>
                    )}
                    {s.liveMode && (
                      <button className="ghost reject" onClick={() => engine.rejectVariant(v.id)}>
                        <X size={16} /> {t('Отклонить')}
                      </button>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

/** Срок решения: сначала — до какого модельного времени; когда подошёл — реальный обратный отсчёт. */
function DecisionTimer({ s, deadline }) {
  const hold = decisionHold(s);
  const left = hold ? Math.max(0, Math.ceil(DECISION_GRACE_SEC - (Date.now() - s.pending.holdSince) / 1000)) : null;
  return (
    <div className={`decide-timer ${hold ? 'hold' : ''}`}>
      <Hourglass size={18} />
      <div>
        {hold ? (
          <>
            <b>{t('Осталось {n} с на выбор', { n: left })}</b>
            <div className="small">{t('Время на карте замедлено до ×1. Потом система сама выберет «{v}».', { v: t('Подождать устранения') })}</div>
          </>
        ) : (
          <>
            <b>{t('Решить до {t}', { t: fmtHM(deadline) })}</b>
            <div className="small">{t('Если не успеть, время замедлится и будет ещё {n} с на выбор.', { n: DECISION_GRACE_SEC })}</div>
          </>
        )}
      </div>
      {hold && <i className="timer-bar" style={{ width: `${(left / DECISION_GRACE_SEC) * 100}%` }} />}
    </div>
  );
}

/** Прогноз индекса при варианте и разница с текущим — подпись отличается от шапки (§27.5). */
function IndexForecast({ v, current }) {
  const d = v.deltaIndex ?? v.index.value - current;
  const cls = d > 0.4 ? 'good' : d < -0.4 ? 'bad' : '';
  return (
    <span className="ix-forecast" title={t('Прогноз индекса при этом варианте и разница с текущим прогнозом')}>
      {t('индекс {v}', { v: v.index.value.toFixed(0) })}
      <b className={`ix-delta ${cls}`}>
        {d > 0.4 ? '+' : d < -0.4 ? '−' : '±'}
        {Math.abs(d).toFixed(1)}
      </b>
    </span>
  );
}

/** Цифры варианта: кто опоздает, остановки, энергия, устойчивость к затягиванию сбоя. */
function VariantFacts({ v, trains, longest }) {
  const late = [...v.impacts].filter((x) => x.delayMin >= 1).sort((a, b) => b.delayMin - a.delayMin).slice(0, 3);
  const cat = (id) => t(CATEGORIES[trains.find((x) => x.id === id)?.category]?.short ?? '');
  return (
    <div className="v-facts">
      <div className="vf-row stack">
        <span>{t('Опаздывают')}</span>
        <div className="vf-chips">
          {late.length ? (
            late.map((x) => (
              <b key={x.trainId} className="vf-chip">
                {x.number} <small>{cat(x.trainId)}</small> +{Math.round(x.delayMin)} {t('мин')}
              </b>
            ))
          ) : (
            <b>{t('никто')}</b>
          )}
        </div>
      </div>
      <div className="vf-row">
        <span>{t('Незапланированные остановки')}</span>
        <b>{v.unplannedStops ?? 0}</b>
      </div>
      <div className="vf-row">
        <span>{t('Энергия на тягу')}</span>
        <b>
          {Math.round(v.energyKWh).toLocaleString('ru-RU')} {t('кВт·ч')}
        </b>
      </div>
      {longest && v.robustExtraMin != null && (
        <div className="v-robust">
          {Math.round(v.robustExtraMin) >= 1
            ? t('Если сбой займёт максимум ({d} мин): +{n} мин к опозданию', { d: Math.round(longest.durMax / 60), n: Math.round(v.robustExtraMin) })
            : t('Если сбой займёт максимум ({d} мин): опоздание не вырастет', { d: Math.round(longest.durMax / 60) })}
        </div>
      )}
    </div>
  );
}
