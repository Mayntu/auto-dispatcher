import { Activity, ArrowLeft, FlaskConical, History, LogOut, Settings, Siren, TrainFront } from 'lucide-react';
import { useEffect, useState } from 'react';
import { DropPopover, EVENT_BY_KIND, EventPalette } from './components/EventPalette';
import { InstructorPanel } from './components/InstructorPanel';
import { Login } from './components/Login';
import { ClockPill, NfrPill, StatusPill } from './components/MapOverlays';
import { MapView } from './components/MapView';
import { RetroPanel } from './components/RetroPanel';
import { SettingsPanel } from './components/SettingsPanel';
import { SituationPanel } from './components/SituationPanel';
import { TrainGraphView } from './components/TrainGraphView';
import { TrainList } from './components/TrainList';
import { TrainPanel } from './components/TrainPanel';
import { WhatIfPanel } from './components/WhatIfPanel';
import { fmtHM } from './core/time';
import { can, ROLES } from './engine/roles';
import { engine, useEngineState } from './engine/store';
import { t as tr, useLang } from './i18n';
import { LangSwitch } from './components/ui';

/** Вкладки диспетчера и администратора. Инструктор работает на своём пульте. */
const TABS = [
  { id: 'situation', label: 'Ситуация', Icon: Activity },
  { id: 'trains', label: 'Поезда', Icon: TrainFront },
  { id: 'whatif', label: 'Что если', Icon: FlaskConical },
  { id: 'history', label: 'История', Icon: History },
  { id: 'settings', label: 'Настройки', Icon: Settings, permission: 'settings' },
];

