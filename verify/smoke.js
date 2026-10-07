'use strict';

/**
 * One-shot HTTP smoke test for the compose verify flow.
 *
 * Exercises the real API of a running app service:
 *   1. create a drill, register a new threshold protocol effective at seq 2
 *   2. submit seq 2 first (it must wait), crash the service, verify the
 *      waiting record survives the restart
 *   3. backfill seq 0 and 1, watch the queue drain in order with seq 2
 *      adjudicated under the NEW protocol
 *   4. crash again after the water-level advance, verify byte-identical state
 *   5. retransmit the original delivery (echo), probe every conflict shape
 *      (409s that must not rewrite anything), and late protocol registration
 *
 * Exit code 0 = all checks passed, 1 = any check failed.
 */

const BASE = (process.env.APP_ADDR || 'http://localhost:8080').replace(/\/$/, '');
const HEALTH_TIMEOUT_MS = 90_000;

let failures = 0;

function ok(msg) {
  console.log(`  ok   ${msg}`);
}

function fail(msg) {
  failures += 1;
  console.error(`  FAIL ${msg}`);
}

function check(cond, msg) {
  if (cond) ok(msg);
  else fail(msg);
  return cond;
}

function section(title) {
  console.log(`\n--- ${title}`);
}

async function req(method, path, body) {
  const res = await fetch(BASE + path, {
    method,
    headers: body ? { 'content-type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let json = null;
  try {
    json = JSON.parse(text);
  } catch {
    /* non-JSON body */
  }
  return { status: res.status, json, text };
}

async function waitHealthy(previousBootId, label) {
  const deadline = Date.now() + HEALTH_TIMEOUT_MS;
  for (;;) {
    try {
      const res = await fetch(`${BASE}/healthz`);
      if (res.ok) {
        const { bootId } = await res.json();
        if (bootId && bootId !== previousBootId) {
          ok(`service healthy at ${BASE} (${label}, bootId ${bootId.slice(0, 8)})`);
          return bootId;
        }
      }
    } catch {
      /* service down, keep polling */
    }
    if (Date.now() > deadline) {
      fail(`service did not become healthy (${label}) within ${HEALTH_TIMEOUT_MS}ms`);
      return null;
    }
    await new Promise((r) => setTimeout(r, 250));
  }
}

async function crash(bootId) {
  try {
    await req('POST', '/admin/crash');
  } catch {
    /* connection reset by the dying process is expected */
  }
  return waitHealthy(bootId, 'restarted after crash');
}

function expectError(r, status, code, what) {
  check(
    r.status === status && r.json && r.json.error && r.json.error.code === code,
    `${what} -> HTTP ${status} ${code} (got ${r.status} ${r.json && r.json.error ? r.json.error.code : r.text})`,
  );
}

async function main() {
  console.log(`smoke target: ${BASE}`);

  section('0. wait for the service');
  let bootId = await waitHealthy('', 'initial start');
  if (!bootId) return;

  section('1. create drill and register the new threshold protocol');
  const created = await req('POST', '/api/drills', { name: `smoke-${Date.now()}`, baseThreshold: 100 });
  if (!check(created.status === 201 && created.json.id, `create drill -> 201 (got ${created.status})`)) return;
  const id = created.json.id;
  check(created.json.waterLevel === 0, `water level starts at 0 (got ${created.json.waterLevel})`);

  const reg = await req('POST', `/api/drills/${id}/protocols`, { effectiveSeq: 2, threshold: 50 });
  check(
    reg.status === 201 && reg.json.protocol.version === 2 && reg.json.protocol.effectiveSeq === 2,
    `register protocol v2 @ seq 2, threshold 50 -> 201 (got ${reg.status})`,
  );

  section('2. submit seq 2 first: it waits, and survives a crash (waiting-queue write)');
  const w2 = await req('POST', `/api/drills/${id}/observations`, { deliveryId: 'smoke-d2', seq: 2, reading: 75 });
  check(
    w2.status === 200 && w2.json.status === 'WAITING' && w2.json.waterLevel === 0,
    `seq 2 ahead of the gap -> WAITING, water level stays 0 (got ${w2.status} ${w2.json && w2.json.status}, WL ${w2.json && w2.json.waterLevel})`,
  );

  bootId = await crash(bootId);
  if (!bootId) return;

  let view = await req('GET', `/api/drills/${id}`);
  check(
    view.status === 200 &&
      view.json.waterLevel === 0 &&
      view.json.waiting.length === 1 &&
      view.json.waiting[0].seq === 2 &&
      view.json.waiting[0].reading === 75 &&
      view.json.waiting[0].deliveryId === 'smoke-d2',
    'after restart: seq 2 still parked in the waiting queue, water level still 0',
  );

  section('3. backfill seq 0 and 1: queue drains in order, seq 2 uses the NEW protocol');
  const a0 = await req('POST', `/api/drills/${id}/observations`, { deliveryId: 'smoke-d0', seq: 0, reading: 10 });
  check(
    a0.status === 200 && a0.json.status === 'ADJUDICATED' && a0.json.adjudication.protocolVersion === 1,
    `seq 0 adjudicated under protocol v1 (got ${a0.status})`,
  );
  const a1 = await req('POST', `/api/drills/${id}/observations`, { deliveryId: 'smoke-d1', seq: 1, reading: 20 });
  check(
    a1.status === 200 && a1.json.waterLevel === 3,
    `seq 1 drains the queue: water level 0 -> 3 (got WL ${a1.json && a1.json.waterLevel})`,
  );

  view = await req('GET', `/api/drills/${id}`);
  const log = view.json.adjudicated || [];
  check(log.length === 3 && log.every((a, i) => a.seq === i), 'adjudication log holds exactly seq 0,1,2 in order');
  if (log.length === 3) {
    check(
      log[0].protocolVersion === 1 && log[0].verdict === 'ACCEPT' && log[1].protocolVersion === 1 && log[1].verdict === 'ACCEPT',
      'seq 0 and 1 adjudicated under the OLD protocol v1 (threshold 100)',
    );
    check(
      log[2].protocolVersion === 2 && log[2].verdict === 'REJECT' && typeof log[2].rejectReason === 'string' && log[2].rejectReason.length > 0,
      `seq 2 adjudicated under the NEW protocol v2 (threshold 50): REJECT with first reject reason "${log[2].rejectReason}"`,
    );
  }
  const snapshot = view.text;

  section('4. crash after the water-level advance: recovery matches uninterrupted execution');
  bootId = await crash(bootId);
  if (!bootId) return;
  view = await req('GET', `/api/drills/${id}`);
  check(view.status === 200 && view.text === snapshot, 'after restart: drill state byte-identical to the pre-crash snapshot');

  section('5. idempotent replay and conflict rejection');
  const echo = await req('POST', `/api/drills/${id}/observations`, { deliveryId: 'smoke-d2', seq: 2, reading: 75 });
  check(
    echo.status === 200 && echo.json.replayed === true && echo.json.status === 'ADJUDICATED' &&
      echo.json.adjudication && echo.json.adjudication.protocolVersion === 2 && echo.json.adjudication.verdict === 'REJECT',
    'identical retransmission of seq 2 echoes the original adjudication (v2, REJECT)',
  );
  if (echo.json.adjudication && log.length === 3) {
    check(
      JSON.stringify(echo.json.adjudication) === JSON.stringify(log[2]),
      'echoed adjudication is exactly the stored one (immutable)',
    );
  }

  expectError(
    await req('POST', `/api/drills/${id}/observations`, { deliveryId: 'smoke-d2', seq: 2, reading: 76 }),
    409, 'DELIVERY_CONFLICT', 'same delivery id, changed reading',
  );
  expectError(
    await req('POST', `/api/drills/${id}/observations`, { deliveryId: 'smoke-d2', seq: 9, reading: 75 }),
    409, 'DELIVERY_CONFLICT', 'same delivery id, changed seq',
  );
  expectError(
    await req('POST', `/api/drills/${id}/observations`, { deliveryId: 'smoke-intruder', seq: 2, reading: 80 }),
    409, 'SEQ_CONFLICT', 'same seq, different content',
  );
  expectError(
    await req('POST', `/api/drills/${id}/protocols`, { effectiveSeq: 1, threshold: 10 }),
    409, 'EFFECTIVE_SEQ_PASSED', 'protocol registration below the water level',
  );

  view = await req('GET', `/api/drills/${id}`);
  check(view.text === snapshot, 'none of the rejections rewrote the waiting queue or the adjudication log');

  const reg3 = await req('POST', `/api/drills/${id}/protocols`, { effectiveSeq: 10, threshold: 20 });
  check(
    reg3.status === 201 && reg3.json.protocol.version === 3,
    `register protocol v3 @ seq 10 (above water level) -> 201 (got ${reg3.status})`,
  );
  expectError(
    await req('POST', `/api/drills/${id}/protocols`, { effectiveSeq: 10, threshold: 30 }),
    409, 'EFFECTIVE_SEQ_DUPLICATE', 'duplicate protocol at an effective sequence',
  );
  view = await req('GET', `/api/drills/${id}`);
  check(
    view.json.protocols.length === 3 && view.json.protocols[2].effectiveSeq === 10 && view.json.protocols[2].threshold === 20,
    'duplicate registration kept the original v3 entry untouched',
  );

  section('6. gap ahead of the water level still blocks; validation holds');
  const w4 = await req('POST', `/api/drills/${id}/observations`, { deliveryId: 'smoke-d4', seq: 4, reading: 5 });
  check(w4.json && w4.json.status === 'WAITING' && w4.json.waterLevel === 3, 'seq 4 waits behind missing seq 3');
  const a3 = await req('POST', `/api/drills/${id}/observations`, { deliveryId: 'smoke-d3', seq: 3, reading: 5 });
  check(a3.json && a3.json.waterLevel === 5, 'seq 3 fills the gap: 3 and 4 drain, water level 5');
  view = await req('GET', `/api/drills/${id}`);
  const last = view.json.adjudicated[view.json.adjudicated.length - 1];
  check(
    last && last.seq === 4 && last.protocolVersion === 2 && last.verdict === 'ACCEPT',
    'seq 4 adjudicated under protocol v2 (5 <= 50 -> ACCEPT)',
  );

  const badBodies = [
    { deliveryId: '', seq: 5, reading: 1 },
    { deliveryId: 'smoke-x', seq: -1, reading: 1 },
    { deliveryId: 'smoke-x', seq: 5, reading: 'not-a-number' },
  ];
  for (const body of badBodies) {
    const r = await req('POST', `/api/drills/${id}/observations`, body);
    check(r.status === 400 && r.json.error && r.json.error.code === 'VALIDATION', `invalid submission rejected -> 400 VALIDATION (${JSON.stringify(body)})`);
  }
}

main()
  .catch((err) => {
    console.error(`smoke aborted with unexpected error: ${err.stack || err}`);
    failures += 1;
  })
  .finally(() => {
    if (failures > 0) {
      console.error(`\nSMOKE RESULT: FAIL (${failures} check(s) failed)`);
      process.exit(1);
    }
    console.log('\nSMOKE RESULT: PASS');
  });
