'use strict';

/**
 * Beam-protection drill state machine.
 *
 * One drill adjudicates a stream of observations identified by integer sample
 * sequence numbers. Adjudication happens strictly in contiguous order starting
 * at sequence 0: the water level is the next sequence number the system is
 * waiting for. Observations that arrive ahead of a gap are parked in the
 * waiting queue and drained, in order, once the gap is filled.
 *
 * Threshold protocols are registered to take effect at an integer sequence
 * number. The protocol actually applied to a sequence is the one with the
 * greatest effectiveSeq <= seq at the moment the water level reaches that
 * sequence. Once the water level has crossed an effective sequence, every
 * later adjudication uses the newer protocol; adjudicated history is never
 * recomputed, so registering a protocol below the current water level is
 * rejected.
 *
 * Every sequence number yields exactly one immutable adjudication.
 * Retransmissions are idempotent:
 *   - same delivery id + same content  -> echo the original outcome
 *   - same delivery id + other content -> DELIVERY_CONFLICT
 *   - same sequence + other reading    -> SEQ_CONFLICT
 * Conflicts never rewrite a waiting record or an existing adjudication.
 */

const VERDICT_ACCEPT = 'ACCEPT';
const VERDICT_REJECT = 'REJECT';

const STATUS_WAITING = 'WAITING';
const STATUS_ADJUDICATED = 'ADJUDICATED';

const CODES = {
  VALIDATION: 'VALIDATION',
  DELIVERY_CONFLICT: 'DELIVERY_CONFLICT',
  SEQ_CONFLICT: 'SEQ_CONFLICT',
  EFFECTIVE_SEQ_PASSED: 'EFFECTIVE_SEQ_PASSED',
  EFFECTIVE_SEQ_DUPLICATE: 'EFFECTIVE_SEQ_DUPLICATE',
  DRILL_NOT_FOUND: 'DRILL_NOT_FOUND',
};

/** Domain error carrying a stable machine-readable code. */
class DrillError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'DrillError';
    this.code = code;
  }
}

function validateThreshold(value, field) {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new DrillError(CODES.VALIDATION, `${field} must be a finite number`);
  }
}

function validateSeq(value, field) {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new DrillError(CODES.VALIDATION, `${field} must be an integer >= 0`);
  }
}

function validateDeliveryId(value) {
  if (typeof value !== 'string' || value.length === 0 || value.length > 200) {
    throw new DrillError(CODES.VALIDATION, 'deliveryId must be a non-empty string of at most 200 characters');
  }
}

/**
 * Create a fresh drill. The base threshold is registered as protocol v1
 * effective at sequence 0; the water level starts at sequence 0.
 */
function createDrill({ id, name, baseThreshold, now }) {
  if (typeof id !== 'string' || id.length === 0) {
    throw new DrillError(CODES.VALIDATION, 'id must not be empty');
  }
  if (typeof name !== 'string' || name.length === 0 || name.length > 120) {
    throw new DrillError(CODES.VALIDATION, 'name must be a non-empty string of at most 120 characters');
  }
  validateThreshold(baseThreshold, 'baseThreshold');
  return {
    id,
    name,
    createdAt: now,
    protocols: [{ version: 1, effectiveSeq: 0, threshold: baseThreshold, registeredAt: now }],
    waterLevel: 0,
    waiting: {},     // seq -> { seq, deliveryId, reading, receivedAt }
    adjudicated: {}, // seq -> adjudication (immutable)
    deliveries: {},  // deliveryId -> { seq, reading }
  };
}

/**
 * Register a new threshold protocol taking effect at effectiveSeq.
 * Rejected when the water level has already passed effectiveSeq (history must
 * not be recomputed) or when a protocol already exists at that sequence.
 */
function registerProtocol(drill, { effectiveSeq, threshold, now }) {
  validateSeq(effectiveSeq, 'effectiveSeq');
  validateThreshold(threshold, 'threshold');
  if (effectiveSeq < drill.waterLevel) {
    throw new DrillError(
      CODES.EFFECTIVE_SEQ_PASSED,
      `effectiveSeq ${effectiveSeq} is below water level ${drill.waterLevel}; adjudicated history must not be recomputed`,
    );
  }
  if (drill.protocols.some((p) => p.effectiveSeq === effectiveSeq)) {
    throw new DrillError(CODES.EFFECTIVE_SEQ_DUPLICATE, `a protocol is already registered at effectiveSeq ${effectiveSeq}`);
  }
  const version = Math.max(...drill.protocols.map((p) => p.version)) + 1;
  const protocol = { version, effectiveSeq, threshold, registeredAt: now };
  drill.protocols.push(protocol);
  drill.protocols.sort((a, b) => a.effectiveSeq - b.effectiveSeq);
  return protocol;
}

