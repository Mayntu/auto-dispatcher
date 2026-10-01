/**
 * Синхронизация экранов (диспетчер, инструктор, администратор) в соседних вкладках и окнах одного компьютера.
 * Один экран ведущий — на нём идёт симуляция; остальные зеркалят его состояние и отправляют действия командами.
 * Если ведущий экран закрыли, ведущим становится один из оставшихся.
 */
import { COMMANDS } from './engine';

const CHANNEL = 'autodispatcher-sync';
const PUBLISH_EVERY = 200;

export function startSync(engine) {
  if (typeof BroadcastChannel === 'undefined') {
    engine.start();
    return;
  }
  const ch = new BroadcastChannel(CHANNEL);
  const id = Math.random();
  let role = 'pending';
  let lastState = 0;
  let timer = null;

  const publish = () => {
    timer = null;
    ch.postMessage({ type: 'state', from: id, state: engine.getState() });
  };
  const schedule = () => {
    if (role === 'primary' && !timer) timer = setTimeout(publish, PUBLISH_EVERY);
  };
  const becomePrimary = () => {
    role = 'primary';
    engine.start();
    publish();
  };
  const becomeMirror = () => {
    role = 'mirror';
    engine.stop();
  };

  // На зеркале действия не выполняются локально, а уходят на ведущий экран.
  for (const name of COMMANDS) {
    const local = engine[name].bind(engine);
    engine[name] = (...args) => {
      if (role === 'mirror') ch.postMessage({ type: 'cmd', name, args, user: engine.getState().user });
      else local(...args);
    };
  }

  engine.subscribe(schedule);

  ch.onmessage = ({ data }) => {
    if (data.type === 'state') {
      lastState = performance.now();
      // Два ведущих (одновременный старт) — уступает тот, у кого id больше.
      if (role === 'primary' && data.from < id) becomeMirror();
      if (role === 'pending') becomeMirror();
      if (role === 'mirror') engine.replaceState(data.state);
    } else if (data.type === 'hello' && role === 'primary') {
      publish();
    } else if (data.type === 'cmd' && role === 'primary') {
      engine.runAs(data.user, data.name, data.args);
    } else if (data.type === 'bye' && role === 'mirror') {
      setTimeout(() => {
        if (role === 'mirror' && performance.now() - lastState > 500) becomePrimary();
      }, 150 + Math.random() * 400);
    }
  };

  ch.postMessage({ type: 'hello' });
  setTimeout(() => role === 'pending' && becomePrimary(), 500);
  window.addEventListener('beforeunload', () => role === 'primary' && ch.postMessage({ type: 'bye' }));
}
