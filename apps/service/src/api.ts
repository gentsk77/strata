import {
  copiedPaths,
  flattenList,
  runKb,
  type KbDoctor,
  type KbIngest,
  type KbList,
  type KbRoots,
  type KbWatch,
} from './kb.ts';
import { readNote } from './notes.ts';
import { blobHash } from './notes.ts';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import type { RunSupervisor } from './runs.ts';
import { distillPrompt, rewritePrompt, settlePrompt } from './runs.ts';
import type { Budget, Comment, ReviewRecord, StateStore } from './state.ts';

/**
 * The service's application logic, one function per endpoint.
 *
 * Split from the HTTP plumbing in `server.ts` so each handler is callable from
 * a test without a socket, and so the routing table reads as a table.
 */

export interface Deps {
  readonly store: StateStore;
  readonly supervisor: RunSupervisor;
  /** Injectable so tests do not need the real knowledge base to exist. */
  readonly kb: typeof runKb;
}

export interface QueueEntry {
  readonly slug: string;
  readonly title: string;
  readonly status: string;
  readonly evidence: string;
  readonly eventDate: string;
  readonly blob: string;
  readonly links: readonly string[];
  /** null when the maintainer has not judged this exact version. */
  readonly review: ReviewRecord | null;
  /** True when a judgment exists but was made against different note text. */
  readonly reviewStale: boolean;
  readonly commentCount: number;
}

/**
 * Builds the review queue.
 *
 * The inventory comes from `kb list --json`, never from parsing `notes/`
 * (ruling D16). The note files are read only to hash them, because the review
 * key is slug plus git blob hash and the hash has to come from the bytes.
 *
 * There is no count anywhere in this function. How many notes are unsettled is
 * measured at runtime, every time, and changes the moment a distill lands.
 */
export async function buildQueue(
  deps: Deps,
  notesDir: string,
): Promise<{ entries: QueueEntry[]; blobs: Map<string, string> }> {
  const listed = await deps.kb<KbList>(['list']);
  if (listed.data === null) {
    throw new Error(`kb list failed (exit ${listed.exitCode}): ${listed.stderr || listed.parseError || 'no output'}`);
  }

  const flat = flattenList(listed.data);
  const blobs = new Map<string, string>();
  const entries: QueueEntry[] = [];

  for (const note of flat) {
    let blob = '';
    try {
      const bytes = await readFile(join(notesDir, `${note.slug}.md`));
      blob = blobHash(bytes);
    } catch {
      // A note kb knows about but we cannot read is a real condition worth
      // showing rather than crashing the whole queue on.
      blob = '';
    }
    blobs.set(note.slug, blob);

    const review = deps.store.reviewFor(note.slug);
    entries.push({
      slug: note.slug,
      title: note.title,
      status: note.status,
      evidence: note.evidence,
      eventDate: note.event_date,
      blob,
      links: note.links,
      review: review !== null && review.blob === blob ? review : null,
      reviewStale: review !== null && review.blob !== blob,
      commentCount: deps.store.thread(note.slug).length,
    });
  }

  return { entries, blobs };
}

export async function getRoots(deps: Deps): Promise<KbRoots> {
  const roots = await deps.kb<KbRoots>(['root']);
  if (roots.data === null) {
    throw new Error(
      `kb cannot find strata (exit ${roots.exitCode}). ${roots.stderr || roots.stdout || ''}`.trim(),
    );
  }
  return roots.data;
}

export async function handleConfig(deps: Deps): Promise<unknown> {
  const [roots, watch] = await Promise.all([getRoots(deps), deps.kb<KbWatch>(['watch'])]);
  return {
    roots,
    watchlist: watch.data?.watched ?? [],
    budget: deps.store.budget(),
    spend: deps.store.spend(),
    activeRunId: deps.supervisor.activeRunId(),
  };
}

export async function handleQueue(deps: Deps): Promise<unknown> {
  const roots = await getRoots(deps);
  const { entries, blobs } = await buildQueue(deps, roots.notes);

  const unsettled = entries.filter((e) => e.status === 'unsettled');
  return {
    notes: entries,
    unsettled,
    counts: {
      total: entries.length,
      unsettled: unsettled.length,
      settled: entries.length - unsettled.length,
      approvedPending: deps.store.pending('approve', blobs).length,
      rejectedPending: deps.store.pending('reject', blobs).length,
    },
  };
}

export async function handleNote(deps: Deps, slug: string): Promise<unknown> {
  const roots = await getRoots(deps);
  const note = await readNote(roots.notes, slug);
  const review = deps.store.reviewFor(slug);
  return {
    slug,
    blob: note.blob,
    frontMatter: note.frontMatter,
    body: note.body,
    markdown: note.markdown,
    review: review !== null && review.blob === note.blob ? review : null,
    reviewStale: review !== null && review.blob !== note.blob,
    thread: deps.store.thread(slug),
  };
}

export async function handleDoctor(deps: Deps): Promise<unknown> {
  const doctor = await deps.kb<KbDoctor>(['doctor']);
  if (doctor.data === null) {
    return { ok: false, unavailable: true, reason: doctor.stderr || 'kb doctor produced no JSON' };
  }
  return { ok: doctor.data.violations.length === 0, ...doctor.data };
}

export async function handleGraph(deps: Deps): Promise<unknown> {
  const graph = await deps.kb<unknown>(['graph']);
  // `== null` on purpose: a kb build without the subcommand yields no data at
  // all, and both spellings of "nothing" have to reach the same branch.
  if (graph.data == null) {
    // `kb graph --json` is the graph pane's only data source (ruling D16). The
    // app does NOT fall back to parsing notes/ to draw a graph; it says the
    // source is missing and shows nothing.
    return {
      available: false,
      reason: graph.stderr.trim() || `kb graph exited ${graph.exitCode} without JSON`,
    };
  }
  return { available: true, ...(graph.data as Record<string, unknown>) };
}

