import { ArrowRight } from 'lucide-react';
import { useState } from 'react';
import { CATEGORY_LABEL } from '../core/qualityIndex';
import { fmtHM, fmtHMS } from '../core/time';
import { SNAPSHOT_STEP } from '../engine/history';
import { downloadCsv, printReport } from '../engine/report';
import { engine } from '../engine/store';
import { CAT_CLASS, Empty } from './ui';
import { t as tr } from '../i18n';
export function RetroPanel({ s, replayT, onReplay }) {
  const [win, setWin] = useState(15);
  const first = engine.history.first?.t ?? s.now;
  const from = Math.max(first, s.now - win * 60);
  const to = s.now;
  const value = replayT ?? to;
  const snap = replayT !== null ? engine.history.at(replayT) : undefined;
  const snaps = engine.history.range(from, to);
  const decisions = s.decisions.filter((d) => d.t >= from);
  const hours = ((s.historySize * SNAPSHOT_STEP) / 3600).toFixed(1);
  return (
    <div className="retro">
      <div className="retro-head">
        <span className="muted">{tr('Окно перемотки:')}</span>
        {[5, 15].map((m) => (
          <button key={m} className={`chip ${win === m ? 'on' : ''}`} onClick={() => setWin(m)}>
            {m} {tr('мин')}
          </button>
        ))}
        {replayT !== null && (
          <button className="ghost small-btn" onClick={() => onReplay(null)}>
            <ArrowRight size={14} /> {tr('Вернуться к текущему моменту')}
          </button>
        )}
      </div>

      <input
        type="range"
        className="scrub"
        min={from}
        max={to}
        step={SNAPSHOT_STEP}
        value={Math.min(Math.max(value, from), to)}
        onChange={(e) => {
          const t = Number(e.target.value);
          onReplay(t >= to - SNAPSHOT_STEP ? null : t);
        }}
      />
      <div className="scrub-scale mono small muted">
        <span>{fmtHM(from)}</span>
        <span>{replayT !== null ? tr('показан момент {t}', { t: fmtHMS(replayT) }) : 'live'}</span>
        <span>{fmtHM(to)}</span>
      </div>

      <Sparkline snaps={snaps} cursor={replayT} from={from} to={to} />

      {snap ? (
        <div className="snap">
          <div>
            <span className="muted small">{tr('Момент')}</span>
            <b className="mono">{fmtHMS(snap.t)}</b>
          </div>
          <div>
            <span className="muted small">{tr('Индекс')}</span>
            <b className={`mono ${CAT_CLASS[snap.category]}-text`}>
              {snap.index.toFixed(0)} · {tr(CATEGORY_LABEL[snap.category])}
            </b>
          </div>
          <div>
            <span className="muted small">{tr('Конфликтов')}</span>
            <b className="mono">{snap.conflicts}</b>
          </div>
          <div>
            <span className="muted small">{tr('Сбоев')}</span>
            <b className="mono">{snap.disruptions}</b>
          </div>
        </div>
      ) : (
        <p className="muted small">
          {tr('Сдвиньте ползунок — мнемосхема покажет обстановку в выбранный момент.')}
        </p>
      )}

      <div className="block-title">{tr('Решения за окно')}</div>
      {decisions.length === 0 && <Empty>{tr('Решений за последние {n} минут не было', { n: win })}</Empty>}
      {decisions.map((d) => (
        <button key={d.id} className="decision-row" onClick={() => onReplay(Math.max(from, d.t - 30))}>
          <span className="mono">{fmtHMS(d.t)}</span>
          <span>
            {tr(d.title)}
            {d.auto ? ` (${tr('авто')})` : ''}
          </span>
          <span className="mono">
            {d.indexBefore.toFixed(0)} → {d.indexAfter.toFixed(0)}
          </span>
        </button>
      ))}

      <div className="row-actions">
        <button className="primary" onClick={() => downloadCsv(s, snaps, from, to)}>
          {tr('Отчёт CSV')}
        </button>
        <button className="ghost" onClick={() => printReport(s, snaps, from, to)}>
          {tr('Отчёт PDF')}
        </button>
      </div>
      <div className="muted small">
        {tr('История: {n} снимков ({h} ч модельного времени), хранение {keep} ч', { n: s.historySize.toLocaleString('ru-RU'), h: hours, keep: s.settings.historyHours })}
      </div>
    </div>
  );
}
function Sparkline({ snaps, cursor, from, to }) {
  if (snaps.length < 2) return <Empty>{tr('История накапливается…')}</Empty>;
  const W = 400;
  const H = 70;
  const x = (t) => ((t - from) / Math.max(1, to - from)) * W;
  const y = (v) => H - (v / 100) * H;
  const pts = snaps.map((p) => `${x(p.t).toFixed(1)},${y(p.index).toFixed(1)}`).join(' ');
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="spark" preserveAspectRatio="none">
      <line x1={0} x2={W} y1={y(80)} y2={y(80)} className="th norm" />
      <line x1={0} x2={W} y1={y(60)} y2={y(60)} className="th warn" />
      <polyline points={pts} className="spark-line" />
      {cursor !== null && <line x1={x(cursor)} x2={x(cursor)} y1={0} y2={H} className="replay" />}
    </svg>
  );
}
