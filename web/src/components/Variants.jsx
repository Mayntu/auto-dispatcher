/**
 * Окно решения диспетчера — в один экран: что случилось и до скольких решать, таблица вариантов
 * (опоздание, пассажирские, индекс, конфликты) с сортировкой по столбцу, коротко — главный плюс, минус
 * и кого задержит; подробности по запросу. Кнопка «Применить» всегда видна внизу панели.
 * Клавиши: 1/2/3… — выбрать, Enter — применить (с подтверждением), Esc — отменить подтверждение.
 */
import { AlertTriangle, Check, ChevronRight, Hourglass, Sparkles, X } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { describeDisruption } from '../core/disruptions';
import { bestFor } from '../core/planner';
import { CATEGORIES } from '../core/section';
import { fmtHM } from '../core/time';
import { DECISION_GRACE_SEC, decisionHold, shownIndex } from '../engine/engine';
import { engine } from '../engine/store';
import { t } from '../i18n';
import { EVENT_BY_KIND } from './EventPalette';

const totalOf = (v) => v.impacts.reduce((sum, x) => sum + x.delayMin, 0);

/** Сортировки по столбцам; по умолчанию — рекомендованный порядок (без конфликтов, затем выше индекс). */
const SORTS = {
  rec: (a, b) => a.conflicts - b.conflicts || b.index.value - a.index.value,
  delay: (a, b) => totalOf(a) - totalOf(b),
  pass: (a, b) => a.passengerDelayMin - b.passengerDelayMin,
  index: (a, b) => b.index.value - a.index.value,
};

