#!/usr/bin/env node
// Runs the Python suite through whichever interpreter this machine actually has.
//
// Why this exists instead of a bare `python3 tests/run_all.py` in package.json:
// the two machines this repo runs on disagree. macOS (homebrew) has no bare
// `python`; Windows has `python`, and its `python3` is a Windows Store alias
// that sits on PATH and fails the moment you run it. Either name hardcoded
// breaks `npm test` on one of the two. The resolution order matches
// `apps/service/src/paths.ts` so the app and the test gate never disagree
// about which interpreter is "the" one.
import { spawnSync } from 'node:child_process';

const candidates = [
  process.env['BENCH_PYTHON'],
  process.platform === 'win32' ? 'python' : 'python3',
  process.platform === 'win32' ? 'python3' : 'python',
].filter(Boolean);

for (const bin of candidates) {
  // A probe, not a PATH lookup: the Store alias exists but cannot execute.
  if (spawnSync(bin, ['-c', ''], { stdio: 'ignore' }).status !== 0) continue;
  const run = spawnSync(bin, ['tests/run_all.py'], { stdio: 'inherit' });
  process.exit(run.status ?? 1);
}

console.error(
  `no working Python found (tried: ${candidates.join(', ')}). ` +
    'Set BENCH_PYTHON to the interpreter you want the test gate to use.',
);
process.exit(1);
