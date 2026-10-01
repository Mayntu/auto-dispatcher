/** Плашки поверх карты: статус участка с качеством движения и часы модели. */
import { Pause, Play, ShieldAlert, ShieldCheck, Wifi, WifiOff } from 'lucide-react';
import { fmtHM } from '../core/time';
import { decisionHold, shownIndex } from '../engine/engine';
import { engine } from '../engine/store';
import { CAT_CLASS } from './ui';
import { t } from '../i18n';

export function StatusPill({ s }) {
  const idx = shownIndex(s);
  const c = s.live?.counters;
  const status = s.liveMode
    ? s.pending
      ? { cls: 'alert', text: t('Требуется решение') }
      : s.planBroken
        ? { cls: 'alert', text: t('План перестал выполняться') }
        : s.disruptions.length
          ? { cls: 'warn', text: t('Сбой на участке') }
          : { cls: 'ok', text: t('Штатно') }
    : s.pending
    ? { cls: 'alert', text: t('Сбой на участке — нужно решение') }
    : s.conflicts.length
      ? { cls: 'warn', text: t('Конфликтов в плане: {n}', { n: s.conflicts.length }) }
      : { cls: 'ok', text: t('Всё идёт по плану') };
  return (
    <details className={`status-pill ${status.cls}`}>
      <summary title={`${status.text}. ${t('Нажмите, чтобы увидеть, из чего складывается качество движения')}`}>
        <i className="dot" />
        <span className="st-text">{status.text}</span>
        <span className="sep" />
        <span className="muted-l">{t('качество')}</span>
        <b className={CAT_CLASS[idx.category]}>{idx.value.toFixed(0)}</b>
        {c && (
          <span className="st-counters muted-l" title={t('Строка статуса')}>
            {t('в пути {a} · на станциях {b} · ожидают {c} · прибыли {d}', { a: c.in_transit, b: c.at_stations, c: c.waiting, d: c.arrived })}
          </span>
        )}
      </summary>
      <div className="quality-pop">
        <div className="pop-head">{t('Качество движения: {v} из 100', { v: idx.value.toFixed(0) })}</div>
        {idx.factors.map((f) => (
          <div key={f.key} title={t(f.detail)}>
            <div className="factor-row">
              <span>{t(f.name)}</span>
              <b>{(f.score * 100).toFixed(0)}</b>
            </div>
            <div className="bar">
              <i style={{ width: `${f.score * 100}%` }} className={f.score >= 0.8 ? 'ok' : f.score >= 0.6 ? 'warn' : 'crit'} />
            </div>
          </div>
        ))}
      </div>
    </details>
  );
}

const SPEEDS = [
  { v: 1, label: '×1' },
  { v: 10, label: '×10' },
  { v: 20, label: '×20' },
  { v: 60, label: '×60' },
];

export function ClockPill({ s, controls = false }) {
  if (!controls) {
    return (
      <div className="clock-pill">
        <span className="mono time">{fmtHM(s.now)}</span>
        <span className="clock-speed">{s.running ? `×${s.live?.effectiveSpeed ?? (decisionHold(s) ? 1 : s.speed)}` : t('пауза')}</span>
        {decisionHold(s) && <span className="clock-hold">{t('ждём решения')}</span>}
      </div>
    );
  }
  return (
    <div className="clock-pill">
      <button className="round" title={t(s.running ? 'Остановить время' : 'Запустить время')} onClick={() => engine.setRunning(!s.running)}>
        {s.running ? <Pause size={16} /> : <Play size={16} />}
      </button>
      <span className="mono time">{fmtHM(s.now)}</span>
      <div className="seg-ctl" role="group" aria-label={t('Скорость времени')}>
        {SPEEDS.map((o) => (
          <button key={o.v} className={s.speed === o.v ? 'on' : ''} onClick={() => engine.setSpeed(o.v)}>
            {o.label}
          </button>
        ))}
      </div>
      {decisionHold(s) && <span className="clock-hold">{t('ждём решения')}</span>}
    </div>
  );
}

/**
 * Живые нефункциональные метрики (§3, §18.2): за сколько пересчитан план, p95 задержки обновления экрана
 * (только с сервером — её считает фронт по ts_wall конвертов) и счётчик нарушений безопасности.
 */
export function NfrPill({ s }) {
  const solve = s.metrics?.lastVariantsMs || s.metrics?.lastReplanMs || 0;
  const violations = s.safetyViolations ?? 0;
  const fmt = (ms) => (ms >= 1000 ? t('{n} с', { n: (ms / 1000).toFixed(1) }) : t('{n} мс', { n: Math.round(ms) }));
  return (
    <div className="nfr-pill">
      <span className={`nfr-safety ${violations ? 'bad' : ''}`} title={t('Нарушений безопасности')}>
        {violations ? <ShieldAlert size={16} /> : <ShieldCheck size={16} />}
        {t('Нарушений безопасности: {n}', { n: violations })}
      </span>
      {solve > 0 && <span title={t('Время последнего расчёта плана')}>{t('план пересчитан за {v}', { v: fmt(solve) })}</span>}
      {s.latencyP95 != null && <span title={t('p95 задержки обновления экрана')}>{t('p95 UI {v}', { v: fmt(s.latencyP95) })}</span>}
      {s.liveMode && (
        <span className={`nfr-conn ${s.connection}`}>
          {s.connection === 'online' ? <Wifi size={14} /> : <WifiOff size={14} />}
          {t(s.connection === 'online' ? 'сервер на связи' : s.connection === 'connecting' ? 'подключение…' : 'нет связи с сервером')}
        </span>
      )}
    </div>
  );
}
