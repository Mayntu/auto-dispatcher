/** Типизированная шина событий: модули обмениваются событиями, не зная друг о друге. */
export class EventBus {
  handlers = new Map();
  on(type, fn) {
    const set = this.handlers.get(type) ?? new Set();
    set.add(fn);
    this.handlers.set(type, set);
    return () => set.delete(fn);
  }
  emit(type, payload) {
    this.handlers.get(type)?.forEach((fn) => fn(payload));
  }
}
