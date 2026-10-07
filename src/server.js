'use strict';

/**
 * HTTP API and operator page for the beam-protection drill service.
 *
 *   GET    /healthz                         liveness + boot id (recovery checks)
 *   GET    /                                operator console (static page)
 *   POST   /api/drills                      create a drill { name, baseThreshold }
 *   GET    /api/drills                      list drill summaries
 *   GET    /api/drills/:id                  water level, waiting queue, adjudications
 *   POST   /api/drills/:id/protocols        register protocol { effectiveSeq, threshold }
 *   POST   /api/drills/:id/observations     submit observation { deliveryId, seq, reading }
 *   POST   /admin/crash                     test-only crash hook (ENABLE_CRASH_ENDPOINT=1)
 *
 * Errors are JSON: { "error": { "code", "message" } }.
 */

const http = require('http');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const drill = require('./drill');

const INDEX_HTML = path.join(__dirname, '..', 'public', 'index.html');

const STATUS_BY_CODE = {
  [drill.CODES.VALIDATION]: 400,
  [drill.CODES.DRILL_NOT_FOUND]: 404,
  [drill.CODES.DELIVERY_CONFLICT]: 409,
  [drill.CODES.SEQ_CONFLICT]: 409,
  [drill.CODES.EFFECTIVE_SEQ_PASSED]: 409,
  [drill.CODES.EFFECTIVE_SEQ_DUPLICATE]: 409,
};

function sendJson(res, status, body) {
  const data = JSON.stringify(body);
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8' });
  res.end(data);
}

function sendError(res, err) {
  if (err instanceof drill.DrillError) {
    sendJson(res, STATUS_BY_CODE[err.code] || 400, { error: { code: err.code, message: err.message } });
    return;
  }
  sendJson(res, 500, { error: { code: 'INTERNAL', message: String((err && err.message) || err) } });
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > 1024 * 1024) {
        reject(new drill.DrillError(drill.CODES.VALIDATION, 'request body too large'));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}

async function readJson(req, allowedFields) {
  const raw = await readBody(req);
  let body;
  try {
    body = JSON.parse(raw);
  } catch {
    throw new drill.DrillError(drill.CODES.VALIDATION, 'request body must be valid JSON');
  }
  if (body === null || typeof body !== 'object' || Array.isArray(body)) {
    throw new drill.DrillError(drill.CODES.VALIDATION, 'request body must be a JSON object');
  }
  for (const key of Object.keys(body)) {
    if (!allowedFields.includes(key)) {
      throw new drill.DrillError(drill.CODES.VALIDATION, `unknown field "${key}"`);
    }
  }
  return body;
}

function drillView(d) {
  return {
    id: d.id,
    name: d.name,
    createdAt: d.createdAt,
    waterLevel: d.waterLevel,
    protocols: d.protocols,
    waiting: drill.sortedWaiting(d),
    adjudicated: drill.sortedAdjudicated(d),
  };
}

function drillSummary(d) {
  return {
    id: d.id,
    name: d.name,
    createdAt: d.createdAt,
    waterLevel: d.waterLevel,
    waitingCount: Object.keys(d.waiting).length,
    adjudicatedCount: Object.keys(d.adjudicated).length,
  };
}

/**
 * Build the request handler. `registry` is a Registry; `options.enableCrash`
 * exposes the test-only crash hook used by the compose verify flow.
 */
function createServer(registry, options = {}) {
  const bootId = crypto.randomUUID();

  return http.createServer(async (req, res) => {
    try {
      const url = new URL(req.url, 'http://localhost');
      const parts = url.pathname.split('/').filter(Boolean);

      if (req.method === 'GET' && url.pathname === '/healthz') {
        sendJson(res, 200, { ok: true, bootId });
        return;
      }

      if (req.method === 'GET' && (url.pathname === '/' || url.pathname === '/index.html')) {
        res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
        res.end(fs.readFileSync(INDEX_HTML));
        return;
      }

      if (req.method === 'POST' && url.pathname === '/admin/crash') {
        if (!options.enableCrash) {
          sendJson(res, 404, { error: { code: 'NOT_FOUND', message: 'not found' } });
          return;
        }
        sendJson(res, 200, { status: 'crashing', bootId });
        // Exit after the response has been flushed; the compose restart
        // policy brings the service back, which exercises recovery.
        setTimeout(() => process.exit(1), 100).unref();
        return;
      }

      if (parts[0] === 'api' && parts[1] === 'drills') {
        // POST /api/drills
        if (parts.length === 2 && req.method === 'POST') {
          const body = await readJson(req, ['name', 'baseThreshold']);
          const created = registry.create({ name: body.name, baseThreshold: body.baseThreshold });
          sendJson(res, 201, drillView(created));
          return;
        }
        // GET /api/drills
        if (parts.length === 2 && req.method === 'GET') {
          sendJson(res, 200, registry.list().map(drillSummary));
          return;
        }
        const id = parts[2];
        // GET /api/drills/:id
        if (parts.length === 3 && req.method === 'GET') {
          sendJson(res, 200, drillView(registry.get(id)));
          return;
        }
        // POST /api/drills/:id/protocols
        if (parts.length === 4 && parts[3] === 'protocols' && req.method === 'POST') {
          const body = await readJson(req, ['effectiveSeq', 'threshold']);
          const now = new Date().toISOString();
          const protocol = registry.mutate(id, (d) =>
            drill.registerProtocol(d, { effectiveSeq: body.effectiveSeq, threshold: body.threshold, now }),
          );
          sendJson(res, 201, { protocol });
          return;
        }
        // POST /api/drills/:id/observations
        if (parts.length === 4 && parts[3] === 'observations' && req.method === 'POST') {
          const body = await readJson(req, ['deliveryId', 'seq', 'reading']);
          const now = new Date().toISOString();
          const result = registry.mutate(id, (d) =>
            drill.submit(d, { deliveryId: body.deliveryId, seq: body.seq, reading: body.reading, now }),
          );
          sendJson(res, 200, { ...result, waterLevel: registry.get(id).waterLevel });
          return;
        }
      }

      sendJson(res, 404, { error: { code: 'NOT_FOUND', message: 'not found' } });
    } catch (err) {
      sendError(res, err);
    }
  });
}

function main() {
  const { Store } = require('./store');
  const { Registry } = require('./registry');

  const port = Number(process.env.PORT || 8080);
  const dataDir = process.env.DATA_DIR || path.join(process.cwd(), 'data');
  const enableCrash = process.env.ENABLE_CRASH_ENDPOINT === '1';

  const registry = new Registry(new Store(dataDir));
  const server = createServer(registry, { enableCrash });
  server.listen(port, () => {
    console.log(
      `beamguard listening on :${port} (data dir: ${dataDir}, crash endpoint: ${enableCrash ? 'enabled' : 'disabled'})`,
    );
  });
}

if (require.main === module) {
  main();
}

module.exports = { createServer };
