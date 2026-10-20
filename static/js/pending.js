// Deferred actions that can still be cancelled until they fire (undoable photo removal).

export class Pending {
  /** `ms` = grace period; timer functions are injectable for tests. */
  constructor(ms, { setTimer = setTimeout, clearTimer = clearTimeout } = {}) {
    this.ms = ms;
    // wrapped: browser timers throw "Illegal invocation" when called as methods of another object
    this.setTimer = (fn, delay) => setTimer(fn, delay);
    this.clearTimer = (t) => clearTimer(t);
    this.items = new Map();
  }

  /** Run `fire(value)` after the grace period unless `take(key)` cancels it first. */
  add(key, value, fire) {
    this.take(key);
    const t = this.setTimer(() => {
      this.items.delete(key);
      fire(value);
    }, this.ms);
    this.items.set(key, { value, fire, t });
  }

  /** Cancel a pending action and hand back its value (null when it already fired or never existed). */
  take(key) {
    const it = this.items.get(key);
    if (!it) return null;
    this.clearTimer(it.t);
    this.items.delete(key);
    return it.value;
  }

  has(key) {
    return this.items.has(key);
  }

  /** Fire everything now (the page is going away); `args` are passed on to each action. */
  flush(...args) {
    const all = [...this.items.values()];
    this.items.clear();
    for (const it of all) {
      this.clearTimer(it.t);
      it.fire(it.value, ...args);
    }
  }
}
