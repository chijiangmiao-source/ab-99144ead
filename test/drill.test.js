'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const {
  createDrill,
  registerProtocol,
  submit,
  protocolFor,
  sortedWaiting,
  sortedAdjudicated,
  DrillError,
  CODES,
} = require('../src/drill');

const T0 = '2026-10-07T00:00:00.000Z';
let tick = 0;
/** Deterministic clock so recovered and uninterrupted runs compare equal. */
function now() {
  tick += 1;
  return new Date(Date.parse(T0) + tick * 1000).toISOString();
}

function newDrill(base = 100) {
  return createDrill({ id: 'drill-test', name: 'test', baseThreshold: base, now: now() });
}

test('observations arriving in order are adjudicated immediately and advance the water level', () => {
  const d = newDrill();
  for (let seq = 0; seq < 3; seq += 1) {
    const r = submit(d, { deliveryId: `d${seq}`, seq, reading: 10, now: now() });
    assert.equal(r.status, 'ADJUDICATED');
    assert.equal(r.adjudication.verdict, 'ACCEPT');
    assert.equal(r.adjudication.protocolVersion, 1);
    assert.equal(d.waterLevel, seq + 1);
  }
  assert.equal(sortedWaiting(d).length, 0);
});

test('out-of-order observation waits persistently; filling the gap drains in sequence order', () => {
  const d = newDrill();
  const r2 = submit(d, { deliveryId: 'd2', seq: 2, reading: 10, now: now() });
  assert.equal(r2.status, 'WAITING');
  assert.equal(d.waterLevel, 0, 'water level must not move while a gap exists');

  submit(d, { deliveryId: 'd4', seq: 4, reading: 10, now: now() });
  assert.equal(d.waterLevel, 0);
  assert.deepEqual(sortedWaiting(d).map((w) => w.seq), [2, 4]);

  const r0 = submit(d, { deliveryId: 'd0', seq: 0, reading: 10, now: now() });
  assert.equal(r0.status, 'ADJUDICATED');
  assert.equal(d.waterLevel, 1, 'seq 2 still blocked by missing seq 1');
  assert.deepEqual(sortedWaiting(d).map((w) => w.seq), [2, 4]);

  submit(d, { deliveryId: 'd1', seq: 1, reading: 10, now: now() });
  assert.equal(d.waterLevel, 3, 'gap filled: 1 and 2 drained in order, 4 still waits for 3');
  assert.deepEqual(sortedAdjudicated(d).map((a) => a.seq), [0, 1, 2]);

  submit(d, { deliveryId: 'd3', seq: 3, reading: 10, now: now() });
  assert.equal(d.waterLevel, 5);
  assert.deepEqual(sortedAdjudicated(d).map((a) => a.seq), [0, 1, 2, 3, 4]);
});

test('protocol registered at an effective sequence applies from that sequence on', () => {
  const d = newDrill(100);
  registerProtocol(d, { effectiveSeq: 2, threshold: 50, now: now() });

  // Backfill out of order: 2 arrives first, then 0 and 1.
  submit(d, { deliveryId: 'd2', seq: 2, reading: 75, now: now() });
  submit(d, { deliveryId: 'd0', seq: 0, reading: 75, now: now() });
  submit(d, { deliveryId: 'd1', seq: 1, reading: 75, now: now() });

  const log = sortedAdjudicated(d);
  assert.deepEqual(log.map((a) => a.seq), [0, 1, 2]);
  assert.equal(log[0].protocolVersion, 1);
  assert.equal(log[0].verdict, 'ACCEPT', '75 <= 100 under the old protocol');
  assert.equal(log[1].protocolVersion, 1);
  assert.equal(log[1].verdict, 'ACCEPT');
  assert.equal(log[2].protocolVersion, 2, 'water level crossed 2: new protocol applies');
  assert.equal(log[2].verdict, 'REJECT', '75 > 50 under the new protocol');
  assert.match(log[2].rejectReason, /exceeds threshold 50/);
});

test('protocol for a sequence is the one with greatest effectiveSeq <= seq', () => {
  const d = newDrill(100);
  registerProtocol(d, { effectiveSeq: 2, threshold: 50, now: now() });
  registerProtocol(d, { effectiveSeq: 5, threshold: 10, now: now() });
  const version = (seq) => protocolFor(d, seq).version;
  assert.equal(version(0), 1);
  assert.equal(version(1), 1);
  assert.equal(version(2), 2);
  assert.equal(version(4), 2);
  assert.equal(version(5), 3);
  assert.equal(version(99), 3);
});

test('registering a protocol below the water level is rejected; history is not recomputed', () => {
  const d = newDrill(100);
  submit(d, { deliveryId: 'd0', seq: 0, reading: 75, now: now() });
  submit(d, { deliveryId: 'd1', seq: 1, reading: 75, now: now() });

  assert.throws(() => registerProtocol(d, { effectiveSeq: 1, threshold: 10, now: now() }), (err) => {
    assert.ok(err instanceof DrillError);
    assert.equal(err.code, CODES.EFFECTIVE_SEQ_PASSED);
    return true;
  });
  assert.throws(
    () => registerProtocol(d, { effectiveSeq: 0, threshold: 10, now: now() }),
    (err) => err.code === CODES.EFFECTIVE_SEQ_PASSED,
  );
  // The adjudication of seq 1 still shows the old protocol, untouched.
  assert.equal(d.adjudicated[1].protocolVersion, 1);
  assert.equal(d.adjudicated[1].verdict, 'ACCEPT');

  // At or above the water level is fine.
  const p = registerProtocol(d, { effectiveSeq: 2, threshold: 10, now: now() });
  assert.equal(p.version, 2);
});