/** Protocol in force for a sequence: greatest effectiveSeq <= seq. */
function protocolFor(drill, seq) {
  let chosen = drill.protocols[0];
  for (const p of drill.protocols) {
    if (p.effectiveSeq <= seq) chosen = p;
    else break;
  }
  return chosen;
}

/** Reading already recorded for a sequence, if any (waiting or adjudicated). */
function recordedReading(drill, seq) {
  const w = drill.waiting[seq];
  if (w) return w.reading;
  const a = drill.adjudicated[seq];
  if (a) return a.reading;
  return undefined;
}

/** Current outcome of a sequence, for (re)transmission echoes. */
function resultFor(drill, seq, replayed) {
  const adjudication = drill.adjudicated[seq];
  if (adjudication) {
    return { status: STATUS_ADJUDICATED, seq, replayed, adjudication };
  }
  return { status: STATUS_WAITING, seq, replayed };
}

/**
 * Adjudicate every sequence the water level can reach, in order. Each drained
 * observation is decided under the protocol in force at that moment and moved
 * to the immutable adjudication log; the water level advances past it.
 */
function drain(drill, now) {
  for (;;) {
    const rec = drill.waiting[drill.waterLevel];
    if (!rec) return;
    const protocol = protocolFor(drill, rec.seq);
    const adjudication = {
      seq: rec.seq,
      deliveryId: rec.deliveryId,
      reading: rec.reading,
      protocolVersion: protocol.version,
      threshold: protocol.threshold,
      verdict: VERDICT_ACCEPT,
      rejectReason: null,
      adjudicatedAt: now,
    };
    if (rec.reading > protocol.threshold) {
      adjudication.verdict = VERDICT_REJECT;
      adjudication.rejectReason =
        `reading ${rec.reading} exceeds threshold ${protocol.threshold} of protocol v${protocol.version}`;
    }
    delete drill.waiting[rec.seq];
    drill.adjudicated[rec.seq] = adjudication;
    drill.waterLevel += 1;
  }
}

/**
 * Submit an observation. Returns the submission outcome; throws DrillError on
 * validation failure or on a delivery/sequence conflict. Conflicting
 * submissions never mutate the drill.
 */
function submit(drill, { deliveryId, seq, reading, now }) {
  validateDeliveryId(deliveryId);
  validateSeq(seq, 'seq');
  validateThreshold(reading, 'reading');

  // 1. Delivery-id idempotency: an identical retransmission echoes the
  //    original outcome; the same id carrying different content is rejected.
  const bound = drill.deliveries[deliveryId];
  if (bound) {
    if (bound.seq !== seq || bound.reading !== reading) {
      throw new DrillError(
        CODES.DELIVERY_CONFLICT,
        `delivery "${deliveryId}" is already bound to seq ${bound.seq} reading ${bound.reading}; ` +
          `refused rebinding to seq ${seq} reading ${reading}`,
      );
    }
    return resultFor(drill, seq, true);
  }

  // 2. Sequence uniqueness: one sequence number carries exactly one content.
  const existing = recordedReading(drill, seq);
  if (existing !== undefined) {
    if (existing !== reading) {
      throw new DrillError(
        CODES.SEQ_CONFLICT,
        `seq ${seq} already carries reading ${existing}; conflicting reading ${reading} rejected`,
      );
    }
    // Same content under a new delivery id: bind it so its retransmissions
    // echo the same outcome too. The stored record itself is untouched.
    drill.deliveries[deliveryId] = { seq, reading };
    return resultFor(drill, seq, true);
  }

  // 3. New observation: persist it in the waiting queue, then drain whatever
  //    the water level can now reach.
  drill.deliveries[deliveryId] = { seq, reading };
  drill.waiting[seq] = { seq, deliveryId, reading, receivedAt: now };
  drain(drill, now);
  return resultFor(drill, seq, false);
}

/** Waiting queue as a list sorted by sequence number. */
function sortedWaiting(drill) {
  return Object.values(drill.waiting).sort((a, b) => a.seq - b.seq);
}

/** Adjudication log as a list sorted by sequence number. */
function sortedAdjudicated(drill) {
  return Object.values(drill.adjudicated).sort((a, b) => a.seq - b.seq);
}

module.exports = {
  VERDICT_ACCEPT,
  VERDICT_REJECT,
  STATUS_WAITING,
  STATUS_ADJUDICATED,
  CODES,
  DrillError,
  createDrill,
  registerProtocol,
  protocolFor,
  submit,
  sortedWaiting,
  sortedAdjudicated,
};
