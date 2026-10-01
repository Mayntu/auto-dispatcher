import { t as tr } from '../i18n';
import { m } from '../i18n/msg';
/**
 * Движок прототипа: эмулятор ДЦ (поле + телесигнализация), оркестратор автодиспетчеризации,
 * шина событий, журнал решений, маршруты, what-if и история.
 * Модули ядра (src/core) не знают о React и могут быть вынесены на backend без изменений.
 */
import { buildDisruption, describeDisruption, KIND_LABEL } from '../core/disruptions';
import { EventBus } from '../core/eventBus';
import { bestFor, buildBaseline, computeVariants, evaluatePlan, impactsOf, manualVariants, planWithStrategy, previewManual } from '../core/planner';
import { routeOf, runLimits, segOf } from '../core/scheduler';
import { destDelayMin } from '../core/qualityIndex';
import { currentPositions, locate } from '../core/positions';
import { CATEGORY_LABEL, weightedDelay } from '../core/qualityIndex';
import { computeRoutes } from '../core/routes';
import { durationOf, isBlocking } from '../core/scheduler';
import { initialTrains, SECTION, SIM_START } from '../core/section';
import { loadSettings, saveSettings } from '../core/settings';
import { fmtHM } from '../core/time';
import { authenticate, persistUser, restoreUser } from './auth';
import { History } from './history';
import { LIVE } from '../api/backend';
import { can, PERMISSIONS, ROLES } from './roles';
const LOG_CAP = 800;
/** Действия, которые можно передать на ведущий экран командой. */
export const COMMANDS = [
  'inject', 'massDisruptions', 'refine', 'resolve', 'applyVariant', 'askVariants', 'manualCommit', 'manualApply', 'removePin', 'cancelManual', 'setRunning', 'setSpeed', 'reset',
  'whatIfOpen', 'whatIfClose', 'whatIfSetMods', 'whatIfApply', 'updateSettings',
];
const emptyMods = () => ({ speed: {}, priority: {}, cancelled: {}, durationMin: {} });
let seq = 0;
const uid = (p) => `${p}${Date.now().toString(36)}${(++seq).toString(36)}`;
/** План, по которому сейчас фактически движутся поезда (прогноз, если решение ещё не принято). */
export const runningPlan = (s) => s.forecast ?? s.plan;
export const shownIndex = (s) => s.forecastIndex ?? s.index;
/** Сколько реальных секунд у диспетчера на выбор, когда срок решения подошёл (время модели в это время идёт ×1). */
export const DECISION_GRACE_SEC = 60;
/** Срок решения подошёл: модель замедлена до ×1 и идёт обратный отсчёт. */
export const decisionHold = (s) => s.pending?.holdSince !== undefined;
export const isActive = (d, t) => d.start <= t && t < d.start + durationOf(d, 'expected');
export class Engine {
  bus = new EventBus();
  history;
  state;
  listeners = new Set();
  timer = null;
  lastReal = 0;
  lastPause = 0;
  worker = null;
  reqSeq = 0;
  reqStarted = 0;
  constructor() {
    const settings = loadSettings();
    this.history = new History(settings.historyHours);
    this.state = LIVE ? this.liveInitialState(settings, restoreUser()) : this.initialState(settings, restoreUser());
    if (!LIVE) this.initWorker();
    this.bus.on('tick', (t) => this.recordSnapshot(t));
    this.bus.on('log', (e) => {
      const fn = e.level === 'crit' ? console.warn : console.info;
      fn(`[${fmtHM(e.t)}] [${tr(e.source)}] ${tr(e.text)}`);
    });
  }
  // ───────────── подписка (useSyncExternalStore) ─────────────
  subscribe = (fn) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };
  getState = () => this.state;
  set(patch) {
    this.state = { ...this.state, ...patch, historySize: this.history.size };
    this.listeners.forEach((l) => l());
  }
  /** С сервером: пустое состояние, всё остальное придёт потоком событий (engine/live.js). */
  liveInitialState(settings, user) {
    const empty = { id: 'empty', strategy: 'baseline', order: [], trains: {} };
    return {
      now: SIM_START,
      running: true,
      speed: 20,
      trains: [],
      disruptions: [],
      baseline: empty,
      plan: empty,
      conflicts: [],
      index: { value: 100, category: 'norm', factors: [] },
      forecastConflicts: [],
      log: [],
      decisions: [],
      pins: [],
      settings,
      metrics: { lastReplanMs: 0, lastVariantsMs: 0 },
      user,
      historySize: 0,
    };
  }
  initialState(settings, user) {
    const trains = initialTrains();
    const baseline = buildBaseline(SECTION, settings, trains);
    const { conflicts, index } = evaluatePlan({ section: SECTION, settings, trains, baseline }, baseline);
    return {
      now: SIM_START,
      running: true,
      speed: 20,
      trains,
      disruptions: [],
      baseline,
      plan: baseline,
      conflicts,
      index,
      forecastConflicts: [],
      log: [],
      decisions: [],
      pins: [],
      settings,
      metrics: { lastReplanMs: 0, lastVariantsMs: 0 },
      user,
      historySize: 0,
    };
  }
  initWorker() {
    if (typeof Worker === 'undefined') return;
    try {
      this.worker = new Worker(new URL('./planner.worker.js', import.meta.url), { type: 'module' });
      this.worker.onmessage = (e) => this.onVariants(e.data.reqId, e.data.result);
      this.worker.onerror = () => {
        this.log('warn', m('Web Worker недоступен — расчёт вариантов в основном потоке'), 'engine');
        this.worker = null;
      };
    } catch {
      this.worker = null;
    }
  }
  // ───────────── симулятор времени ─────────────
  start() {
    if (this.timer) return;
    this.lastReal = performance.now();
    this.timer = setInterval(() => this.tick(), 250);
  }
  stop() {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }
  tick() {
    const real = performance.now();
    const dt = Math.min(1, (real - this.lastReal) / 1000);
    this.lastReal = real;
    const s = this.state;
    if (!s.running) {
      // На паузе обратный отсчёт решения стоит.
      if (decisionHold(s)) this.set({ pending: { ...s.pending, holdSince: s.pending.holdSince + (real - this.lastPause) } });
      this.lastPause = real;
      return;
    }
    this.lastPause = real;
    // Пока диспетчер выбирает вариант на последних минутах — время модели не обгоняет человека.
    this.step(dt * (decisionHold(s) ? Math.min(1, s.speed) : s.speed));
  }
  /** Один шаг модельного времени: телесигнализация, маршруты, сроки решений, история. */
  step(sec) {
    const now = this.state.now + sec;
    this.set({ now });
    this.checkPendingDeadline();
    this.bus.emit('tick', now);
  }
  setRunning(running) {
    if (!this.allowed('scenario', 'управление временем')) return;
    this.set({ running });
  }
  setSpeed(speed) {
    if (!this.allowed('scenario', 'управление временем')) return;
    this.set({ speed, running: true });
  }
  reset() {
    if (!this.allowed('scenario', 'сброс участка')) return;
    this.history.clear();
    this.reqSeq++;
    this.state = this.initialState(this.state.settings, this.state.user);
    this.log('info', m('Участок сброшен: штатное движение по нормативному графику'), 'sim');
  }
  // ───────────── доступ ─────────────
  login(login, password) {
    const user = authenticate(login, password);
    if (!user) return false;
    persistUser(user);
    this.set({ user });
    this.log('info', m('Вход: {who}', { who: this.actor() }), 'Доступ');
    return true;
  }
  logout() {
    persistUser(null);
    this.set({ user: null, whatIf: undefined });
  }
  /** Выполнить команду от имени пользователя другого экрана (инструктор, диспетчер в соседнем окне). */
  runAs(user, name, args) {
    if (!COMMANDS.includes(name)) return;
    this.actingUser = user;
    try {
      this[name](...args);
    } finally {
      this.actingUser = null;
    }
  }
  /** Подменить состояние снимком с ведущего экрана, сохранив своего пользователя. */
  replaceState(state) {
    this.state = { ...state, user: this.state.user };
    this.lastReal = performance.now();
    this.listeners.forEach((l) => l());
  }
  /** Проверка прав роли. Действия самой системы (автоприменение, просроченные маршруты) идут в обход. */
  allowed(permission, action) {
    if (can(this.actingUser ?? this.state.user, permission)) return true;
    this.log('warn', m('Нет права «{perm}»: {action}', { perm: m(PERMISSIONS[permission]), action: m(action) }), 'Доступ');
    return false;
  }
  /** Кто выполнил действие — для журнала решений. */
  actor() {
    const u = this.actingUser ?? this.state.user;
    return u ? m('{role} {login}', { role: m(ROLES[u.role].name), login: u.login }) : '—';
  }
  // ───────────── журнал ─────────────
  log(level, text, source) {
    const e = { id: uid('E'), t: this.state.now, level, text, source };
    const log = [e, ...this.state.log].slice(0, LOG_CAP);
    this.set({ log });
    this.bus.emit('log', e);
  }
  input(over = {}) {
    const s = this.state;
    return {
      section: SECTION,
      settings: s.settings,
      trains: s.trains,
      disruptions: s.disruptions,
      now: s.now,
      prev: runningPlan(s),
      baseline: s.baseline,
      pins: (s.pins ?? []).filter((p) => p.status !== 'removed'),
      ...over,
    };
  }
  evaluate(plan, trains = this.state.trains, disruptions = this.state.disruptions) {
    return evaluatePlan(
      { section: SECTION, settings: this.state.settings, trains, disruptions, baseline: this.state.baseline },
      plan,
    );
  }
  // ───────────── сбои ─────────────
  inject(specs) {
    if (!this.allowed('scenario', 'создание события')) return;
    const s = this.state;
    const running = runningPlan(s);
    const created = specs.map((sp) => buildDisruption(SECTION, s.trains, running, s.now, sp));
    const disruptions = [...s.disruptions, ...created];
    this.set({ disruptions, metrics: { ...s.metrics } });
    for (const d of created) {
      this.log('crit', describeDisruption(d, s.trains), 'ДЦ');
      this.bus.emit('disruption.created', d);
    }
    this.forecastAndRequest(created.map((d) => d.id));
  }
  /** Прогноз «без вмешательства» + запрос вариантов у планировщика. */
  forecastAndRequest(newIds) {
    const s = this.state;
    const input = this.input();
    const naive = planWithStrategy(input, 'naive', s.settings.planDuration);
    const { conflicts, index } = this.evaluate(naive);
    const first = conflicts.find((c) => c.time >= s.now)?.time;
    this.set({
      forecast: naive,
      forecastConflicts: conflicts,
      forecastIndex: index,
      pending: {
        id: uid('R'),
        createdAt: s.now,
        status: 'computing',
        variants: [],
        disruptionIds: [...new Set([...(s.pending?.disruptionIds ?? []), ...newIds])],
        firstConflictAt: first,
        holdSince: s.pending?.holdSince,
      },
    });
    this.log(
      conflicts.length ? 'warn' : 'info',
      m('Прогноз без вмешательства: индекс {i} ({cat}), конфликтов впереди: {n}{first}', {
        i: index.value,
        cat: m(CATEGORY_LABEL[index.category]),
        n: conflicts.length,
        first: first !== undefined ? m(', первый в {t}', { t: fmtHM(first) }) : '',
      }),
      'CDR',
    );
    this.requestVariants(input);
  }
  requestVariants(input) {
    const reqId = ++this.reqSeq;
    this.reqStarted = performance.now();
    if (this.worker) this.worker.postMessage({ reqId, input });
    else {
      setTimeout(() => this.onVariants(reqId, computeVariants(input)), 0);
    }
  }
  onVariants(reqId, result) {
    if (reqId !== this.reqSeq || !this.state.pending) return;
    const total = performance.now() - this.reqStarted;
    const pending = { ...this.state.pending, status: 'ready', variants: result.variants, computeMs: total };
    this.set({ pending, metrics: { ...this.state.metrics, lastVariantsMs: total } });
    const best = bestFor(result.variants, 'optimal');
    this.log(
      'ok',
      m('Готово вариантов: {n} за {ms} мс. Оптимально — «{title}» (индекс {i})', { n: result.variants.length, ms: Math.round(total), title: m(best?.title), i: best?.index.value }),
      'Планировщик',
    );
    this.bus.emit('variants.ready', pending);
  }
  applyVariant(variantId, auto = false) {
    const s = this.state;
    const v = s.pending?.variants.find((x) => x.id === variantId);
    if (!v || !s.pending) return;
    if (!auto && !this.allowed('section', 'утверждение плана')) return;
    const t0 = performance.now();
    let plan = v.plan;
    // Указание диспетчера из ручного изменения вступает в силу вместе с планом.
    const pins = v.pin ? [...(s.pins ?? []).filter((p) => !(p.trainId === v.pin.trainId && p.station === v.pin.station && p.kind === v.pin.kind)), { ...v.pin, status: 'active', createdAt: s.now }] : s.pins ?? [];
    if (s.now - s.pending.createdAt > 20) {
      // Обстановка ушла вперёд — пересчитываем ту же стратегию от текущего момента.
      const mode = v.strategy === 'robust' ? 'max' : s.settings.planDuration;
      plan = planWithStrategy(this.input({ pins }), v.strategy, mode, v.plan.order);
    }
    const { conflicts, index } = this.evaluate(plan);
    const decision = {
      id: uid('S'),
      t: s.now,
      title: v.title,
      strategy: v.strategy,
      indexBefore: shownIndex(s).value,
      indexAfter: index.value,
      weightedDelayMin: weightedDelay(plan, s.baseline, s.trains, s.settings),
      by: auto ? 'система (истёк срок решения)' : this.actor(),
      auto,
    };
    this.set({
      plan,
      conflicts,
      index,
      forecast: undefined,
      forecastConflicts: [],
      forecastIndex: undefined,
      pending: undefined,
      pins,
      decisions: [decision, ...s.decisions],
      metrics: { ...s.metrics, lastReplanMs: performance.now() - t0 },
    });
    if (v.pin) this.toast(m('Указание: {d}', { d: v.pin.description }));
    this.checkPins();
    this.log(
      auto ? 'crit' : 'ok',
      m(auto ? 'Автоприменён безопасный вариант «{title}»: индекс {a} → {b}' : 'Утверждён вариант «{title}»: индекс {a} → {b}', { title: m(v.title), a: decision.indexBefore, b: index.value }),
      auto ? 'Система' : this.actor(),
    );
    this.bus.emit('plan.applied', decision);
  }
  checkPendingDeadline() {
    const s = this.state;
    const p = s.pending;
    if (!p || p.firstConflictAt === undefined) return;
    if (p.firstConflictAt - s.now > s.settings.autoApplyLeadSec) return;
    // Срок подошёл: сначала даём диспетчеру реальную минуту, и только потом решает система.
    if (p.holdSince === undefined) {
      this.set({ pending: { ...p, holdSince: Date.now() } });
      return;
    }
    if (p.status !== 'ready') return;
    if (Date.now() - p.holdSince < DECISION_GRACE_SEC * 1000 && s.now < p.firstConflictAt) return;
    const safe = p.variants.find((v) => v.strategy === 'hold') ?? p.variants[0];
    if (safe) this.applyVariant(safe.id, true);
  }
  refine(id, minMin, maxMin) {
    if (!this.allowed('scenario', 'уточнение длительности сбоя')) return;
    const lo = Math.max(1, Math.min(minMin, maxMin));
    const hi = Math.max(lo, maxMin);
    const disruptions = this.state.disruptions.map((d) =>
      d.id === id ? { ...d, durMin: lo * 60, durMax: hi * 60, durExpected: Math.round(((lo + hi) / 2) * 60) } : d,
    );
    this.set({ disruptions });
    this.log('info', m('Уточнена длительность: {lo}–{hi} мин', { lo, hi }), 'ДЦ');
    this.afterDisruptionChange();
  }
  resolve(id) {
    if (!this.allowed('scenario', 'отметка об устранении')) return;
    const disruptions = this.state.disruptions.map((d) => (d.id === id ? { ...d, resolvedAt: this.state.now } : d));
    this.set({ disruptions });
    const d = disruptions.find((x) => x.id === id);
    this.log('ok', m('Устранено: {kind}', { kind: m(KIND_LABEL[d.kind]) }), 'ДЦ');
    this.afterDisruptionChange();
  }
  /** Диспетчер просит варианты без нового события — например, когда в действующем плане остались конфликты. */
  askVariants() {
    if (!this.allowed('section', 'запрос вариантов')) return;
    if (this.state.pending) return;
    this.log('info', m('Запрошены варианты: в плане конфликтов — {n}', { n: this.state.conflicts.length }), this.actor());
    this.forecastAndRequest([]);
  }
  afterDisruptionChange() {
    if (this.state.pending) this.forecastAndRequest([]);
    else this.replanActive('уточнение обстановки');
  }
  /** Перепланирование со скользящим горизонтом по стратегии действующего плана. */
  replanActive(reason) {
    const s = this.state;
    const t0 = performance.now();
    const strategy = ['hold', 'robust', 'passengers', 'keep_order'].includes(s.plan.strategy) ? s.plan.strategy : 'optimized';
    const mode = strategy === 'robust' ? 'max' : s.settings.planDuration;
    const plan = planWithStrategy(this.input({ prev: s.plan }), strategy, mode, s.plan.order);
    const { conflicts, index } = this.evaluate(plan);
    const ms = performance.now() - t0;
    this.set({ plan, conflicts, index, metrics: { ...s.metrics, lastReplanMs: ms } });
    this.log('info', m('План пересчитан ({reason}) за {ms} мс, индекс {i}', { reason: m(reason), ms: Math.round(ms), i: index.value }), 'Планировщик');
    this.checkPins();
  }
  // ───────────── указания диспетчера: ручное изменение времени на ГИД ─────────────
  /** Короткое уведомление для экрана (у интерфейса — тост). */
  toast(text, kind = 'info', extra = {}) {
    this.set({ toast: { id: uid('T'), text, kind, ...extra } });
  }
  /** Точка нитки в действующем плане: номер в маршруте, сама остановка, соседние. */
  pointOf(trainId, station) {
    const s = this.state;
    const plan = runningPlan(s);
    const tp = plan.trains[trainId];
    const train = s.trains.find((t) => t.id === trainId);
    if (!tp || !train) return null;
    const j = tp.stops.findIndex((x) => x.station === station);
    if (j < 0) return null;
    return { s, plan, tp, train, j, st: tp.stops[j], prev: tp.stops[j - 1], last: j === tp.stops.length - 1 };
  }
  /**
   * Границы перетаскивания точки (§4, §6.1 ТЗ на ручное изменение): отправление — не раньше расписания,
   * минимальной стоянки и «сейчас»; прибытие — не быстрее минимального и не медленнее 1,3 обычного хода.
   */
  manualBounds(trainId, station) {
    const p = this.pointOf(trainId, station);
    if (!p) return null;
    const { s, train, j, st, prev, last } = p;
    const ctx = { section: SECTION, settings: s.settings, disruptions: s.disruptions, pins: [], durMode: s.settings.planDuration };
    const route = routeOf(SECTION, train);
    const planned = j === 0 || last || train.stops.includes(station);
    const minDwell = j === 0 || last ? 0 : planned ? train.dwell : 0;
    const passenger = train.category !== 'freight' && train.category !== 'freightFast';
    const sched = s.baseline.trains[trainId]?.stops[j];
    const pin = (s.pins ?? []).find((x) => x.trainId === trainId && x.station === station && x.status !== 'removed') ?? null;
    const reason = (code, text) => ({ code, text });
    const horizon = s.now + 3 * 3600;
    const pointType = j === 0 ? 'origin' : last ? 'destination' : st.dep - st.arr <= 60 && !planned ? 'pass' : 'stop';
    let dep = null;
    if (!last) {
      const cands = [
        [s.now, reason('now', m('Нельзя раньше текущего времени'))],
        [j === 0 ? train.departure : st.arr + minDwell, j === 0 ? reason('schedule', m('Не раньше отправления по расписанию')) : reason('min_dwell', m('Минимальная стоянка {n} мин', { n: Math.round(minDwell / 60) }))],
      ];
      if (passenger && planned && sched) cands.push([sched.dep, reason('schedule', m('Не раньше отправления по расписанию'))]);
      const [min, minReason] = cands.reduce((a, b) => (b[0] > a[0] ? b : a));
      dep = { current: st.dep, min: Math.min(min, st.dep), max: Math.max(horizon, st.dep), minReason, maxReason: reason('horizon', m('Дальше горизонта планирования')), locked: st.dep < s.now, lockedReason: st.dep < s.now ? m('Поезд уже отправился') : null };
    }
    let arr = null;
    let run = null;
    if (j > 0) {
      const seg = segOf(route, j - 1);
      const lim = runLimits(ctx, train, seg, prev.dep);
      const g = SECTION.segments[seg];
      run = { ...lim, lengthKm: g.lengthKm, vmax: Math.min(g.vmax, train.vmaxOverride ?? Infinity) };
      const minA = Math.max(s.now, prev.dep + lim.min);
      const minReason = prev.dep + lim.min >= s.now
        ? reason('min_run_time', m('Быстрее нельзя: минимальное время хода {n} мин (средняя {v} км/ч)', { n: Math.round(lim.min / 60), v: Math.round((g.lengthKm / lim.min) * 3600) }))
        : reason('now', m('Нельзя раньше текущего времени'));
      arr = { current: st.arr, min: Math.min(minA, st.arr), max: Math.max(prev.dep + lim.max, st.arr), minReason, maxReason: reason('max_run_time', m('Медленнее нельзя: задержите отправление с предыдущего пункта')), locked: st.arr < s.now, lockedReason: st.arr < s.now ? m('Поезд уже прибыл') : null };
    }
    return { pointType, arr, dep, info: { minDwell, prevDep: prev?.dep ?? null, prevStation: prev?.station ?? null, run, schedArr: sched?.arr ?? null, schedDep: sched?.dep ?? null, pin } };
  }
  /** Указание из запроса: прибытие раньше — отправление остаётся на месте (стоянка растёт). */
  pinFrom(req) {
    const p = this.pointOf(req.trainId, req.station);
    const name = SECTION.stations[req.station]?.short ?? '';
    const pin = { id: uid('PIN'), trainId: req.trainId, station: req.station, kind: req.kind, time: req.time, status: 'active' };
    if (req.kind === 'arr' && p && req.time < p.st.arr && !p.last) pin.holdDep = p.st.dep;
    pin.description = m(req.kind === 'dep' ? 'Задержать {n} отправлением со станции {st} до {t}' : 'Поезд {n}: прибытие на {st} в {t}', { n: p?.train.number ?? req.trainId, st: m(name), t: fmtHM(req.time) });
    return pin;
  }
  withPin(pin) {
    const others = (this.state.pins ?? []).filter((x) => x.status !== 'removed' && !(x.trainId === pin.trainId && x.station === pin.station && x.kind === pin.kind));
    return [...others, pin];
  }
  /** Прогноз на каждый шаг перетаскивания: что станет с этим и остальными поездами при прежнем порядке. */
  manualPreview(req) {
    const s = this.state;
    const t0 = performance.now();
    const pin = this.pinFrom(req);
    const prev = runningPlan(s);
    const { plan, conflicts, index } = previewManual(this.input({ pins: this.withPin(pin) }));
    const changed = [];
    for (const t of s.trains) {
      const a = prev.trains[t.id]?.stops;
      const b = plan.trains[t.id]?.stops;
      if (!a || !b) continue;
      let maxD = 0;
      b.forEach((x, i) => (maxD = Math.max(maxD, Math.abs(x.arr - (a[i]?.arr ?? x.arr)), Math.abs(x.dep - (a[i]?.dep ?? x.dep)))));
      if (maxD >= 30) changed.push({ trainId: t.id, number: t.number, deltaFinal: b.at(-1).arr - a.at(-1).arr, deltaMax: maxD });
    }
    const p = plan.trains[req.trainId]?.stops.find((x) => x.station === req.station);
    const j = plan.trains[req.trainId]?.stops.findIndex((x) => x.station === req.station) ?? -1;
    const pr = j > 0 ? plan.trains[req.trainId].stops[j - 1] : null;
    const totalDelay = (pl) => s.trains.reduce((sum, t) => sum + (t.cancelled ? 0 : Math.max(0, destDelayMin(pl, s.baseline, t.id))), 0);
    return {
      time: req.kind === 'dep' ? p?.dep : p?.arr,
      dragged: p ? { arr: p.arr, dep: p.dep, dwell: p.dep - p.arr, prevRun: pr ? p.arr - pr.dep : null } : null,
      plan,
      changed: changed.sort((a, b) => b.deltaMax - a.deltaMax),
      conflicts,
      index,
      deltaIndex: index.value - shownIndex(s).value,
      totalDelayDelta: totalDelay(plan) - totalDelay(prev),
      ms: performance.now() - t0,
    };
  }
  /** Отпустили точку: варианты «Сохранить порядок» / «Переразвести» — диспетчер выбирает, как при сбое. */
  manualCommit(req) {
    if (!this.allowed('section', 'ручное изменение времени')) return;
    const s = this.state;
    const pin = this.pinFrom(req);
    const p = this.pointOf(req.trainId, req.station);
    const { variants, computeMs } = manualVariants(this.input({ pins: this.withPin(pin) }));
    for (const v of variants) v.pin = pin;
    this.set({
      pending: {
        id: uid('R'),
        source: 'manual',
        manual: { ...req, from: p ? (req.kind === 'dep' ? p.st.dep : p.st.arr) : null, description: pin.description },
        createdAt: s.now,
        status: 'ready',
        variants,
        disruptionIds: [],
      },
      metrics: { ...s.metrics, lastVariantsMs: computeMs },
    });
    this.log('info', m('Ручное изменение: {d} — вариантов {n}', { d: pin.description, n: variants.length }), this.actor());
  }
  /**
   * Ручное изменение сразу в работу: указание закрепляется, остальные поезда подстраиваются по времени
   * при прежнем порядке. Отменить — снять указание (кнопка в уведомлении или на замке).
   */
  manualApply(req) {
    if (!this.allowed('section', 'ручное изменение времени')) return;
    const s = this.state;
    const t0 = performance.now();
    const pin = { ...this.pinFrom(req), createdAt: s.now };
    const pins = this.withPin(pin);
    const plan = planWithStrategy(this.input({ pins, prev: s.plan }), 'keep_order', s.settings.planDuration);
    const { conflicts, index } = this.evaluate(plan);
    const decision = {
      id: uid('S'),
      t: s.now,
      title: pin.description,
      strategy: 'manual',
      indexBefore: shownIndex(s).value,
      indexAfter: index.value,
      weightedDelayMin: weightedDelay(plan, s.baseline, s.trains, s.settings),
      by: this.actor(),
      auto: false,
    };
    this.set({ plan, conflicts, index, pins, decisions: [decision, ...s.decisions], metrics: { ...s.metrics, lastReplanMs: performance.now() - t0 } });
    // Если ждали решения по сбою — варианты пересчитываются уже с учётом указания.
    if (s.pending && s.pending.source !== 'manual') this.forecastAndRequest([]);
    this.log('ok', m('Указание: {d}, индекс {a} → {b}', { d: pin.description, a: decision.indexBefore, b: index.value }), this.actor());
    this.toast(m('Указание: {d}', { d: pin.description }), 'info', { undoPinId: pin.id });
    this.checkPins();
  }
  /** Отказ от ручного изменения: варианты убираются, план прежний. */
  cancelManual() {
    if (this.state.pending?.source === 'manual') this.set({ pending: undefined });
  }
  removePin(id) {
    if (!this.allowed('section', 'снятие указания')) return;
    const pin = (this.state.pins ?? []).find((p) => p.id === id);
    if (!pin) return;
    this.set({ pins: this.state.pins.filter((p) => p.id !== id) });
    this.log('info', m('Указание снято: {d}', { d: pin.description }), this.actor());
    this.toast(m('Указание по {n} снято', { n: pin.trainId }));
    this.replanActive('снятие указания');
  }
  /** Указание, которое план больше не выполняет (больше минуты расхождения), — красный замок и уведомление. */
  checkPins() {
    const s = this.state;
    const plan = runningPlan(s);
    let changed = false;
    const pins = (s.pins ?? []).map((p) => {
      const st = plan.trains[p.trainId]?.stops.find((x) => x.station === p.station);
      if (!st) return p;
      const t = p.kind === 'dep' ? st.dep : st.arr;
      const bad = p.kind === 'dep' ? t > p.time + 60 : Math.abs(t - p.time) > 60;
      const done = (p.kind === 'dep' ? st.dep : st.arr) < s.now - 60;
      const status = done ? 'done' : bad ? 'violated' : 'active';
      if (status === p.status) return p;
      changed = true;
      if (status === 'violated') {
        const reason = m('план даёт {t} вместо {p}', { t: fmtHM(t), p: fmtHM(p.time) });
        this.log('crit', m('Указание по {n} невыполнимо: {r}', { n: p.trainId, r: reason }), 'Планировщик');
        this.toast(m('Указание по {n} невыполнимо: {r}', { n: p.trainId, r: reason }), 'crit', { pinId: p.id, trainId: p.trainId });
        return { ...p, status, reason };
      }
      return { ...p, status };
    });
    if (changed) this.set({ pins });
  }
  // ───────────── сценарии демо ─────────────
  /** Вброс n нештатных ситуаций одновременно. */
  massDisruptions(n = 8) {
    if (!this.allowed('scenario', 'вброс событий')) return;
    const s = this.state;
    const plan = runningPlan(s);
    const onLine = s.trains.filter((t) => !t.cancelled && locate(plan, t.id, s.now).state !== 'done');
    const kinds = [
      'livestock',
      'signal',
      'breakdown',
      'delay',
      'closure',
      'breakdown',
      'delay',
      'signal',
      'livestock',
      'delay',
    ];
    const specs = [];
    const rnd = (a, b) => a + Math.floor(Math.random() * (b - a + 1));
    const usedSeg = new Set();
    const usedTrain = new Set();
    for (let i = 0; i < n; i++) {
      const kind = kinds[i % kinds.length];
      const lo = rnd(5, 15);
      const hi = lo + rnd(5, 20);
      if (kind === 'breakdown' || kind === 'delay') {
        const t = onLine.find((x) => !usedTrain.has(x.id)) ?? onLine[rnd(0, Math.max(0, onLine.length - 1))];
        if (!t) continue;
        usedTrain.add(t.id);
        specs.push({ kind, trainId: t.id, minMin: lo, maxMin: hi });
      } else {
        let seg = rnd(0, SECTION.segments.length - 1);
        for (let g = 0; g < 5 && usedSeg.has(seg); g++) seg = rnd(0, SECTION.segments.length - 1);
        usedSeg.add(seg);
        specs.push({ kind, segment: seg, minMin: lo, maxMin: hi });
      }
    }
    this.log('warn', m('Массовый вброс: {n} нештатных ситуаций', { n: specs.length }), 'sim');
    this.inject(specs);
  }
  // ───────────── what-if ─────────────
  whatIfOpen() {
    if (!this.allowed('section', 'what-if моделирование')) return;
    this.set({ whatIf: { openedAt: this.state.now, mods: emptyMods() } });
    this.whatIfRun();
  }
  whatIfClose() {
    this.set({ whatIf: undefined });
  }
  whatIfSetMods(mods) {
    if (!this.state.whatIf || !this.allowed('section', 'what-if моделирование')) return;
    this.set({ whatIf: { ...this.state.whatIf, mods } });
    this.whatIfRun();
  }
  sandbox(mods) {
    const s = this.state;
    const plan = runningPlan(s);
    const trains = s.trains.map((t) => {
      const notDeparted = locate(plan, t.id, s.now).state === 'before';
      return {
        ...t,
        vmaxOverride: mods.speed[t.id] >= 10 ? mods.speed[t.id] : t.vmaxOverride,
        priorityOverride: mods.priority[t.id] ?? t.priorityOverride,
        cancelled: (notDeparted && mods.cancelled[t.id]) || t.cancelled,
      };
    });
    const disruptions = s.disruptions.map((d) => {
      const m = mods.durationMin[d.id];
      if (m === undefined) return d;
      const sec = m * 60;
      return { ...d, durExpected: sec, durMin: Math.min(d.durMin, sec), durMax: Math.max(d.durMax, sec) };
    });
    return { trains, disruptions };
  }
  whatIfRun() {
    const s = this.state;
    if (!s.whatIf) return;
    const t0 = performance.now();
    const { trains, disruptions } = this.sandbox(s.whatIf.mods);
    const running = runningPlan(s);
    const plan = planWithStrategy(
      this.input({ trains, disruptions, prev: running }),
      'whatif',
      s.settings.planDuration,
      running.order,
    );
    const { conflicts, index } = this.evaluate(plan, trains, disruptions);
    const affected = impactsOf(running, plan, s.baseline, trains).filter(
      (x) => Math.abs(x.deltaMin) >= 1 || !running.trains[x.trainId],
    );
    this.set({
      whatIf: {
        ...s.whatIf,
        result: { plan, trains, index, conflicts: conflicts.length, affected, computeMs: performance.now() - t0 },
      },
    });
  }
  whatIfApply() {
    const s = this.state;
    const r = s.whatIf?.result;
    if (!s.whatIf || !r || !this.allowed('section', 'перенос what-if в работу')) return;
    const { trains, disruptions } = this.sandbox(s.whatIf.mods);
    const t0 = performance.now();
    const plan = planWithStrategy(
      this.input({ trains, disruptions, prev: runningPlan(s) }),
      'optimized',
      s.settings.planDuration,
      r.plan.order,
    );
    const ev = this.evaluate(plan, trains, disruptions);
    const decision = {
      id: uid('S'),
      t: s.now,
      title: 'What-if перенесён в работу',
      strategy: 'whatif',
      indexBefore: shownIndex(s).value,
      indexAfter: ev.index.value,
      weightedDelayMin: weightedDelay(plan, s.baseline, trains, s.settings),
      by: this.actor(),
      auto: false,
    };
    this.set({
      trains,
      disruptions,
      plan,
      conflicts: ev.conflicts,
      index: ev.index,
      forecast: undefined,
      forecastConflicts: [],
      forecastIndex: undefined,
      pending: undefined,
      whatIf: undefined,
      decisions: [decision, ...s.decisions],
      metrics: { ...s.metrics, lastReplanMs: performance.now() - t0 },
    });
    this.log(
      'ok',
      m('Результат what-if перенесён в работу: индекс {a} → {b}', { a: decision.indexBefore, b: ev.index.value }),
      this.actor(),
    );
  }
  // ───────────── маршруты ДЦ ─────────────
  routes() {
    const s = this.state;
    return computeRoutes(s.trains, runningPlan(s), s.now);
  }
  // ───────────── настройки ─────────────
  updateSettings(settings) {
    if (!this.allowed('settings', 'изменение настроек индекса и оптимизации')) return;
    saveSettings(settings);
    this.history.setRetention(settings.historyHours);
    const s = this.state;
    this.set({ settings });
    const ev = this.evaluate(s.plan);
    const fc = s.forecast ? this.evaluate(s.forecast) : undefined;
    this.set({ index: ev.index, conflicts: ev.conflicts, forecastIndex: fc?.index });
    this.log('info', m('Настройки индекса и оптимизации обновлены'), this.actor());
  }
  // ───────────── история ─────────────
  recordSnapshot(t) {
    if (!this.history.shouldRecord(t)) return;
    const s = this.state;
    const idx = shownIndex(s);
    const active = s.disruptions.filter((d) => isActive(d, t));
    const snap = {
      t,
      index: idx.value,
      category: idx.category,
      positions: currentPositions(SECTION, s, runningPlan(s), t),
      blocks: active
        .filter(isBlocking)
        .map(({ id, kind, segment, track, posStart, posEnd }) => ({ id, kind, segment, track, posStart, posEnd })),
      limited: active.filter((d) => d.kind === 'signal').map((d) => d.segment),
      conflicts: (s.forecast ? s.forecastConflicts : s.conflicts).length,
      disruptions: active.length,
    };
    this.history.record(snap);
  }
}
