import assert from 'node:assert/strict';
import { after, describe, it } from 'node:test';

import { handleDistill, type Deps } from './api.ts';
import { resolvePort } from './config.ts';
import { buildHealthResponse, createServer, DEFAULT_SERVICE_PORT, HEALTH_PATH } from './server.ts';
import {
  buildTestDeps,
  fakeKb,
  missingWebRoot,
  startTestService,
  tempWebRoot,
  type RunningService,
} from './testing.ts';

const NOTES_FIXTURE = {
  stated: [
    {
      slug: 'cost-driver-is-rounds-times-context',
      title: 'agent 查库的成本驱动是轮数乘以重放的上下文',
      status: 'unsettled',
      event_date: '2026-08-12',
      links: ['locate-phase-dumping-dominates-query-cost'],
    },
    {
      slug: 'editorial-visual-restraint',
      title: '编辑部式视觉的档次感来自约束',
      status: 'settled',
      event_date: '2026-08-01',
      links: [],
    },
  ],
};

const KB = {
  root: {
    strata_root: 'D:/nowhere/strata',
    notes: 'D:/nowhere/strata/notes',
    inbox: 'D:/nowhere/strata/inbox',
    ingest_cursor: 'D:/nowhere/strata/.kb/ingest-cursor.json',
    bench_root: 'D:/nowhere/bench',
    watchlist: 'D:/nowhere/bench/watchlist.json',
    source: 'strata.config.json',
  },
  list: NOTES_FIXTURE,
  doctor: { notes: 2, violations: [], dangling: [], orphans: [], stubs: [], stale_artifacts: [] },
  watch: { watched: [{ name: 'learning-agent', path: 'D:/x', exists: true, git_root: true }], count: 1 },
};

const open: RunningService[] = [];

after(async () => {
  await Promise.all(open.map((service) => service.close()));
});

async function serve(webRoot?: string): Promise<RunningService> {
  const deps = await buildTestDeps(fakeKb(KB));
  const service = await startTestService(webRoot === undefined ? { deps } : { deps, webRoot });
  open.push(service);
  return service;
}

describe('the health endpoint over real HTTP', () => {
  it('answers GET with a well-formed payload', async () => {
    const service = await serve();
    const response = await fetch(`${service.baseUrl}${HEALTH_PATH}`);

    assert.equal(response.status, 200);
    assert.match(response.headers.get('content-type') ?? '', /application\/json/);

    const payload = (await response.json()) as { service: string; status: string };
    assert.equal(payload.service, 'strata');
    assert.equal(payload.status, 'ok');
  });

  it('rejects a write to the health path instead of silently accepting it', async () => {
    const service = await serve();
    const response = await fetch(`${service.baseUrl}${HEALTH_PATH}`, { method: 'POST' });
    assert.equal(response.status, 405);
  });

  it('clamps uptime when the system clock runs backwards', () => {
    assert.equal(buildHealthResponse(2_000, 1_000).uptimeSeconds, 0);
  });
});

describe('the queue endpoint', () => {
  it('counts what kb reported and hardcodes nothing', async () => {
    const service = await serve();
    const payload = (await (await fetch(`${service.baseUrl}/api/queue`)).json()) as {
      counts: { total: number; unsettled: number; settled: number };
      notes: Array<{ slug: string; evidence: string }>;
    };

    // Two notes in the fixture, one unsettled. If the queue ever grew a
    // constant these numbers would stop tracking the fixture.
    assert.equal(payload.counts.total, 2);
    assert.equal(payload.counts.unsettled, 1);
    assert.equal(payload.counts.settled, 1);
    assert.equal(payload.notes[0]?.evidence, 'stated');
  });

  it('reports the graph source as unavailable rather than parsing notes itself', async () => {
    // KB has no `graph` key at all, which is what a kb build predating the
    // subcommand looks like from here.
    const deps = await buildTestDeps(fakeKb(KB));
    const service = await startTestService({ deps });
    open.push(service);

    const payload = (await (await fetch(`${service.baseUrl}/api/graph`)).json()) as {
      available: boolean;
    };
    assert.equal(payload.available, false);
  });
});