test('duplicate effective sequence registration is rejected', () => {
  const d = newDrill();
  registerProtocol(d, { effectiveSeq: 3, threshold: 50, now: now() });
  assert.throws(
    () => registerProtocol(d, { effectiveSeq: 3, threshold: 60, now: now() }),
    (err) => err.code === CODES.EFFECTIVE_SEQ_DUPLICATE,
  );
});

test('identical retransmission echoes the original outcome without changing state', () => {
  const d = newDrill();
  // Waiting record echo.
  submit(d, { deliveryId: 'd5', seq: 5, reading: 42, now: now() });
  const waitingEcho = submit(d, { deliveryId: 'd5', seq: 5, reading: 42, now: now() });
  assert.equal(waitingEcho.status, 'WAITING');
  assert.equal(waitingEcho.replayed, true);
  assert.equal(sortedWaiting(d).length, 1, 'no duplicate waiting record');

  // Adjudicated echo.
  const first = submit(d, { deliveryId: 'd0', seq: 0, reading: 7, now: now() });
  const echo = submit(d, { deliveryId: 'd0', seq: 0, reading: 7, now: now() });
  assert.equal(echo.status, 'ADJUDICATED');
  assert.equal(echo.replayed, true);
  assert.deepEqual(echo.adjudication, first.adjudication);
  assert.equal(d.waterLevel, 1);
  assert.equal(sortedAdjudicated(d).length, 1, 'still exactly one adjudication per sequence');
});

test('same delivery id with changed seq or reading is rejected and rewrites nothing', () => {
  const d = newDrill();
  submit(d, { deliveryId: 'd0', seq: 0, reading: 7, now: now() });
  const before = structuredClone(d);

  assert.throws(
    () => submit(d, { deliveryId: 'd0', seq: 0, reading: 8, now: now() }),
    (err) => err.code === CODES.DELIVERY_CONFLICT,
  );
  assert.throws(
    () => submit(d, { deliveryId: 'd0', seq: 1, reading: 7, now: now() }),
    (err) => err.code === CODES.DELIVERY_CONFLICT,
  );
  assert.deepEqual(d, before, 'conflict must not mutate the drill');
});

test('same sequence with different content is rejected and rewrites nothing', () => {
  const d = newDrill();
  submit(d, { deliveryId: 'd0', seq: 0, reading: 7, now: now() });
  submit(d, { deliveryId: 'd9', seq: 9, reading: 1, now: now() }); // waiting record
  const before = structuredClone(d);

  // Conflict against an adjudicated sequence.
  assert.throws(
    () => submit(d, { deliveryId: 'other', seq: 0, reading: 8, now: now() }),
    (err) => err.code === CODES.SEQ_CONFLICT,
  );
  // Conflict against a waiting sequence.
  assert.throws(
    () => submit(d, { deliveryId: 'other2', seq: 9, reading: 2, now: now() }),
    (err) => err.code === CODES.SEQ_CONFLICT,
  );
  assert.deepEqual(d, before, 'neither the adjudication nor the waiting record may be rewritten');
});

test('same sequence and content under a new delivery id echoes and binds the id', () => {
  const d = newDrill();
  submit(d, { deliveryId: 'd0', seq: 0, reading: 7, now: now() });
  const echo = submit(d, { deliveryId: 'alias', seq: 0, reading: 7, now: now() });
  assert.equal(echo.status, 'ADJUDICATED');
  assert.equal(echo.replayed, true);
  // The alias now echoes too, and cannot be rebound to other content.
  assert.equal(submit(d, { deliveryId: 'alias', seq: 0, reading: 7, now: now() }).replayed, true);
  assert.throws(
    () => submit(d, { deliveryId: 'alias', seq: 0, reading: 9, now: now() }),
    (err) => err.code === CODES.DELIVERY_CONFLICT,
  );
});

test('each sequence yields exactly one immutable adjudication even under mixed replays', () => {
  const d = newDrill(100);
  registerProtocol(d, { effectiveSeq: 1, threshold: 5, now: now() });
  submit(d, { deliveryId: 'd1', seq: 1, reading: 9, now: now() }); // waits
  submit(d, { deliveryId: 'd0', seq: 0, reading: 1, now: now() }); // drains 0 and 1
  const adjudicated = d.adjudicated[1];
  assert.equal(adjudicated.protocolVersion, 2);
  assert.equal(adjudicated.verdict, 'REJECT');

  // Replays and conflicts afterwards never touch the stored adjudication.
  submit(d, { deliveryId: 'd1', seq: 1, reading: 9, now: now() });
  assert.throws(() => submit(d, { deliveryId: 'x', seq: 1, reading: 3, now: now() }));
  assert.deepEqual(d.adjudicated[1], adjudicated);
});

test('validation rejects malformed submissions and registrations', () => {
  const d = newDrill();
  const bad = [
    { deliveryId: '', seq: 0, reading: 1 },
    { deliveryId: 'x', seq: -1, reading: 1 },
    { deliveryId: 'x', seq: 0.5, reading: 1 },
    { deliveryId: 'x', seq: 0, reading: Number.NaN },
    { deliveryId: 'x', seq: 0, reading: Infinity },
    { deliveryId: 'x', seq: 0, reading: '7' },
  ];
  for (const body of bad) {
    assert.throws(() => submit(d, { ...body, now: now() }), (err) => err.code === CODES.VALIDATION);
  }
  assert.throws(() => registerProtocol(d, { effectiveSeq: -1, threshold: 1, now: now() }));
  assert.throws(() => registerProtocol(d, { effectiveSeq: 1, threshold: Number.NaN, now: now() }));
  assert.throws(() => createDrill({ id: 'x', name: '', baseThreshold: 1, now: now() }));
  assert.throws(() => createDrill({ id: 'x', name: 'n', baseThreshold: Number.NaN, now: now() }));
});
