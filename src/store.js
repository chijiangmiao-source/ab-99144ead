'use strict';

/**
 * Crash-safe persistence for drills.
 *
 * Every accepted mutation is persisted before the caller is answered, so the
 * state recovered after a restart equals the state an uninterrupted run would
 * have had. Writes are atomic: serialize to a temporary file, fsync it, then
 * rename over the previous file and fsync the directory. A crash can leave a
 * stale .tmp file behind but never a half-written drill file.
 */

const fs = require('fs');
const path = require('path');
const { DrillError, CODES } = require('./drill');

const ID_PATTERN = /^[a-z0-9-]+$/;

class Store {
  constructor(dir) {
    this.dir = dir;
    fs.mkdirSync(dir, { recursive: true });
  }

  fileFor(id) {
    if (!ID_PATTERN.test(id)) {
      throw new DrillError(CODES.DRILL_NOT_FOUND, `no drill with id "${id}"`);
    }
    return path.join(this.dir, `${id}.json`);
  }

  save(drill) {
    const file = this.fileFor(drill.id);
    const tmp = `${file}.tmp`;
    const data = JSON.stringify(drill, null, 2);
    const fd = fs.openSync(tmp, 'w', 0o600);
    try {
      fs.writeFileSync(fd, data);
      fs.fsyncSync(fd);
    } finally {
      fs.closeSync(fd);
    }
    fs.renameSync(tmp, file);
    // Durability of the rename itself.
    const dirFd = fs.openSync(this.dir, 'r');
    try {
      fs.fsyncSync(dirFd);
    } finally {
      fs.closeSync(dirFd);
    }
  }

  load(id) {
    const file = this.fileFor(id);
    let raw;
    try {
      raw = fs.readFileSync(file, 'utf8');
    } catch (err) {
      if (err.code === 'ENOENT') {
        throw new DrillError(CODES.DRILL_NOT_FOUND, `no drill with id "${id}"`);
      }
      throw err;
    }
    return normalize(JSON.parse(raw));
  }

  exists(id) {
    try {
      fs.accessSync(this.fileFor(id));
      return true;
    } catch {
      return false;
    }
  }

  list() {
    return fs
      .readdirSync(this.dir)
      .filter((name) => name.endsWith('.json'))
      .sort()
      .map((name) => normalize(JSON.parse(fs.readFileSync(path.join(this.dir, name), 'utf8'))));
  }
}

/** Fill in any missing containers so older files remain loadable. */
function normalize(drill) {
  return {
    ...drill,
    protocols: drill.protocols || [],
    waiting: drill.waiting || {},
    adjudicated: drill.adjudicated || {},
    deliveries: drill.deliveries || {},
  };
}

module.exports = { Store };
