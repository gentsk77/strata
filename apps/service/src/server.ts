import { createServer as createHttpServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http';
import { access, readFile } from 'node:fs/promises';
import { extname, normalize, resolve, sep } from 'node:path';

import * as api from './api.ts';
import type { Deps } from './api.ts';
import { runKb } from './kb.ts';
import { WEB_ROOT } from './paths.ts';
import { RunSupervisor, type QueryFn } from './runs.ts';
import { StateStore } from './state.ts';

export const SERVICE_VERSION = '0.1.0';
export const HEALTH_PATH = '/api/health';
export const DEFAULT_SERVICE_PORT = 5177;

export interface HealthResponse {
  readonly service: 'strata';
  readonly status: 'ok';
  readonly version: string;
  readonly uptimeSeconds: number;
}

export interface CreateServerOptions {
  readonly startedAt?: number;
  readonly now?: () => number;
  readonly deps: Deps;
  /** Static root. Overridable so a test can serve a fixture directory. */
  readonly webRoot?: string;
}

export function buildHealthResponse(startedAt: number, now: number): HealthResponse {
  return {
    service: 'strata',
    status: 'ok',
    version: SERVICE_VERSION,
    uptimeSeconds: Math.max(0, (now - startedAt) / 1000),
  };
}

/** Wires a real state store, a real supervisor and the real kb bridge. */
export async function buildDeps(stateDir: string, query: QueryFn): Promise<Deps> {
  const store = await StateStore.open(stateDir);
  return { store, supervisor: new RunSupervisor(store, query), kb: runKb };
}

/**
 * The local service's HTTP surface.
 *
 * Loopback only, single user, no auth: the design says this is not a product
 * (design doc requirement 7). Two halves, a small JSON API under `/api`, and
 * the static browser app served from Vite's build output, `apps/web/dist`.
 */
export function createServer(options: CreateServerOptions): Server {
  const now = options.now ?? Date.now;
  const startedAt = options.startedAt ?? now();
  const webRoot = options.webRoot ?? WEB_ROOT;
  const { deps } = options;

  return createHttpServer((request, response) => {
    void route(request, response, deps, webRoot, startedAt, now).catch((error: unknown) => {
      // Every unhandled failure becomes a readable 500 rather than a hung
      // socket. A local tool that silently stops answering is worse than one
      // that says what broke.
      const message = error instanceof Error ? error.message : String(error);
      if (!response.headersSent) sendJson(response, 500, { error: message });
      else response.end();
    });
  });
}

async function route(
  request: IncomingMessage,
  response: ServerResponse,
  deps: Deps,
  webRoot: string,
  startedAt: number,
  now: () => number,
): Promise<void> {
  const url = new URL(request.url ?? '/', 'http://127.0.0.1');
  const path = url.pathname;
  const method = request.method ?? 'GET';

  if (path === HEALTH_PATH) {
    if (method !== 'GET' && method !== 'HEAD') {
      sendJson(response, 405, { error: 'method not allowed', allow: 'GET' });
      return;
    }
    sendJson(response, 200, buildHealthResponse(startedAt, now()));
    return;
  }

  if (path === '/api/events') {
    streamEvents(request, response, deps);
    return;
  }

  if (path.startsWith('/api/')) {
    await apiRoute(request, response, deps, path, method, url);
    return;
  }

  await serveStatic(response, webRoot, path);
}

async function apiRoute(
  request: IncomingMessage,
  response: ServerResponse,
  deps: Deps,
  path: string,
  method: string,
  url: URL,
): Promise<void> {
  if (method === 'GET') {
    switch (path) {
      case '/api/config':
        sendJson(response, 200, await api.handleConfig(deps));
        return;
      case '/api/queue':
        sendJson(response, 200, await api.handleQueue(deps));
        return;
      case '/api/doctor':
        sendJson(response, 200, await api.handleDoctor(deps));
        return;
      case '/api/graph':
        sendJson(response, 200, await api.handleGraph(deps));
        return;
      case '/api/runs':
        sendJson(response, 200, { runs: deps.store.runs(), activeRunId: deps.supervisor.activeRunId() });
        return;
      default:
        break;
    }

    const note = matchPrefix(path, '/api/notes/');
    if (note !== null) {
      try {
        sendJson(response, 200, await api.handleNote(deps, note));
      } catch (error) {
        sendJson(response, 404, { error: `no note ${note}`, detail: String(error) });
      }
      return;
    }

    const run = matchPrefix(path, '/api/runs/');
    if (run !== null) {
      const record = deps.store.run(run);
      if (record === null) sendJson(response, 404, { error: `no run ${run}` });
      else sendJson(response, 200, record);
      return;
    }
  }

  if (method === 'POST' || method === 'PUT') {
    let body: Record<string, unknown>;
    try {
      body = await readJsonBody(request);
    } catch (error) {
      sendJson(response, 400, { error: error instanceof Error ? error.message : 'invalid JSON body' });
      return;
    }
    const model = typeof body['model'] === 'string' ? body['model'] : undefined;

    switch (path) {
      case '/api/review': {
        const slug = String(body['slug'] ?? '');
        const action = String(body['action'] ?? '');
        if (action !== 'approve' && action !== 'reject' && action !== 'skip') {
          sendJson(response, 400, { error: `unknown disposition "${action}"` });
          return;
        }
        const comment = body['comment'];
        sendJson(
          response,
          200,
          await api.handleReview(deps, {
            slug,
            action,
            ...(typeof comment === 'string' ? { comment } : {}),
          }),
        );
        return;
      }
      case '/api/runs/distill':
        sendJson(response, 200, await api.handleDistill(deps, String(body['source'] ?? ''), model));
        return;
      case '/api/runs/settle':
        sendJson(response, 200, await api.handleSettle(deps, model));
        return;
      case '/api/runs/rewrite':
        sendJson(response, 200, await api.handleRewrite(deps, model));
        return;
      case '/api/runs/stop':
        sendJson(response, 200, { ok: deps.supervisor.stop() });
        return;
      case '/api/budget':
        sendJson(response, 200, await api.handleBudget(deps, body as Record<string, number>));
        return;
      default:
        break;
    }
  }

  sendJson(response, 404, { error: 'not found', path, method, query: url.search });
}

/**
 * Server-sent events for live run telemetry.
 *
 * SSE rather than a websocket because the traffic is one-way and SSE needs no
 * dependency: `EventSource` is already in the browser.
 */
function streamEvents(request: IncomingMessage, response: ServerResponse, deps: Deps): void {
  response.writeHead(200, {
    'content-type': 'text/event-stream; charset=utf-8',
    'cache-control': 'no-cache',
    connection: 'keep-alive',
  });
  // Declared before anything can call close(). `write` below may close
  // synchronously on an already-dead socket, and a `const` captured by that
  // path would still be in its temporal dead zone.
  let closed = false;
  let unsubscribe: (() => void) | null = null;
  let heartbeat: NodeJS.Timeout | null = null;

  // Idempotent on the RESOURCES, not on a flag. A flag-guarded close that runs
  // before the interval and subscription exist would mark itself done and then
  // leak both of them, which is how a "closed" stream keeps a run's events
  // alive forever.
  const close = (): void => {
    closed = true;
    if (heartbeat !== null) {
      clearInterval(heartbeat);
      heartbeat = null;
    }
    if (unsubscribe !== null) {
      unsubscribe();
      unsubscribe = null;
    }
  };

  // A dead client must never take the service with it. A browser tab that goes
  // away mid-run leaves a socket that errors on the next write, and an
  // unhandled 'error' on a stream is a process-level crash.
  request.on('error', close);
  response.on('error', close);
  request.on('close', close);
  response.on('close', close);

  const write = (frame: string): void => {
    if (closed || response.writableEnded || response.destroyed) {
      close();
      return;
    }
    try {
      response.write(frame);
    } catch {
      close();
    }
  };

  write(`: connected\n\n`);

  unsubscribe = deps.supervisor.subscribe((event) => {
    write(`data: ${JSON.stringify(event)}\n\n`);
  });

  // A comment frame every 20s keeps intermediaries from reaping an idle stream
  // while a long distill is thinking.
  heartbeat = setInterval(() => write(`: ping\n\n`), 20_000);

  // A client that died during the two statements above never fires another
  // event, so settle up now.
  if (closed) close();
}

const MIME: Record<string, string> = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.woff2': 'font/woff2',
  '.ico': 'image/x-icon',
};