export function Variants({ s, selectedId, onSelect }) {
  const p = s.pending;
  const variants = p?.variants ?? [];
  const [sortKey, setSortKey] = useState('rec');
  const [confirm, setConfirm] = useState(null);
  const recommended = variants.length ? bestFor(variants, 'optimal') : null;

  // Новый набор вариантов — выбран рекомендованный.
  useEffect(() => {
    if (recommended) onSelect(recommended.id);
    setConfirm(null);
  }, [p?.id, p?.status]);

  const sorted = useMemo(() => [...variants].sort(SORTS[sortKey]), [variants, sortKey]);

  const apply = (id) => {
    if (confirm !== id) return setConfirm(id);
    setConfirm(null);
    engine.applyVariant(id);
  };

  useEffect(() => {
    const onKey = (e) => {
      if (e.target.closest?.('input, textarea, select')) return;
      const i = Number(e.key) - 1;
      if (Number.isInteger(i) && i >= 0 && sorted[i]) onSelect(sorted[i].id);
      if (e.key === 'Escape') setConfirm(null);
      if (e.key === 'Enter' && selectedId && sorted.some((v) => v.id === selectedId)) {
        e.preventDefault();
        apply(selectedId);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  if (!p) return null;
  const sel = variants.find((v) => v.id === selectedId) ?? recommended;
  const events = s.disruptions.filter((d) => p.disruptionIds.includes(d.id));
  const deadline = p.firstConflictAt !== undefined ? p.firstConflictAt - s.settings.autoApplyLeadSec : null;
  const current = shownIndex(s).value;
  const ranged = s.disruptions.filter((d) => d.resolvedAt === undefined && s.now < d.start + d.durMax && d.durMax > d.durMin);
  const longest = ranged.sort((a, b) => b.durMax - a.durMax)[0];

  // Почему рекомендуем не самый быстрый вариант — коротко, чтобы не выглядело ошибкой.
  const fastest = variants.length ? [...variants].sort(SORTS.delay)[0] : null;
  let recReason = null;
  if (recommended && fastest && fastest.id !== recommended.id) {
    recReason = fastest.conflicts > recommended.conflicts ? t('у вариантов быстрее — конфликты') : t('лучше по общему качеству');
  }

  const head = events[events.length - 1];
  const HeadIcon = head ? EVENT_BY_KIND[head.kind].Icon : AlertTriangle;
  const col = (key, label) => (
    <button className={`dz-th ${sortKey === key ? 'on' : ''}`} onClick={() => setSortKey(sortKey === key ? 'rec' : key)} title={t('Сортировать')}>
      {t(label)}
    </button>
  );

  return (
    <div className="dz">
      {/* что случилось и до скольких решать */}
      <section className="dz-head">
        <HeadIcon size={18} className="dz-head-icon" />
        <div className="dz-head-text">
          <div className="dz-what">
            {head
              ? t(describeDisruption(head, s.trains))
              : p.source === 'manual'
                ? t(p.manual.description)
                : t('В действующем плане остались конфликты — система подбирает варианты их разрешить')}
            {events.length > 1 && <span className="muted"> · {t('и ещё {n}', { n: events.length - 1 })}</span>}
          </div>
          {deadline !== null && <Deadline s={s} deadline={deadline} />}
        </div>
      </section>

      {p.status === 'ready' && variants.length > 0 && variants.every((x) => x.conflicts > 0) && (
        <div className="dz-note">
          <AlertTriangle size={14} />
          {t('Конфликты остаются во всех вариантах — поезда уже в пути. Выберите вариант с наименьшим числом и задержите поезд у входного светофора вручную.')}
        </div>
      )}
      {p.notice && (
        <div className="dz-note">
          <AlertTriangle size={14} /> {t(p.notice)}
        </div>
      )}

      {p.status === 'computing' ? (
        <div className="computing">
          <span className="spinner" /> {t('Подбираю варианты…')}
        </div>
      ) : (
        <>
          {/* таблица вариантов */}
          <div className="dz-table" role="radiogroup">
            <div className="dz-row dz-hrow">
              <span className="dz-th-name">{t('Вариант')}</span>
              {col('delay', 'Опозд.')}
              {col('pass', 'Пасс.')}
              {col('index', 'Индекс')}
            </div>
            {sorted.map((v) => {
              const on = sel?.id === v.id;
              const total = totalOf(v);
              const d = v.index.value - current;
              return (
                <button key={v.id} className={`dz-row ${on ? 'on' : ''}`} onClick={() => onSelect(v.id)} title={v.when ? t(v.when) : undefined} role="radio" aria-checked={on}>
                  <span className="dz-name">
                    <i className="dz-radio" />
                    <span className="dz-title">{t(v.title)}</span>
                    {v.id === recommended?.id && variants.length > 1 && <Sparkles size={13} className="dz-star" aria-label={t('рекомендуем')} />}
                    {v.conflicts > 0 && <span className="dz-conf">⚠{v.conflicts}</span>}
                  </span>
                  <span className={`dz-n ${total < 1 ? 'zero' : ''}`}>{total < 1 ? '0' : `+${Math.round(total)}`}</span>
                  <span className={`dz-n ${v.passengerDelayMin < 1 ? 'zero' : ''}`}>{v.passengerDelayMin < 1 ? '0' : `+${Math.round(v.passengerDelayMin)}`}</span>
                  <span className="dz-n">
                    {v.index.value.toFixed(0)}
                    <small className={d > 0.4 ? 'good' : d < -0.4 ? 'bad' : ''}>
                      {d > 0.4 ? '+' : d < -0.4 ? '−' : '±'}
                      {Math.abs(d).toFixed(0)}
                    </small>
                  </span>
                </button>
              );
            })}
            <div className="dz-legend muted">{t('Опоздание — в минутах')}</div>
          </div>

          {/* коротко о выбранном */}
          {sel && <Summary v={sel} recommended={sel.id === recommended?.id && variants.length > 1} recReason={recReason} trains={s.trains} longest={longest} />}

          {/* кнопка всегда внизу панели */}
          {sel && (
            <div className="dz-foot">
              {confirm === sel.id ? (
                <>
                  <button className="ghost dz-cancel" onClick={() => setConfirm(null)} title={t('Отмена')}>
                    <X size={18} />
                  </button>
                  <button className="apply sure" onClick={() => apply(sel.id)}>
                    <Check size={18} /> {t('Подтвердить')}
                  </button>
                </>
              ) : (
                <button className="apply" onClick={() => apply(sel.id)}>
                  <Check size={18} /> {t('Применить «{v}»', { v: t(sel.title) })}
                </button>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}

/** Главный плюс, главный минус, кого задержит; остальное — под «Подробнее». */
function Summary({ v, recommended, recReason, trains, longest }) {
  const late = [...v.impacts].filter((x) => x.delayMin >= 1).sort((a, b) => b.delayMin - a.delayMin);
  const cat = (id) => t(CATEGORIES[trains.find((x) => x.id === id)?.category]?.short ?? '');
  return (
    <section className="dz-sum">
      {recommended && (
        <div className="dz-rec">
          <Sparkles size={13} /> {t('рекомендуем')}
          {recReason && <span className="muted"> — {recReason}</span>}
        </div>
      )}
      <Steps steps={v.steps ?? []} />
      {v.pros[0] && <div className="dz-pro">+ {t(v.pros[0])}</div>}
      {v.cons[0] && <div className="dz-con">− {t(v.cons[0])}</div>}
      <div className="dz-late">
        <span className="muted">{t('Задержит:')}</span>
        {late.length ? (
          <>
            {late.slice(0, 3).map((x) => (
              <b key={x.trainId}>
                {x.number} <small>{cat(x.trainId)}</small> +{Math.round(x.delayMin)}
              </b>
            ))}
            {late.length > 3 && <span className="muted">{t('и ещё {n}', { n: late.length - 3 })}</span>}
          </>
        ) : (
          <b className="ok-text">{t('никого')}</b>
        )}
      </div>
      <details className="dz-more">
        <summary>
          <ChevronRight size={14} /> {t('Подробнее')}
        </summary>
        {v.when && <p className="dz-when">{t(v.when)}</p>}
        {(v.pros.length > 1 || v.cons.length > 1) && (
          <ul className="dz-list">
            {v.pros.slice(1).map((x, i) => (
              <li key={`p${i}`} className="dz-pro">
                + {t(x)}
              </li>
            ))}
            {v.cons.slice(1).map((x, i) => (
              <li key={`c${i}`} className="dz-con">
                − {t(x)}
              </li>
            ))}
          </ul>
        )}
        <div className="dz-facts">
          <span>{t('Незапланированные остановки')}</span>
          <b>{v.unplannedStops ?? 0}</b>
          <span>{t('Энергия на тягу')}</span>
          <b>
            {Math.round(v.energyKWh).toLocaleString('ru-RU')} {t('кВт·ч')}
          </b>
          {longest && v.robustExtraMin != null && (
            <>
              <span>{t('Если сбой займёт максимум ({d} мин)', { d: Math.round(longest.durMax / 60) })}</span>
              <b>{Math.round(v.robustExtraMin) >= 1 ? t('+{n} мин', { n: Math.round(v.robustExtraMin) }) : t('без изменений')}</b>
            </>
          )}
        </div>
        {v.explanation.length > 1 && (
          <ul className="why">
            {v.explanation.slice(1).map((e, i) => (
              <li key={i}>{t(e)}</li>
            ))}
          </ul>
        )}
      </details>
    </section>
  );
}

/** Срок решения одной строкой; когда подошёл — обратный отсчёт с полоской. */
function Deadline({ s, deadline }) {
  const hold = decisionHold(s);
  const left = hold ? Math.max(0, Math.ceil(DECISION_GRACE_SEC - (Date.now() - s.pending.holdSince) / 1000)) : null;
  return (
    <div className={`dz-deadline ${hold ? 'hold' : ''}`} title={hold ? t('Время на карте замедлено до ×1. Потом система сама выберет «{v}».', { v: t('Подождать устранения') }) : t('Если не успеть, время замедлится и будет ещё {n} с на выбор.', { n: DECISION_GRACE_SEC })}>
      <Hourglass size={13} />
      {hold ? t('осталось {n} с — потом система выберет сама', { n: left }) : t('решить до {t}', { t: fmtHM(deadline) })}
      {hold && <i className="timer-bar" style={{ width: `${(left / DECISION_GRACE_SEC) * 100}%` }} />}
    </div>
  );
}

/** Что именно сделать по этому варианту — по шагам, по времени. */
function Steps({ steps }) {
  const [all, setAll] = useState(false);
  if (!steps.length) return <div className="dz-steps-none muted">{t('Порядок и время поездов не меняются — ждём устранения')}</div>;
  const shown = all ? steps : steps.slice(0, 4);
  return (
    <div className="dz-steps">
      <div className="dz-steps-head">{t('Шаги')}</div>
      <ol>
        {shown.map((x, i) => (
          <li key={i}>
            <span className="mono dz-step-t">{fmtHM(x.t)}</span> {t(x.text)}
          </li>
        ))}
      </ol>
      {steps.length > 4 && (
        <button className="link dz-steps-more" onClick={() => setAll((v) => !v)}>
          {all ? t('Свернуть') : t('Ещё шагов: {n}', { n: steps.length - 4 })}
        </button>
      )}
    </div>
  );
}
