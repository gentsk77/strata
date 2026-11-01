import { mkdir, mkdtemp, writeFile } from 'node:fs/promises';
import type { AddressInfo } from 'node:net';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';

import type { Deps } from './api.ts';
import type { KbInvocation } from './kb.ts';
import { RunSupervisor, type QueryFn } from './runs.ts';
import { StateStore } from './state.ts';
import { createServer, type CreateServerOptions } from './server.ts';

export interface RunningService {
  /** e.g. `http://127.0.0.1:51234`, no trailing slash. */
  readonly baseUrl: string;
  readonly close: () => Promise<void>;
}

export async function startTestService(
  options: Omit<CreateServerOptions, 'deps'> & { deps: Deps },
): Promise<RunningService> {
  const server = createServer(options);

  await new Promise<void>((resolvePromise, reject) => {
    server.once('error', reject);
    // Port 0 asks the OS for a free port, so concurrent test files never
    // collide and nothing depends on the developer's machine being quiet.
    server.listen(0, '127.0.0.1', resolvePromise);
  });

  const address = server.address() as AddressInfo;

  return {
    baseUrl: `http://127.0.0.1:${address.port}`,
    close: () =>
      new Promise<void>((resolvePromise, reject) => {
        server.close((error) => (error ? reject(error) : resolvePromise()));
      }),
  };
}

/** A `.bench-state` directory in the OS temp area, fresh per test. */
export async function tempStateDir(): Promise<string> {
  return mkdtemp(join(tmpdir(), 'bench-state-'));
}

/**
 * A stand-in for `apps/web/dist`, fresh per test.
 *
 * Static-serving tests must not depend on whether `npm run build:web` has run
 * on this machine. Before the web root moved to the gitignored build output
 * they passed either way by accident; now an unbuilt checkout answers 503, so
 * any test asserting a 404 or a served file has to own its own directory.
 */
export async function tempWebRoot(files: Record<string, string> = {}): Promise<string> {
  const root = await mkdtemp(join(tmpdir(), 'bench-web-'));
  for (const [relative, contents] of Object.entries(files)) {
    const target = join(root, relative);
    await mkdir(dirname(target), { recursive: true });
    await writeFile(target, contents, 'utf8');
  }
  return root;
}

/**
 * A path that provably does not exist: a child of a directory just created.
 *
 * Not a fixed name under `tmpdir()`. On Linux and macOS that is `/tmp`, shared
 * by every user on the box, so anything that ever created the same name would
 * flip the "never built" tests onto the 404 path and fail them with no clue
 * why. `mkdtemp` gives a directory nobody else holds; its child cannot exist.
 */
export async function missingWebRoot(): Promise<string> {
  return join(await mkdtemp(join(tmpdir(), 'bench-web-')), 'never-built');
}

/**
 * A stand-in for the `kb` command line.
 *
 * Tests that exercise routing and state must not depend on a real strata
 * checkout existing, or on its note count today.
 */
export function fakeKb(responses: Record<string, unknown>): Deps['kb'] {
  const runner = <T,>(args: readonly string[]): Promise<KbInvocation<T>> => {
    const key = args.join(' ');
    const match = Object.keys(responses).find((candidate) => key.startsWith(candidate));
    if (match === undefined) {
      return Promise.resolve({
        exitCode: 2,
        data: null,
        stdout: '',
        stderr: `fakeKb has no answer for "${key}"`,
        parseError: null,
      });
    }
    const data = responses[match] as T;
    return Promise.resolve({
      exitCode: 0,
      data,
      stdout: JSON.stringify(data),
      stderr: '',
      parseError: null,
    });
  };
  return runner as Deps['kb'];
}

/** A `query` that replays a scripted message list, for testing the supervisor. */
export function fakeQuery(messages: ReadonlyArray<Record<string, unknown>>, throwAfter = false): QueryFn {
  return () => ({
    async *[Symbol.asyncIterator]() {
      for (const message of messages) {
        yield message as { type: string };
      }
      if (throwAfter) {
        // Exactly what the real SDK does: emit the result, then throw.
        throw new Error('Claude Code returned an error result: Reached maximum number of turns');
      }
    },
  });
}

export async function buildTestDeps(
  kb: Deps['kb'],
  query: QueryFn = fakeQuery([]),
): Promise<Deps & { stateDir: string }> {
  const stateDir = await tempStateDir();
  const store = await StateStore.open(stateDir);
  return { store, supervisor: new RunSupervisor(store, query), kb, stateDir };
}
