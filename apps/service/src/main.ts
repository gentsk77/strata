/**
 * Executable entry point for the local service.
 *
 * `src/index.ts` is the library entry (importable from tests). This file is the
 * one that actually listens.
 */
import { query } from '@anthropic-ai/claude-agent-sdk';

import { resolvePort } from './config.ts';
import { STATE_DIR } from './paths.ts';
import type { QueryFn } from './runs.ts';
import { buildDeps, createServer, DEFAULT_SERVICE_PORT } from './server.ts';

const port = resolvePort(process.env['BENCH_SERVICE_PORT'], DEFAULT_SERVICE_PORT);

// The SDK's own types are far richer than the handful of fields the supervisor
// reads. The cast is the seam: the supervisor is written against a minimal
// message shape so it stays testable with a fake.
const deps = await buildDeps(STATE_DIR, query as unknown as QueryFn);
const server = createServer({ deps });

/**
 * Say what went wrong in words, not in a Node stack.
 *
 * A stale service holding the port is the single most likely thing to greet
 * whoever runs `npm start` next, and the default behaviour is an unhandled
 * 'error' event: eleven lines of internal stack frames and exit 1. That tells
 * the maintainer nothing about what to do.
 */
server.on('error', (error: NodeJS.ErrnoException) => {
  if (error.code === 'EADDRINUSE') {
    console.error(
      `Port ${port} is already taken, most likely by a bench service that is still running.\n` +
        `  Open http://127.0.0.1:${port} to check, stop that one, or start this on another port:\n` +
        `  BENCH_SERVICE_PORT=5178 npm start`,
    );
  } else {
    console.error(`The service could not start: ${error.message}`);
  }
  process.exit(1);
});

// Loopback only. This is a single-user local tool (design doc requirement 7);
// it has no business being reachable from the network.
server.listen(port, '127.0.0.1', () => {
  console.log(`strata service listening on http://127.0.0.1:${port}`);
});

/**
 * Never die without saying why.
 *
 * An unattended overnight run left this service dead with an empty log, which
 * made the cause unrecoverable after the fact. Whatever kills it next time
 * leaves a stack behind.
 */
process.on('uncaughtException', (error) => {
  console.error('[fatal] uncaught exception:', error);
  process.exit(1);
});
process.on('unhandledRejection', (reason) => {
  console.error('[fatal] unhandled rejection:', reason);
  process.exit(1);
});

for (const signal of ['SIGINT', 'SIGTERM'] as const) {
  process.on(signal, () => {
    server.close(() => {
      process.exit(0);
    });
  });
}
