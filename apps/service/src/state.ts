import { mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import { join } from 'node:path';

/**
 * App state, on disk under `.bench-state/` (ruling D2).
 *
 * Everything in here is process state, not knowledge: review progress, comment
 * threads, run records, budget. None of it may reach strata's git history
 * (design doc section 2). The directory is gitignored in this repo too, the
 * bench's own history is not the place for it either.
 *
 * The store is deliberately dumb JSON with whole-file atomic writes. This is a
 * single-user local tool with one writer; a database would be a bigger promise
 * than the problem makes.
 */

export type Disposition = 'approve' | 'reject';

export interface ReviewRecord {
  readonly slug: string;
  /**
   * The note's git blob hash at the moment the maintainer judged it.
   * Half the review key: if the note text changes this no longer matches and
   * the note returns to the queue (design doc section 3.2).
   */
  readonly blob: string;
  readonly action: Disposition;
  readonly at: string;
  /** The run that consumed this judgment. Null while it is still batched up. */
  readonly dispatchedRunId: string | null;
}

export interface Comment {
  readonly at: string;
  readonly author: 'maintainer' | 'agent';
  readonly text: string;
  /** The blob the comment was written against, so a thread stays legible after a rewrite. */
  readonly blob: string;
}

export interface Budget {
  /** Hard turn ceiling handed to the SDK per run. */
  readonly maxTurns: number;
  /** Hard cost ceiling handed to the SDK per run, in estimated USD. */
  readonly maxCostUsd: number;
}

export const DEFAULT_BUDGET: Budget = { maxTurns: 40, maxCostUsd: 2 };

export interface ToolUseRecord {
  readonly id: string;
  readonly name: string;
  readonly input: unknown;
  readonly at: string;
  readonly ok: boolean | null;
  readonly resultSummary: string | null;
}

export type RunKind = 'distill' | 'settle' | 'rewrite';
export type RunStatus = 'running' | 'success' | 'error' | 'stopped';

export interface RunUsage {
  readonly inputTokens: number;
  readonly outputTokens: number;
  readonly cacheReadTokens: number;
  readonly cacheCreationTokens: number;
}

export interface RunRecord {
  readonly id: string;
  readonly kind: RunKind;
  readonly status: RunStatus;
  readonly startedAt: string;
  readonly endedAt: string | null;
  /** What the run was pointed at: a watchlist source, or the batched slugs. */
  readonly subject: string;
  readonly slugs: readonly string[];
  readonly sessionId: string | null;
  readonly numTurns: number;
  readonly usage: RunUsage;
  /**
   * SDK list-price estimate, NOT an invoice. This account is a Max
   * subscription, so no dollar figure here was ever billed. Always rendered as
   * "estimated" in the UI.
   */
  readonly estimatedCostUsd: number;
  readonly durationMs: number;
  readonly durationApiMs: number;
  readonly toolUses: readonly ToolUseRecord[];
  readonly modelUsage: Readonly<Record<string, unknown>>;
  readonly permissionDenials: readonly unknown[];
  /** Why it stopped: SDK `subtype` plus `terminal_reason`. */
  readonly terminalReason: string | null;
  readonly errors: readonly string[];
  /** Notes that were unsettled after the run but not before it. */
  readonly producedSlugs: readonly string[];
  /** doctor's verdict after the run. Null until the gate ran. */
  readonly doctorOk: boolean | null;
  readonly rateLimit: unknown | null;
  readonly transcript: readonly string[];
}

interface StateShape {
  reviews: Record<string, ReviewRecord>;
  threads: Record<string, Comment[]>;
  runs: RunRecord[];
  budget: Budget;
}

const EMPTY: StateShape = { reviews: {}, threads: {}, runs: [], budget: DEFAULT_BUDGET };

/**
 * Reads and writes `.bench-state/`.
 *
 * Loaded once at construction and held in memory, then written through on
 * every mutation. A restart re-reads from disk, which is the property the
 * acceptance bar cares about: an approval or a comment must survive a service
 * restart.
 */
export class StateStore {
  readonly #dir: string;
  #state: StateShape = structuredClone(EMPTY);

  constructor(dir: string) {
    this.#dir = dir;
  }

  static async open(dir: string): Promise<StateStore> {
    const store = new StateStore(dir);
    await store.load();
    return store;
  }

  async load(): Promise<void> {
    await mkdir(this.#dir, { recursive: true });
    const loaded = structuredClone(EMPTY);
    loaded.reviews = await this.#readFile('reviews.json', EMPTY.reviews);
    loaded.threads = await this.#readFile('threads.json', EMPTY.threads);
    loaded.runs = await this.#readFile('runs.json', EMPTY.runs);
    loaded.budget = await this.#readFile('budget.json', DEFAULT_BUDGET);
    this.#state = loaded;
  }

  async #readFile<T>(name: string, fallback: T): Promise<T> {
    try {
      const raw = await readFile(join(this.#dir, name), 'utf8');
      return JSON.parse(raw) as T;
    } catch (error) {
      const code = (error as NodeJS.ErrnoException).code;
      if (code === 'ENOENT') return structuredClone(fallback);
      // A corrupt state file must be loud. Silently starting from empty would
      // erase the maintainer's judgments and look like nothing happened.
      throw new Error(`.bench-state/${name} is unreadable: ${String(error)}`);
    }
  }

  /** Whole-file write via a temp file plus rename, so a crash mid-write cannot truncate state. */
  async #writeFile(name: string, value: unknown): Promise<void> {
    await mkdir(this.#dir, { recursive: true });
    const target = join(this.#dir, name);
    const temp = `${target}.tmp`;
    await writeFile(temp, `${JSON.stringify(value, null, 2)}\n`, 'utf8');
    await rename(temp, target);
  }

  // -- reviews --------------------------------------------------------------

  reviews(): readonly ReviewRecord[] {
    return Object.values(this.#state.reviews);
  }

  reviewFor(slug: string): ReviewRecord | null {
    return this.#state.reviews[slug] ?? null;
  }

  /**
   * Records the maintainer's judgment on one note.
   *
   * This is an input to a future run, never a write to the note. The app does
   * not flip `status`, only a settle run does (design doc section 2).
   */
  async recordReview(record: ReviewRecord): Promise<void> {
    this.#state.reviews[record.slug] = record;
    await this.#writeFile('reviews.json', this.#state.reviews);
  }

  async clearReview(slug: string): Promise<void> {
    delete this.#state.reviews[slug];
    await this.#writeFile('reviews.json', this.#state.reviews);
  }

  /** Judgments still waiting for a run, matching the note's current blob. */
  pending(action: Disposition, currentBlobs: ReadonlyMap<string, string>): ReviewRecord[] {
    return this.reviews()
      .filter((r) => r.action === action && r.dispatchedRunId === null)
      .filter((r) => currentBlobs.get(r.slug) === r.blob)
      .sort((a, b) => a.slug.localeCompare(b.slug));
  }

  async markDispatched(slugs: readonly string[], runId: string): Promise<void> {
    for (const slug of slugs) {
      const existing = this.#state.reviews[slug];
      if (existing) this.#state.reviews[slug] = { ...existing, dispatchedRunId: runId };
    }
    await this.#writeFile('reviews.json', this.#state.reviews);
  }

  /**
   * Hands judgments back to the queue when the run that consumed them ended
   * without settling or rewriting anything (error/stopped). Only clears a slug
   * still dispatched to THIS run, so a later dispatch is never clobbered.
   */
  async releaseDispatched(slugs: readonly string[], runId: string): Promise<void> {
    let changed = false;
    for (const slug of slugs) {
      const existing = this.#state.reviews[slug];
      if (existing !== undefined && existing.dispatchedRunId === runId) {
        this.#state.reviews[slug] = { ...existing, dispatchedRunId: null };
        changed = true;
      }
    }
    if (changed) await this.#writeFile('reviews.json', this.#state.reviews);
  }

  // -- threads --------------------------------------------------------------

  thread(slug: string): readonly Comment[] {
    return this.#state.threads[slug] ?? [];
  }

  allThreads(): Readonly<Record<string, readonly Comment[]>> {
    return this.#state.threads;
  }

  /**
   * Appends to a note's comment thread.
   *
   * The whole thread is kept, not just the latest comment: a rewrite run is
   * handed every comment on that note (design doc section 3.3, ruling D17).
   */
  async appendComment(slug: string, comment: Comment): Promise<void> {
    const existing = this.#state.threads[slug] ?? [];
    this.#state.threads[slug] = [...existing, comment];
    await this.#writeFile('threads.json', this.#state.threads);
  }

  // -- runs -----------------------------------------------------------------

  runs(): readonly RunRecord[] {
    return this.#state.runs;
  }

  run(id: string): RunRecord | null {
    return this.#state.runs.find((r) => r.id === id) ?? null;
  }

  activeRun(): RunRecord | null {
    return this.#state.runs.find((r) => r.status === 'running') ?? null;
  }

  async upsertRun(record: RunRecord): Promise<void> {
    const index = this.#state.runs.findIndex((r) => r.id === record.id);
    if (index === -1) this.#state.runs.unshift(record);
    else this.#state.runs[index] = record;
    await this.#writeFile('runs.json', this.#state.runs);
  }

  // -- budget ---------------------------------------------------------------

  budget(): Budget {
    return this.#state.budget;
  }

  async setBudget(budget: Budget): Promise<void> {
    this.#state.budget = budget;
    await this.#writeFile('budget.json', budget);
  }

  /** Estimated spend across finished runs, for the budget panel's cumulative view. */
  spend(): { today: number; week: number; total: number } {
    const now = Date.now();
    const day = 24 * 60 * 60 * 1000;
    let today = 0;
    let week = 0;
    let total = 0;
    for (const run of this.#state.runs) {
      const age = now - Date.parse(run.startedAt);
      total += run.estimatedCostUsd;
      if (age <= day) today += run.estimatedCostUsd;
      if (age <= 7 * day) week += run.estimatedCostUsd;
    }
    return { today, week, total };
  }
}
