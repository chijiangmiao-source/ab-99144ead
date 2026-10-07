'use strict';

/**
 * Registry: in-memory cache of drills in front of the durable store.
 *
 * Every mutation runs on a deep copy of the drill; the copy is persisted
 * atomically and only then becomes the cached state. A failed mutation or a
 * failed persist therefore never corrupts the visible state, and whatever a
 * client was told had happened is guaranteed to survive a restart.
 */

const crypto = require('crypto');
const { createDrill } = require('./drill');

class Registry {
  constructor(store) {
    this.store = store;
    this.cache = new Map();
  }

  /** Create a drill with a fresh unique id. */
  create({ name, baseThreshold }) {
    const now = new Date().toISOString();
    let drill;
    do {
      const id = `drill-${crypto.randomBytes(4).toString('hex')}`;
      drill = createDrill({ id, name, baseThreshold, now });
    } while (this.store.exists(drill.id));
    this.store.save(drill);
    this.cache.set(drill.id, drill);
    return drill;
  }

  /** Load a drill (from cache or disk). Throws DRILL_NOT_FOUND. */
  get(id) {
    if (!this.cache.has(id)) {
      this.cache.set(id, this.store.load(id));
    }
    return this.cache.get(id);
  }

  /** All drills, freshest state first. */
  list() {
    const drills = this.store.list();
    for (const d of drills) this.cache.set(d.id, d);
    return drills;
  }

  /**
   * Apply fn to a working copy of the drill, persist it, then swap it in.
   * Returns fn's return value. Any thrown error leaves state untouched.
   */
  mutate(id, fn) {
    const current = this.get(id);
    const working = structuredClone(current);
    const result = fn(working);
    this.store.save(working);
    this.cache.set(id, working);
    return result;
  }
}

module.exports = { Registry };