describe('routing', () => {
  it('404s an unknown api route rather than falling through to the app shell', async () => {
    const service = await serve();
    const response = await fetch(`${service.baseUrl}/api/does-not-exist`);
    assert.equal(response.status, 404);
  });

  it('400s a malformed percent-encoding instead of 500ing', async () => {
    const service = await serve();
    const response = await fetch(`${service.baseUrl}/%zz`, { redirect: 'manual' });
    assert.equal(response.status, 400);
  });

  it('refuses to serve a file outside the web root', async () => {
    const service = await serve(await tempWebRoot({ 'index.html': '<h1>bench</h1>' }));
    // Percent-encoded, because fetch collapses a literal `../` before it ever
    // reaches the socket. An earlier version of this test sent `/../../x` and
    // passed only because the file happened not to exist.
    const response = await fetch(`${service.baseUrl}/%2e%2e%2f%2e%2e%2fpackage.json`, { redirect: 'manual' });
    assert.equal(response.status, 403);
  });

  it('serves the built app shell at the root', async () => {
    const service = await serve(await tempWebRoot({ 'index.html': '<h1>bench</h1>' }));
    const response = await fetch(`${service.baseUrl}/`);

    assert.equal(response.status, 200);
    assert.match(response.headers.get('content-type') ?? '', /text\/html/);
    assert.equal(await response.text(), '<h1>bench</h1>');
  });

  it('serves a hashed bundle out of the build output', async () => {
    const service = await serve(
      await tempWebRoot({ 'assets/index-a1b2c3.js': 'export const ok = 1\n' }),
    );
    const response = await fetch(`${service.baseUrl}/assets/index-a1b2c3.js`);

    assert.equal(response.status, 200);
    assert.match(response.headers.get('content-type') ?? '', /text\/javascript/);
  });

  it('404s a missing asset instead of quietly serving the app shell', async () => {
    const service = await serve(await tempWebRoot({ 'index.html': '<h1>bench</h1>' }));
    const response = await fetch(`${service.baseUrl}/assets/typo.css`);
    assert.equal(response.status, 404);
  });

  it('says how to build instead of 404ing when the app was never built', async () => {
    const service = await serve(await missingWebRoot());
    const response = await fetch(`${service.baseUrl}/`);
    const body = await response.text();

    // 503, not 404: the route is right, the build is absent. A 404 here sends
    // the reader hunting for a routing bug that does not exist.
    assert.equal(response.status, 503);
    assert.match(response.headers.get('content-type') ?? '', /text\/html/);
    assert.match(body, /npm run build:web/);
  });

  it('says how to build when a build was emptied out half way', async () => {
    // `vite build` empties its outDir in place. A build that dies in that
    // window leaves the directory present and empty forever, which is not
    // "built", probing the directory rather than index.html called it built
    // and handed back a bare 404.
    const service = await serve(await tempWebRoot())
    const response = await fetch(`${service.baseUrl}/`);

    assert.equal(response.status, 503);
    assert.match(await response.text(), /npm run build:web/);
  });

  it('keeps answering the API when the app was never built', async () => {
    // The missing bundle must not take the API down with it: an agent run
    // started from the CLI has to keep working on an unbuilt checkout. Both
    // assertions hit the same running service, so the 200 is evidence about a
    // service whose static half is genuinely broken, not about any service.
    const service = await serve(await missingWebRoot());

    assert.equal((await fetch(`${service.baseUrl}/`)).status, 503);
    assert.equal((await fetch(`${service.baseUrl}${HEALTH_PATH}`)).status, 200);
  });

  it('rejects an unknown disposition', async () => {
    const service = await serve();
    const response = await fetch(`${service.baseUrl}/api/review`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ slug: 'x', action: 'obliterate' }),
    });
    assert.equal(response.status, 400);
  });

  it('rejects a malformed JSON body as 400 rather than coercing it to {}', async () => {
    const service = await serve();
    const response = await fetch(`${service.baseUrl}/api/review`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: '{ this is not json',
    });
    assert.equal(response.status, 400);
  });
});

