'use strict';

/**
 * Project build check: parse package.json and syntax-check every project
 * JavaScript file with `node --check`. Exits non-zero on the first problem.
 */

const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const SCAN_DIRS = ['src', 'test', 'verify', 'scripts'];

function collect(dir) {
  const out = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...collect(full));
    else if (entry.name.endsWith('.js')) out.push(full);
  }
  return out;
}

function main() {
  JSON.parse(fs.readFileSync(path.join(ROOT, 'package.json'), 'utf8'));
  console.log('  ok   package.json parses');

  const files = SCAN_DIRS.flatMap((d) => collect(path.join(ROOT, d)));
  let checked = 0;
  for (const file of files) {
    execFileSync(process.execPath, ['--check', file], { stdio: ['ignore', 'pipe', 'inherit'] });
    checked += 1;
  }
  console.log(`  ok   ${checked} source file(s) pass node --check`);
}

main();