export interface ReviewRequest {
  readonly slug: string;
  readonly action: 'approve' | 'reject' | 'skip';
  readonly comment?: string;
}

/**
 * Records one disposition.
 *
 * Note what this does NOT do: it does not touch the note. Approving is the
 * maintainer's judgment being written into app state, and nothing more. The
 * note's `status` field is flipped later, by a settle run, or never.
 */
export async function handleReview(deps: Deps, body: ReviewRequest): Promise<unknown> {
  const roots = await getRoots(deps);
  const note = await readNote(roots.notes, body.slug);
  const at = new Date().toISOString();

  if (body.action === 'skip') {
    await deps.store.clearReview(body.slug);
    return { ok: true, slug: body.slug, action: 'skip' };
  }

  if (body.action === 'reject') {
    const text = (body.comment ?? '').trim();
    if (text === '') {
      return { ok: false, error: 'a decline needs a comment saying what is wrong' };
    }
    await deps.store.appendComment(body.slug, {
      at,
      author: 'maintainer',
      text,
      blob: note.blob,
    });
  }

  const record: ReviewRecord = {
    slug: body.slug,
    blob: note.blob,
    action: body.action,
    at,
    dispatchedRunId: null,
  };
  await deps.store.recordReview(record);
  return { ok: true, slug: body.slug, action: body.action, review: record };
}

export async function handleDistill(deps: Deps, source: string, model?: string): Promise<unknown> {
  const watch = await deps.kb<KbWatch>(['watch']);
  const known = (watch.data?.watched ?? []).some((entry) => entry.name === source);
  if (!known) {
    return { ok: false, error: `"${source}" is not on the watchlist` };
  }

  // Mechanical copy first, no LLM. This is the one write the app itself is
  // allowed to cause in strata, and it only touches gitignored inbox/ and the
  // ingest cursor.
  const ingest = await deps.kb<KbIngest>(['ingest', source]);
  if (ingest.exitCode !== 0) {
    return { ok: false, error: `kb ingest ${source} failed: ${ingest.stderr || ingest.stdout}` };
  }
  // A report we cannot read is as bad as a failed exit, and worse here than
  // elsewhere: the file list below is load-bearing. Reading "no data" as an
  // empty batch would hand the run the "nothing new, stop" prompt while the
  // ingest cursor has already advanced, the copied files would sit in inbox/
  // forever with nothing anywhere reporting an error.
  if (ingest.data === null) {
    return {
      ok: false,
      error: `kb ingest ${source} exited 0 but its report was unreadable: ${ingest.parseError ?? 'no output'}. The cursor may have advanced; check strata's inbox/ before retrying.`,
    };
  }

  const started = await deps.supervisor.start({
    kind: 'distill',
    subject: source,
    slugs: [],
    // What this round actually copied, not just the source name. inbox/ is
    // never emptied, so the source name alone does not identify a batch; the
    // reasoning is on `distillPrompt`.
    prompt: distillPrompt(source, copiedPaths(ingest.data, source)),
    ...(model !== undefined ? { model } : {}),
  });
  return 'error' in started ? { ok: false, ...started } : { ok: true, ...started, ingest: ingest.data };
}

export async function handleSettle(deps: Deps, model?: string): Promise<unknown> {
  const roots = await getRoots(deps);
  const { blobs } = await buildQueue(deps, roots.notes);
  const pending = deps.store.pending('approve', blobs);
  if (pending.length === 0) {
    return { ok: false, error: 'nothing is approved and waiting' };
  }

  const slugs = pending.map((r) => r.slug);
  const started = await deps.supervisor.start({
    kind: 'settle',
    subject: `${slugs.length} approved`,
    slugs,
    prompt: settlePrompt(slugs),
    ...(model !== undefined ? { model } : {}),
  });
  if ('error' in started) return { ok: false, ...started };
  await deps.store.markDispatched(slugs, started.runId);
  return { ok: true, ...started, slugs };
}

export async function handleRewrite(deps: Deps, model?: string): Promise<unknown> {
  const roots = await getRoots(deps);
  const { blobs } = await buildQueue(deps, roots.notes);
  const pending = deps.store.pending('reject', blobs);
  if (pending.length === 0) {
    return { ok: false, error: 'nothing is declined and waiting' };
  }

  const threads = pending.map((r) => ({
    slug: r.slug,
    comments: deps.store.thread(r.slug) as readonly Comment[],
  }));
  const slugs = pending.map((r) => r.slug);

  const started = await deps.supervisor.start({
    kind: 'rewrite',
    subject: `${slugs.length} declined`,
    slugs,
    prompt: rewritePrompt(threads),
    ...(model !== undefined ? { model } : {}),
  });
  if ('error' in started) return { ok: false, ...started };
  await deps.store.markDispatched(slugs, started.runId);
  return { ok: true, ...started, slugs };
}

export async function handleBudget(deps: Deps, body: Partial<Budget>): Promise<unknown> {
  const current = deps.store.budget();
  const maxTurns = Number(body.maxTurns ?? current.maxTurns);
  const maxCostUsd = Number(body.maxCostUsd ?? current.maxCostUsd);
  if (!Number.isFinite(maxTurns) || maxTurns < 1) {
    return { ok: false, error: 'the turn ceiling has to be at least 1' };
  }
  if (!Number.isFinite(maxCostUsd) || maxCostUsd <= 0) {
    return { ok: false, error: 'the cost ceiling has to be above zero' };
  }
  const budget: Budget = { maxTurns: Math.floor(maxTurns), maxCostUsd };
  await deps.store.setBudget(budget);
  return { ok: true, budget };
}