async function serveStatic(response: ServerResponse, webRoot: string, path: string): Promise<void> {
  let relative: string;
  try {
    relative = path === '/' ? 'index.html' : decodeURIComponent(path).replace(/^\/+/, '');
  } catch {
    // A malformed percent sequence is a client error, not a 500.
    sendJson(response, 400, { error: 'malformed URL encoding', path });
    return;
  }
  const target = resolve(webRoot, normalize(relative));

  // Path traversal guard. Loopback-only is not a reason to serve the whole disk.
  if (target !== webRoot && !target.startsWith(webRoot + sep)) {
    sendJson(response, 403, { error: 'outside the web root' });
    return;
  }

  try {
    const bytes = await readFile(target);
    response.writeHead(200, {
      'content-type': MIME[extname(target).toLowerCase()] ?? 'application/octet-stream',
      // A local dev tool with a cached stale bundle wastes more time than it saves.
      'cache-control': 'no-store',
    });
    response.end(bytes);
  } catch {
    // "No build here" is a different failure from "that one file is missing":
    // the app was never built, or a build died half way. A bare 404 sends the
    // maintainer looking for a routing bug that is not there, so say what to
    // run instead.
    //
    // The probe is `index.html`, not the directory. `vite build` empties its
    // outDir in place rather than removing it, so between the emptying and the
    // first file written, and permanently, if the build fails in that window,
    // `apps/web/dist` exists and is useless. Testing the directory called that
    // state "built" and fell through to the 404 this branch exists to remove.
    if (!(await exists(resolve(webRoot, 'index.html')))) {
      sendBuildMissing(response, webRoot);
      return;
    }
    // A real 404, deliberately not an index.html fallback. This app has no
    // client-side routing, so every URL should resolve to a real file. Serving
    // the shell for a missing asset turns a typo in a stylesheet path into a
    // page that half-works, which is far harder to diagnose than a 404.
    sendJson(response, 404, { error: 'not found', path });
  }
}