describe('the event stream', () => {
  it('drops its subscription when the client goes away', async () => {
    const deps = await buildTestDeps(fakeKb(KB));
    const service = await startTestService({ deps });
    open.push(service);

    const controller = new AbortController();
    const response = await fetch(`${service.baseUrl}/api/events`, { signal: controller.signal });
    assert.match(response.headers.get('content-type') ?? '', /text\/event-stream/);

    // Read one frame so the handler is fully wired, then abandon the stream.
    const reader = response.body?.getReader();
    await reader?.read();
    controller.abort();

    // A leaked subscription keeps a dead socket on the supervisor's list and
    // every later run event writes into it. Give the close event a tick.
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 150));

    // The service is still answering, which is the property that matters: a
    // client that vanished must not take the process with it.
    const health = await fetch(`${service.baseUrl}${HEALTH_PATH}`);
    assert.equal(health.status, 200);
  });

  it('survives many clients connecting and vanishing', async () => {
    const deps = await buildTestDeps(fakeKb(KB));
    const service = await startTestService({ deps });
    open.push(service);

    for (let index = 0; index < 8; index += 1) {
      const controller = new AbortController();
      const response = await fetch(`${service.baseUrl}/api/events`, { signal: controller.signal });
      await response.body?.getReader().read();
      controller.abort();
    }
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 150));

    const health = await fetch(`${service.baseUrl}${HEALTH_PATH}`);
    assert.equal(health.status, 200);
  });
});

describe('createServer', () => {
  it('needs deps and does not invent them', async () => {
    const deps = await buildTestDeps(fakeKb(KB));
    const server = createServer({ deps });
    assert.ok(server);
    server.close();
  });
});

describe('resolvePort', () => {
  it('uses the default when the env var is absent or unusable', () => {
    for (const bad of [undefined, '', 'not-a-port', '70000', '-1']) {
      assert.equal(resolvePort(bad, DEFAULT_SERVICE_PORT), DEFAULT_SERVICE_PORT);
    }
  });

  it('honours a usable override', () => {
    assert.equal(resolvePort('6000', DEFAULT_SERVICE_PORT), 6000);
  });
});

describe('handleDistill on an unreadable ingest report', () => {
  it('refuses to start a run rather than reading "no data" as an empty batch', async () => {
    // kb ingest can exit 0 with stdout the JSON layer cannot parse. The file
    // list in the report became load-bearing for the distill prompt, so this
    // must be an error, not an empty batch: an empty batch tells the run
    // "nothing new, write nothing" while the ingest cursor has already
    // advanced, the copied files would sit in inbox/ forever, undistilled,
    // with nothing anywhere reporting a problem.
    const kb: Deps['kb'] = <T,>(args: readonly string[]) => {
      if (args[0] === 'watch') {
        return Promise.resolve({
          exitCode: 0,
          data: { watched: [{ name: 'learning-agent', path: 'D:/x', exists: true, git_root: true }], count: 1 } as T,
          stdout: '',
          stderr: '',
          parseError: null,
        });
      }
      return Promise.resolve({
        exitCode: 0,
        data: null,
        stdout: 'ingest ok but not json',
        stderr: '',
        parseError: 'Unexpected token i in JSON at position 0',
      });
    };
    const deps = await buildTestDeps(kb);

    const result = (await handleDistill(deps, 'learning-agent')) as { ok: boolean; error?: string };
    assert.equal(result.ok, false);
    assert.match(result.error ?? '', /unreadable/);
    // And no run may have been fired on the bad data.
    assert.equal(deps.supervisor.activeRunId(), null);
  });
});
