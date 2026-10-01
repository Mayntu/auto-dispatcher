export const SNAPSHOT_STEP = 5;
export class History {
  items = [];
  maxItems;
  constructor(hours) {
    this.maxItems = Math.ceil((hours * 3600) / SNAPSHOT_STEP);
  }
  setRetention(hours) {
    this.maxItems = Math.ceil((hours * 3600) / SNAPSHOT_STEP);
    this.trim();
  }
  get size() {
    return this.items.length;
  }
  get first() {
    return this.items[0];
  }
  shouldRecord(t) {
    const last = this.items[this.items.length - 1];
    return !last || t - last.t >= SNAPSHOT_STEP;
  }
  record(s) {
    this.items.push(s);
    this.trim();
  }
  clear() {
    this.items = [];
  }
  /** Ближайший снимок не позже t (бинарный поиск). */
  at(t) {
    let lo = 0;
    let hi = this.items.length - 1;
    let ans;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (this.items[mid].t <= t) {
        ans = this.items[mid];
        lo = mid + 1;
      } else hi = mid - 1;
    }
    return ans ?? this.items[0];
  }
  range(from, to) {
    return this.items.filter((s) => s.t >= from && s.t <= to);
  }
  trim() {
    if (this.items.length > this.maxItems) this.items.splice(0, this.items.length - this.maxItems);
  }
}
