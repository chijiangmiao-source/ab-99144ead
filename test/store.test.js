'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { Store } = require('../src/store');
const { createDrill, registerProtocol, submit } = require('../src/drill');

const T0 = Date.parse('2026-10-07T00:00:00.000Z');
let tick = 0;
function now() {
  tick += 1;
  return new Date(T0 + tick * 1000).toISOString();
}

function tempDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'beamguard-store-'));
}

/**
 * The scripted scenario: register a protocol switch, submit 2 ahead of the
 * gap, then backfill 0 and 1 so 2 drains under the new protocol, then keep
 * going past another gap.
 */
function scenario() {
  return [
    (d) => registerProtocol(d, { effectiveSeq: 2, threshold: 50, now: now() }),
    (d) => submit(d, { deliveryId: 'd2', seq: 2, reading: 75, now: now() }),
    (d) => submit(d, { deliveryId: 'd0', seq: 0, reading: 10, now: now() }),
    (d) => submit(d, { deliveryId: 'd1', seq: 1, reading: 20, now: now() }),
    (d) => submit(d, { deliveryId: 'd2', seq: 2, reading: 75, now: now() }), // replay echo
    (d) => submit(d, { deliveryId: 'd4', seq: 4, reading: 5, now: now() }),
    (d) => submit(d, { deliveryId: 'd3', seq: 3, reading: 5, now: now() }),
  ];
}

test('save/load round-trips the full drill state', () => {
  const store = new Store(tempDir());
  const d = createDrill({ id: 'drill-rt', name: 'round trip', baseThreshold: 100, now: now() });
  registerProtocol(d, { effectiveSeq: 2, threshold: 50, now: now() });
  submit(d, { deliveryId: 'd2', seq: 2, reading: 75, now: now() });
  submit(d, { deliveryId: 'd0', seq: 0, reading: 10, now: now() });
  store.save(d);

  const loaded = store.load('drill-rt');
  assert.deepEqual(loaded, d);
});

test('recovery after a crash at any step matches the uninterrupted run', () => {
  const ops = scenario();

  // Uninterrupted reference run, purely in memory.
  tick = 0;
  const reference = createDrill({ id: 'drill-ref', name: 'ref', baseThreshold: 100, now: now() });
  for (const op of ops) op(reference);

  // Crash-prone run: reload the drill from disk before every single step,
  // which is exactly what a restart between any two steps would see.
  tick = 0;
  const dir = tempDir();
  const store = new Store(dir);
  let d = createDrill({ id: 'drill-ref', name: 'ref', baseThreshold: 100, now: now() });
  store.save(d);
  for (const op of ops) {
    d = store.load('drill-ref'); // process restart
    op(d);
    store.save(d);
  }
  assert.deepEqual(d, reference);
});

test('crash right after a waiting-queue write keeps the record parked', () => {
  const dir = tempDir();
  const store = new Store(dir);
  const d = createDrill({ id: 'drill-c', name: 'c', baseThreshold: 100, now: now() });
  submit(d, { deliveryId: 'd2', seq: 2, reading: 75, now: now() });
  store.save(d); // accepted: waiting write is durable
  // --- process dies here, before any adjudication ---
  const recovered = new Store(dir).load('drill-c');
  assert.equal(recovered.waterLevel, 0);
  assert.deepEqual(Object.keys(recovered.waiting), ['2']);
  assert.equal(recovered.waiting[2].reading, 75);
});

test('crash right after a water-level advance keeps the immutable adjudications', () => {
  const dir = tempDir();
  const store = new Store(dir);
  const d = createDrill({ id: 'drill-w', name: 'w', baseThreshold: 100, now: now() });
  registerProtocol(d, { effectiveSeq: 2, threshold: 50, now: now() });
  submit(d, { deliveryId: 'd2', seq: 2, reading: 75, now: now() });
  submit(d, { deliveryId: 'd0', seq: 0, reading: 10, now: now() });
  submit(d, { deliveryId: 'd1', seq: 1, reading: 20, now: now() }); // drains 1 and 2
  store.save(d); // accepted: water level 3 is durable
  // --- process dies here ---
  const recovered = new Store(dir).load('drill-w');
  assert.equal(recovered.waterLevel, 3);
  assert.deepEqual(Object.keys(recovered.adjudicated).sort(), ['0', '1', '2']);
  assert.equal(recovered.adjudicated[2].protocolVersion, 2);
  assert.equal(recovered.adjudicated[2].verdict, 'REJECT');
  // A retransmission after recovery still echoes the original adjudication.
  const echo = submit(recovered, { deliveryId: 'd2', seq: 2, reading: 75, now: now() });
  assert.equal(echo.replayed, true);
  assert.deepEqual(echo.adjudication, recovered.adjudicated[2]);
});

test('a torn write never appears: leftover .tmp files are ignored', () => {
  const dir = tempDir();
  const store = new Store(dir);
  const d = createDrill({ id: 'drill-t', name: 't', baseThreshold: 100, now: now() });
  store.save(d);
  // Simulate a crash mid-write: truncated tmp file next to the good file.
  fs.writeFileSync(path.join(dir, 'drill-t.json.tmp'), '{"id":"drill-t","name":');
  const loaded = store.load('drill-t');
  assert.deepEqual(loaded, d);
  assert.equal(store.list().length, 1);
});

test('unknown ids and unsafe ids are reported as not found', () => {
  const store = new Store(tempDir());
  assert.throws(() => store.load('drill-missing'), (err) => err.code === 'DRILL_NOT_FOUND');
  assert.throws(() => store.load('../etc/passwd'), (err) => err.code === 'DRILL_NOT_FOUND');
});