async function exists(path: string): Promise<boolean> {
  try {
    await access(path);
    return true;
  } catch {
    return false;
  }
}

/**
 * 503, not 404: the route is right, the build is absent.
 *
 * Answers in HTML because the reader is a browser tab showing a blank page,
 * and in plain words because "ENOENT apps/web/dist/index.html" does not tell
 * anyone which command fixes it.
 */
function sendBuildMissing(response: ServerResponse, webRoot: string): void {
  const body =
    '<!doctype html><meta charset="utf-8"><title>strata: not built</title>' +
    '<style>body{font:16px/1.6 system-ui,sans-serif;margin:4rem auto;max-width:34rem;padding:0 1.5rem}' +
    'code{background:#f2f0ec;padding:.15em .4em;border-radius:3px}</style>' +
    '<h1>The web app has not been built yet.</h1>' +
    '<p>The service is running and its API is fine; there is just nothing to serve at ' +
    `<code>${escapeHtml(webRoot)}</code>.</p>` +
    '<p>Build it once, then reload:</p><pre><code>npm run build:web</code></pre>' +
    '<p>Or use the dev loop instead, which serves the UI from Vite on port 5173:</p>' +
    '<pre><code>npm run dev:web</code></pre>';
  response.writeHead(503, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' });
  response.end(body);
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>"]/g, (character) => {
    switch (character) {
      case '&':
        return '&amp;';
      case '<':
        return '&lt;';
      case '>':
        return '&gt;';
      default:
        return '&quot;';
    }
  });
}

function matchPrefix(path: string, prefix: string): string | null {
  if (!path.startsWith(prefix)) return null;
  const rest = path.slice(prefix.length);
  return rest === '' || rest.includes('/') ? null : rest;
}

async function readJsonBody(request: IncomingMessage): Promise<Record<string, unknown>> {
  const chunks: Buffer[] = [];
  for await (const chunk of request) chunks.push(chunk as Buffer);
  const raw = Buffer.concat(chunks).toString('utf8').trim();
  if (raw === '') return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    // A broken body must be loud, not silently coerced to {}: the alternative
    // surfaces as a misleading downstream error ("unknown disposition").
    throw new Error('invalid JSON body');
  }
  if (typeof parsed !== 'object' || parsed === null) {
    throw new Error('JSON body must be an object');
  }
  return parsed as Record<string, unknown>;
}

function sendJson(response: ServerResponse, statusCode: number, body: unknown): void {
  response.writeHead(statusCode, { 'content-type': 'application/json; charset=utf-8' });
  response.end(JSON.stringify(body));
}
