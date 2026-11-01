import { resolve } from 'node:path';

/**
 * Every path the service needs, derived from this file's own location.
 *
 * Deliberately not from `process.cwd()`: the service gets started from an npm
 * script, from a shell in a subdirectory, and from a test runner, and all three
 * must agree on where the repo is.
 */
const HERE = import.meta.dirname; // apps/service/src

/** The repo root. Also the cwd every agent run gets. */
export const REPO_ROOT = resolve(HERE, '..', '..', '..');

/**
 * Static assets the service serves to the browser: Vite's build output.
 *
 * Not `apps/web` itself. Since ruling D11 that directory holds TypeScript and
 * JSX sources that no browser can execute; serving it would hand the browser
 * `main.tsx` and produce a blank page with a MIME error. The built bundle is
 * gitignored, so it may legitimately not exist yet, `serveStatic` says so in
 * words rather than 404ing (`npm start` builds first, but `npm run dev` does
 * not, and neither does running this file directly).
 */
export const WEB_ROOT = resolve(REPO_ROOT, 'apps', 'web', 'dist');

/**
 * App state: review progress, comment threads, run records, budget.
 *
 * Gitignored, and it lives here rather than in strata on purpose, process
 * state is not knowledge and must not pollute strata's git history (ruling D2,
 * design doc section 2).
 */
export const STATE_DIR = resolve(REPO_ROOT, '.bench-state');

/**
 * The Python interpreter used to run `kb`. Overridable for odd installs.
 *
 * macOS and Linux homebrew ship only `python3`; Windows is the reverse and
 * has no `python3` by default. The platform default keeps `BENCH_PYTHON` as
 * the escape hatch for anything this heuristic gets wrong.
 */
export const PYTHON =
  process.env['BENCH_PYTHON'] ?? (process.platform === 'win32' ? 'python' : 'python3');
