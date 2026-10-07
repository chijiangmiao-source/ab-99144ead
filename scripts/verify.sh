#!/bin/sh
# One-shot acceptance entrypoint for the verify container.
# Runs the build check, the state-machine test suite, and the HTTP smoke
# test against the app service, then exits with the overall result.
set -eu
cd "$(dirname "$0")/.."

echo "=== [1/3] project build check ==="
node scripts/check.js

echo ""
echo "=== [2/3] state machine code tests ==="
node --test test/

echo ""
echo "=== [3/3] HTTP smoke: out-of-order backfill, protocol switch, crash recovery ==="
node verify/smoke.js

echo ""
echo "VERIFY RESULT: PASS"