export default function App() {
  const s = useEngineState();
  useLang(); // смена языка перерисовывает весь интерфейс
  const [tab, setTab] = useState('situation');
  const [selected, setSelected] = useState(null);
  const [variantId, setVariantId] = useState(null);
  const [replayT, setReplayT] = useState(null);
  const [armed, setArmed] = useState(null);
  const [draft, setDraft] = useState(null);
  const [toast, setToast] = useState(null);
  const [graphOpen, setGraphOpen] = useState(false);
  // Уведомления движка: указание применено, снято, невыполнимо (красное — до закрытия, с кнопками).
  const [notice, setNotice] = useState(null);
  const toastId = s.toast?.id;
  useEffect(() => {
    if (!s.toast) return undefined;
    setNotice(s.toast);
    if (s.toast.kind === 'crit') return undefined;
    const id = setTimeout(() => setNotice((n) => (n?.id === s.toast.id ? null : n)), 3500);
    return () => clearTimeout(id);
  }, [toastId]);

  const pendingId = s.pending?.id;
  useEffect(() => {
    if (pendingId) setTab('situation');
  }, [pendingId]);

  useEffect(() => {
    if (!can(s.user, 'settings')) setTab((t) => (t === 'settings' ? 'situation' : t));
  }, [s.user]);

  useEffect(() => {
    const onKey = (e) => {
      if (e.key !== 'Escape') return;
      setArmed(null);
      setDraft(null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  useEffect(() => {
    if (!toast) return;
    const id = setTimeout(() => setToast(null), 2500);
    return () => clearTimeout(id);
  }, [toast]);

  if (!s.user) return <Login />;

  const instructor = can(s.user, 'scenario');
  const onPlace = (kind, target, x, y) => {
    setArmed(null);
    if (!target) {
      setToast(tr('Не попали: бросьте событие прямо на путь или поезд'));
      return;
    }
    setDraft({ kind, ...target, x, y });
  };
  const selectTrain = (id) => {
    setSelected(id);
    if (!instructor) setTab('trains');
  };

  const snapshot = replayT !== null ? engine.history.at(replayT) : undefined;
  const preview = s.pending && variantId ? s.pending.variants.find((v) => v.id === variantId)?.plan : undefined;
  const tabs = TABS.filter((t) => !t.permission || can(s.user, t.permission));
  const current = tabs.find((t) => t.id === tab) ?? tabs[0];

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="side-top">
          <span className={`logo ${instructor ? 'instructor' : ''}`}>{instructor ? <Siren size={18} /> : <TrainFront size={18} />}</span>
          <b>{tr(instructor ? 'Пульт инструктора' : 'Автодиспетчер')}</b>
          <span className={`role ${s.user.role}`}>{tr(ROLES[s.user.role].name)}</span>
          <LangSwitch />
          <button className="icon-btn" title={tr('Выйти')} onClick={() => engine.logout()}>
            <LogOut size={18} />
          </button>
        </div>

        {instructor ? (
          <>
            <h1 className="side-title">{tr('Учение')}</h1>
            <div className="side-body">
              <InstructorPanel s={s} />
            </div>
          </>
        ) : (
          <>
            <nav className="modes">
              {tabs.map((t) => (
                <button key={t.id} className={current.id === t.id ? 'on' : ''} onClick={() => setTab(t.id)} title={tr(t.label)}>
                  <t.Icon size={22} />
                  <span>{tr(t.label)}</span>
                  {t.id === 'situation' && s.pending && <i className="alert-dot" />}
                </button>
              ))}
            </nav>
            <h1 className="side-title">
              {current.id === 'trains' && selected ? (
                <button className="back" onClick={() => setSelected(null)}>
                  <ArrowLeft size={22} /> {tr('Все поезда')}
                </button>
              ) : (
                tr(current.label)
              )}
            </h1>
            <div className="side-body">
              {current.id === 'situation' && <SituationPanel s={s} selectedVariant={variantId} onSelectVariant={setVariantId} />}
              {current.id === 'trains' &&
                (selected ? (
                  <TrainPanel s={s} selected={selected} onSelect={setSelected} />
                ) : (
                  <TrainList s={s} selected={selected} onSelect={setSelected} />
                ))}
              {current.id === 'whatif' && <WhatIfPanel s={s} />}
              {current.id === 'history' && <RetroPanel s={s} replayT={replayT} onReplay={setReplayT} />}
              {current.id === 'settings' && <SettingsPanel s={s} />}
            </div>
          </>
        )}
      </aside>

      <main className="stage">
        <div className="map-area">
          <MapView
            s={s}
            snapshot={snapshot}
            selected={selected}
            onSelect={selectTrain}
            armed={armed}
            onPlace={onPlace}
            allowEvents={instructor}
          />
          <div className="overlay-top">
            <ClockPill s={s} controls={instructor} />
            <StatusPill s={s} />
            {instructor ? <EventPalette armed={armed} onArm={setArmed} /> : <span className="toolbar-spacer" />}
            {!instructor && <NfrPill s={s} />}
          </div>
          {(armed || snapshot) && (
            <div className="hint-pill">
              {armed
                ? tr(EVENT_BY_KIND[armed].target === 'segment' ? '{kind}: кликните по пути · Esc — отмена' : '{kind}: кликните по поезду · Esc — отмена', { kind: tr(EVENT_BY_KIND[armed].label) })
                : tr('История: показан момент {t}', { t: fmtHM(snapshot.t) })}
            </div>
          )}
        </div>
        <TrainGraphView
          s={s}
          selected={selected}
          onSelect={selectTrain}
          preview={preview}
          open={graphOpen}
          onToggle={() => setGraphOpen((o) => !o)}
        />
      </main>

      {draft && <DropPopover draft={draft} trains={s.trains} onDone={() => setDraft(null)} />}
      {toast && <div className="toast">{toast}</div>}
      {notice && !toast && (
        <div className={`toast ${notice.kind === 'crit' ? 'crit' : ''}`}>
          <span>{tr(notice.text)}</span>
          {notice.undoPinId && (
            <button
              onClick={() => {
                engine.removePin(notice.undoPinId);
                setNotice(null);
              }}
            >
              {tr('Отменить')}
            </button>
          )}
          {notice.pinId && (
            <>
              <button
                onClick={() => {
                  engine.removePin(notice.pinId);
                  setNotice(null);
                }}
              >
                {tr('Снять')}
              </button>
              <button
                onClick={() => {
                  setGraphOpen(true);
                  setSelected(notice.trainId);
                  setNotice(null);
                }}
              >
                {tr('Показать')}
              </button>
            </>
          )}
          {notice.kind === 'crit' && (
            <button className="toast-x" onClick={() => setNotice(null)} title={tr('Закрыть')}>
              ×
            </button>
          )}
        </div>
      )}
    </div>
  );
}
