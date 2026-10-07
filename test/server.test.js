'use strict';

const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { createServer } = require('../src/server');
const { Store } = require('../src/store');
const { Registry } = require('../src/registry');

let dataDir;
let server;
let base;

async function api(method, p, body) {
  const res = await fetch(base + p, {
    method,
    headers: body ? { 'content-type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let json = null;
  try {
    json = JSON.parse(text);
  } catch {
    /* non-JSON response (e.g. the operator page) */
  }
  return { status: res.status, body: json, text };
}

/** Simulate a process restart: a brand-new server over the same data dir. */
function restart() {
  server.close();
  server = createServer(new Registry(new Store(dataDir)), { enableCrash: true });
  server.listen(0);
  base = `http://127.0.0.1:${server.address().port}`;
}

before(async () => {
  dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'beamguard-http-'));
  server = createServer(new Registry(new Store(dataDir)), { enableCrash: true });
  server.listen(0);
  base = `http://127.0.0.1:${server.address().port}`;
});

after(() => server.close());

test('HTTP flow: out-of-order backfill, protocol switch, restart recovery, idempotency, conflicts', async () => {
  // Create a drill with base threshold 100, switch to 50 at sequence 2.
  const created = await api('POST', '/api/drills', { name: 'http-flow', baseThreshold: 100 });
  assert.equal(created.status, 201);
  const id = created.body.id;
  assert.equal(created.body.waterLevel, 0);

  const reg = await api('POST', `/api/drills/${id}/protocols`, { effectiveSeq: 2, threshold: 50 });
  assert.equal(reg.status, 201);
  assert.equal(reg.body.protocol.version, 2);

  // Submit 2 first: it must wait, water level stays at 0.
  const w = await api('POST', `/api/drills/${id}/observations`, { deliveryId: 'd2', seq: 2, reading: 75 });
  assert.equal(w.status, 200);
  assert.equal(w.body.status, 'WAITING');
  assert.equal(w.body.waterLevel, 0);

  // Restart #1: crash after the waiting-queue write.
  restart();
  let view = (await api('GET', `/api/drills/${id}`)).body;
  assert.equal(view.waterLevel, 0);
  assert.deepEqual(view.waiting.map((x) => x.seq), [2]);

  // Backfill 0 and 1: the queue drains in order, 2 under the new protocol.
  const a0 = await api('POST', `/api/drills/${id}/observations`, { deliveryId: 'd0', seq: 0, reading: 10 });
  assert.equal(a0.body.status, 'ADJUDICATED');
  const a1 = await api('POST', `/api/drills/${id}/observations`, { deliveryId: 'd1', seq: 1, reading: 20 });
  assert.equal(a1.body.waterLevel, 3);

  view = (await api('GET', `/api/drills/${id}`)).body;
  assert.deepEqual(view.adjudicated.map((a) => [a.seq, a.protocolVersion, a.verdict]), [
    [0, 1, 'ACCEPT'],
    [1, 1, 'ACCEPT'],
    [2, 2, 'REJECT'],
  ]);
  assert.ok(view.adjudicated[2].rejectReason.length > 0);
  const snapshot = JSON.stringify(view);

  // Restart #2: crash after the water-level advance; state must be identical.
  restart();
  const recovered = await api('GET', `/api/drills/${id}`);
  assert.equal(JSON.stringify(recovered.body), snapshot);

  // Identical retransmission echoes the original adjudication.
  const echo = await api('POST', `/api/drills/${id}/observations`, { deliveryId: 'd2', seq: 2, reading: 75 });
  assert.equal(echo.status, 200);
  assert.equal(echo.body.replayed, true);
  assert.deepEqual(echo.body.adjudication, view.adjudicated[2]);

  // Same id, changed content -> 409; same seq, different content -> 409.
  for (const body of [
    { deliveryId: 'd2', seq: 2, reading: 76 },
    { deliveryId: 'd2', seq: 5, reading: 75 },
    { deliveryId: 'intruder', seq: 2, reading: 80 },
  ]) {
    const r = await api('POST', `/api/drills/${id}/observations`, body);
    assert.equal(r.status, 409, JSON.stringify(body));
  }
  // Late protocol registration -> 409.
  const late = await api('POST', `/api/drills/${id}/protocols`, { effectiveSeq: 1, threshold: 10 });
  assert.equal(late.status, 409);
  assert.equal(late.body.error.code, 'EFFECTIVE_SEQ_PASSED');

  // None of the rejections rewrote anything.
  const afterConflicts = await api('GET', `/api/drills/${id}`);
  assert.equal(JSON.stringify(afterConflicts.body), snapshot);
});

test('HTTP validation and unknown routes', async () => {
  const created = await api('POST', '/api/drills', { name: 'validation', baseThreshold: 1 });
  const id = created.body.id;

  for (const body of [
    { deliveryId: '', seq: 0, reading: 1 },
    { deliveryId: 'x', seq: -1, reading: 1 },
    { deliveryId: 'x', seq: 0, reading: 'nope' },
    { deliveryId: 'x', seq: 0 },
    { deliveryId: 'x', seq: 0, reading: 1, extra: true },
  ]) {
    const r = await api('POST', `/api/drills/${id}/observations`, body);
    assert.equal(r.status, 400, JSON.stringify(body));
    assert.equal(r.body.error.code, 'VALIDATION');
  }

  assert.equal((await api('GET', '/api/drills/drill-nope')).status, 404);
  assert.equal((await api('POST', '/api/drills', { name: 'x' })).status, 400);
  assert.equal((await api('GET', '/healthz')).status, 200);
  assert.equal((await api('GET', '/')).status, 200);
});
