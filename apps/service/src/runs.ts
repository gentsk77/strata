import { randomUUID } from 'node:crypto';

import { runKb, type KbDoctor } from './kb.ts';
import { REPO_ROOT } from './paths.ts';
import type { Comment, RunKind, RunRecord, RunUsage, StateStore, ToolUseRecord } from './state.ts';

/**
 * The model every run uses unless a caller names another one.
 *
 * Pinned rather than inherited: an unattended run writes the knowledge base, so
 * which model wrote a note has to be reconstructible from the run record. The
 * maintainer ruled Sonnet 5 on 2026-08-13.
 */
export const DEFAULT_RUN_MODEL = 'claude-sonnet-5';

/**
 * Supervises unattended Agent SDK runs.
 *
 * Three hard rules live here rather than in the routes, because they are
 * properties of the knowledge base and not of any one endpoint:
 *
 * 1. **One run at a time.** strata is a single-writer system (design doc
 *    section 4). A second concurrent run is refused, not queued.
 * 2. **The app never writes `notes/`.** Nothing in this file edits a note. The
 *    app hands a run the maintainer's judgment and the run does the writing,
 *    under the note law this repo owns (design doc section 2).
 * 3. **Never push.** Runs commit, in both repos, and that is all. Enforced
 *    below by a PreToolUse hook, not by hoping the prompt is obeyed.
 */

/** The minimum an SDK message must look like for us to read it. */
interface SdkMessage {
  readonly type: string;
  readonly subtype?: string;
  readonly [key: string]: unknown;
}

export interface QueryFn {
  (params: { prompt: string; options: Record<string, unknown> }): AsyncIterable<SdkMessage>;
}

export interface RunEvent {
  readonly runId: string;
  readonly kind: 'started' | 'message' | 'tool' | 'telemetry' | 'finished' | 'rate_limit';
  readonly at: string;
  readonly payload: unknown;
}

export interface StartRunRequest {
  readonly kind: RunKind;
  readonly subject: string;
  readonly slugs: readonly string[];
  readonly prompt: string;
  readonly model?: string;
}

/**
 * Commands a run must never issue.
 *
 * `git push` is the one that matters: the maintainer owns the remotes of both
 * repos, and an unattended run that pushes cannot be taken back. Matched
 * loosely on purpose, `git   push`, `git -C ../elsewhere push` and a push buried
 * behind `&&` all have to trip it.
 */
export const FORBIDDEN_BASH = /\bgit\b[^\n;|&]*\bpush\b/;

/** True when a Bash command must be refused before it runs. */
export function isForbiddenCommand(toolName: string, command: string): boolean {
  return toolName === 'Bash' && FORBIDDEN_BASH.test(command);
}

export class RunSupervisor {
  readonly #store: StateStore;
  readonly #query: QueryFn;
  readonly #listeners = new Set<(event: RunEvent) => void>();
  #active: { id: string; kind: RunKind; abort: AbortController } | null = null;

  constructor(store: StateStore, query: QueryFn) {
    this.#store = store;
    this.#query = query;
  }

  subscribe(listener: (event: RunEvent) => void): () => void {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  }

  #emit(event: RunEvent): void {
    for (const listener of this.#listeners) {
      try {
        listener(event);
      } catch {
        // A broken SSE client must not take down a run that is mid-flight.
      }
    }
  }

  activeRunId(): string | null {
    return this.#active?.id ?? null;
  }

  stop(): boolean {
    if (this.#active === null) return false;
    this.#active.abort.abort();
    return true;
  }

  /**
   * Fires a run and returns as soon as it has an id.
   *
   * The run itself continues in the background and reports through events and
   * the run record, because a distill takes minutes and an HTTP request that
   * waits for it is a request that times out.
   */
  async start(request: StartRunRequest): Promise<{ runId: string } | { error: string }> {
    if (this.#active !== null) {
      // Named by kind, not by uuid. This string reaches the maintainer as a
      // banner, and a uuid tells them nothing about what is in their way.
      return { error: `A ${this.#active.kind} run is already going. The knowledge base takes one writer at a time.` };
    }

    const budget = this.#store.budget();
    const id = randomUUID();
    const abort = new AbortController();
    this.#active = { id, kind: request.kind, abort };

    const unsettledBefore = new Set(await this.#unsettledSlugs());

    const record: RunRecord = {
      id,
      kind: request.kind,
      status: 'running',
      startedAt: new Date().toISOString(),
      endedAt: null,
      subject: request.subject,
      slugs: request.slugs,
      sessionId: null,
      numTurns: 0,
      usage: { inputTokens: 0, outputTokens: 0, cacheReadTokens: 0, cacheCreationTokens: 0 },
      estimatedCostUsd: 0,
      durationMs: 0,
      durationApiMs: 0,
      toolUses: [],
      modelUsage: {},
      permissionDenials: [],
      terminalReason: null,
      errors: [],
      producedSlugs: [],
      doctorOk: null,
      rateLimit: null,
      transcript: [],
    };
    await this.#store.upsertRun(record);
    this.#emit({ runId: id, kind: 'started', at: record.startedAt, payload: record });

    void this.#drive(record, request, budget, abort, unsettledBefore).finally(() => {
      this.#active = null;
    });

    return { runId: id };
  }

  async #drive(
    initial: RunRecord,
    request: StartRunRequest,
    budget: { maxTurns: number; maxCostUsd: number },
    abort: AbortController,
    unsettledBefore: ReadonlySet<string>,
  ): Promise<void> {
    let record = initial;
    const toolUses = new Map<string, ToolUseRecord>();
    const transcript: string[] = [];
    let result: SdkMessage | null = null;
    let thrown: Error | null = null;
    let liveTurns = 0;
    let liveUsage = { inputTokens: 0, outputTokens: 0, cacheReadTokens: 0, cacheCreationTokens: 0 };

    const save = async (patch: Partial<RunRecord>): Promise<void> => {
      record = { ...record, ...patch } as RunRecord;
      await this.#store.upsertRun(record);
    };

    const options: Record<string, unknown> = {
      // The bench is the cwd, not strata: the skills, the spec and the kb tool
      // all live here now. strata is only a notes path kb resolves.
      cwd: REPO_ROOT,
      // Explicit, and load-bearing. Omitting this pulls in every personal
      // user-level skill on the machine and makes runs irreproducible; `[]`
      // silently disables this project's own /kb-distill and /kb-settle.
      settingSources: ['project'],
      permissionMode: 'bypassPermissions',
      maxTurns: budget.maxTurns,
      maxBudgetUsd: budget.maxCostUsd,
      abortController: abort,
      hooks: {
        PreToolUse: [
          {
            hooks: [
              async (input: unknown): Promise<unknown> => {
                const hookInput = input as { tool_name?: string; tool_input?: { command?: string } };
                const command = hookInput.tool_input?.command ?? '';
                if (isForbiddenCommand(hookInput.tool_name ?? '', command)) {
                  return {
                    hookSpecificOutput: {
                      hookEventName: 'PreToolUse',
                      permissionDecision: 'deny',
                      permissionDecisionReason:
                        'The bench never pushes. The maintainer owns both remotes. Commit and stop.',
                    },
                  };
                }
                return {};
              },
            ],
          },
        ],
      },
    };
    // Runs write the knowledge base, so the model is a recorded decision, never
    // an ambient default. Leaving it unset once let a real distill run silently
    // execute on Haiku with nothing in the record saying so.
    options['model'] = request.model ?? DEFAULT_RUN_MODEL;

    try {
      for await (const message of this.#query({ prompt: request.prompt, options })) {
        if (message.type === 'result') {
          // Collected INSIDE the loop on purpose: the iterator throws on any
          // non-success result AFTER emitting it, so a naive `for await` loses
          // the entire run's telemetry at the finish line.
          result = message;
          continue;
        }
        if (message.type === 'system' && message.subtype === 'init') {
          await save({ sessionId: (message['session_id'] as string) ?? null });
          this.#emit({ runId: record.id, kind: 'message', at: new Date().toISOString(), payload: { type: 'init' } });
          continue;
        }
        if (message.type === 'rate_limit_event') {
          await save({ rateLimit: message['rate_limit_info'] ?? null });
          this.#emit({
            runId: record.id,
            kind: 'rate_limit',
            at: new Date().toISOString(),
            payload: message['rate_limit_info'],
          });
          continue;
        }
        if (message.type === 'assistant') {
          const content = readContent(message);
          for (const block of content) {
            if (block['type'] === 'tool_use') {
              const use: ToolUseRecord = {
                id: String(block['id'] ?? ''),
                name: String(block['name'] ?? 'unknown'),
                input: block['input'] ?? null,
                at: new Date().toISOString(),
                ok: null,
                resultSummary: null,
              };
              toolUses.set(use.id, use);
              this.#emit({ runId: record.id, kind: 'tool', at: use.at, payload: use });
            } else if (block['type'] === 'text') {
              const text = String(block['text'] ?? '').trim();
              if (text !== '') {
                transcript.push(text);
                this.#emit({
                  runId: record.id,
                  kind: 'message',
                  at: new Date().toISOString(),
                  payload: { type: 'text', text },
                });
              }
            }
          }
          // Live telemetry. The authoritative numbers only arrive on the final
          // result message, but a panel that reads zero for the ten minutes a
          // distill takes is not observability. Each assistant message carries
          // its own usage, so accumulate as we go and let the result overwrite
          // at the end.
          liveTurns += 1;
          const envelope = message['message'] as { usage?: Record<string, number> } | undefined;
          if (envelope?.usage) {
            const partial = partialUsage(envelope.usage);
            liveUsage = {
              inputTokens: liveUsage.inputTokens + partial.inputTokens,
              outputTokens: liveUsage.outputTokens + partial.outputTokens,
              cacheReadTokens: liveUsage.cacheReadTokens + partial.cacheReadTokens,
              cacheCreationTokens: liveUsage.cacheCreationTokens + partial.cacheCreationTokens,
            };
            this.#emit({
              runId: record.id,
              kind: 'telemetry',
              at: new Date().toISOString(),
              payload: liveUsage,
            });
          }
          await save({
            toolUses: [...toolUses.values()],
            transcript: [...transcript],
            numTurns: liveTurns,
            usage: liveUsage,
          });
          continue;
        }
        if (message.type === 'user') {
          // The structured `tool_use_result` is the good source for a tool-use
          // view; the text blob in `content` is a rendering of it.
          const structured = message['tool_use_result'];
          for (const block of readContent(message)) {
            if (block['type'] !== 'tool_result') continue;
            const id = String(block['tool_use_id'] ?? '');
            const existing = toolUses.get(id);
            if (existing === undefined) continue;
            toolUses.set(id, {
              ...existing,
              ok: block['is_error'] === true ? false : true,
              resultSummary: summarise(structured ?? block['content']),
            });
          }
          await save({ toolUses: [...toolUses.values()] });
          continue;
        }
      }
    } catch (error) {
      thrown = error instanceof Error ? error : new Error(String(error));
    }

    const unsettledAfter = await this.#unsettledSlugs();
    const produced = unsettledAfter.filter((slug) => !unsettledBefore.has(slug)).sort();

    const doctor = await runKb<KbDoctor>(['doctor']);
    const doctorOk = doctor.data !== null ? doctor.data.violations.length === 0 : null;

    const patch = telemetryFrom(result, thrown, abort.signal.aborted);
    await save({
      ...patch,
      toolUses: [...toolUses.values()],
      transcript: [...transcript],
      producedSlugs: produced,
      doctorOk,
      endedAt: new Date().toISOString(),
    });

    // A failed or stopped run must hand the maintainer's judgments back. They
    // were marked dispatched when the run started, and a run that never settled
    // or rewrote anything would otherwise leave them stuck "in flight" forever,
    // because pending() only selects dispatchedRunId === null.
    if (patch.status === 'error' || patch.status === 'stopped') {
      await this.#store.releaseDispatched(request.slugs, record.id);
    }

    this.#emit({ runId: record.id, kind: 'finished', at: new Date().toISOString(), payload: record });
  }

  /** Slugs currently `status: unsettled`, straight from kb. Never parsed here. */
  async #unsettledSlugs(): Promise<string[]> {
    const listed = await runKb<Record<string, Array<{ slug: string; status: string }>>>(['list']);
    if (listed.data === null) return [];
    const out: string[] = [];
    for (const group of Object.values(listed.data)) {
      if (!Array.isArray(group)) continue;
      for (const note of group) {
        if (note.status === 'unsettled') out.push(note.slug);
      }
    }
    return out;
  }
}

function readContent(message: SdkMessage): Array<Record<string, unknown>> {
  const inner = message['message'] as { content?: unknown } | undefined;
  const content = inner?.content;
  return Array.isArray(content) ? (content as Array<Record<string, unknown>>) : [];
}

function partialUsage(usage: Record<string, number>): RunUsage {
  return {
    inputTokens: usage['input_tokens'] ?? 0,
    outputTokens: usage['output_tokens'] ?? 0,
    cacheReadTokens: usage['cache_read_input_tokens'] ?? 0,
    cacheCreationTokens: usage['cache_creation_input_tokens'] ?? 0,
  };
}

function summarise(value: unknown): string {
  if (value === null || value === undefined) return '';
  const text = typeof value === 'string' ? value : JSON.stringify(value);
  return text.length > 400 ? `${text.slice(0, 400)}…` : text;
}

/**
 * Turns the SDK's result message into the fields the run panel shows.
 *
 * Handles the case where the iterator threw before any result arrived: the run
 * still gets a status and an error, rather than sitting on "running" forever.
 */
export function telemetryFrom(
  result: SdkMessage | null,
  thrown: Error | null,
  aborted: boolean,
): Partial<RunRecord> {
  if (result === null) {
    return {
      status: aborted ? 'stopped' : 'error',
      errors: thrown ? [thrown.message] : ['the run produced no result message'],
      terminalReason: aborted ? 'aborted' : 'no_result',
    };
  }

  const usage = (result['usage'] ?? {}) as Record<string, number>;
  const subtype = String(result['subtype'] ?? '');
  const errors = Array.isArray(result['errors']) ? (result['errors'] as string[]) : [];
  const modelUsage = (result['modelUsage'] ?? {}) as Record<string, unknown>;

  return {
    status: aborted ? 'stopped' : subtype === 'success' ? 'success' : 'error',
    sessionId: (result['session_id'] as string) ?? null,
    numTurns: Number(result['num_turns'] ?? 0),
    usage: totalUsage(usage, modelUsage),
    estimatedCostUsd: Number(result['total_cost_usd'] ?? 0),
    durationMs: Number(result['duration_ms'] ?? 0),
    durationApiMs: Number(result['duration_api_ms'] ?? 0),
    modelUsage: (result['modelUsage'] ?? {}) as Record<string, unknown>,
    permissionDenials: Array.isArray(result['permission_denials'])
      ? (result['permission_denials'] as unknown[])
      : [],
    terminalReason: (result['terminal_reason'] as string) ?? subtype,
    errors: thrown && errors.length === 0 ? [thrown.message] : errors,
  };
}

/**
 * The run's true token totals.
 *
 * The result's top-level `usage` counts the MAIN THREAD ONLY. When a run
 * delegates to a subagent, that subagent's tokens land in `modelUsage` and
 * never appear in `usage`. On the first real distill this repo ran, the two
 * disagreed by more than an order of magnitude: `usage` claimed 230,695
 * cache-read tokens while `modelUsage` totalled 6,377,219 for the same run that
 * cost $1.88. Showing the smaller number next to the real cost would tell the
 * maintainer their cache is cheap when it is the entire bill.
 *
 * So: sum `modelUsage` across models when it has anything, and fall back to the
 * top-level figures when it does not.
 */
export function totalUsage(
  usage: Record<string, number>,
  modelUsage: Record<string, unknown>,
): RunUsage {
  const models = Object.values(modelUsage).filter(
    (entry): entry is Record<string, number> => typeof entry === 'object' && entry !== null,
  );

  if (models.length === 0) {
    return {
      inputTokens: Number(usage['input_tokens'] ?? 0),
      outputTokens: Number(usage['output_tokens'] ?? 0),
      cacheReadTokens: Number(usage['cache_read_input_tokens'] ?? 0),
      cacheCreationTokens: Number(usage['cache_creation_input_tokens'] ?? 0),
    };
  }

  const sum = (key: string): number =>
    models.reduce((total, entry) => total + Number(entry[key] ?? 0), 0);

  return {
    inputTokens: sum('inputTokens'),
    outputTokens: sum('outputTokens'),
    cacheReadTokens: sum('cacheReadInputTokens'),
    cacheCreationTokens: sum('cacheCreationInputTokens'),
  };
}

// ---------------------------------------------------------------------------
// Prompts. Each one is the app's entry into an existing skill; the app adds no
// second implementation of distillation or review (design doc requirement 4).
// ---------------------------------------------------------------------------

/**
 * The distill run's prompt.
 *
 * It has to name the files, not just the source. `inbox/` is a pile of
 * copies that is never emptied: `kb ingest` copies whatever changed since its
 * cursor and leaves every earlier round's copy in place. So "the material from
 * source X" is not a set the run can compute, on a source ingested twice it
 * means this round's handful plus every file distilled in earlier rounds, and
 * the run has no way to tell them apart.
 *
 * Naming the source alone therefore reads as "re-distill everything you can
 * see", which costs a full run and produces duplicate notes for material that
 * already has them. The list `kb ingest --json` returns is the only thing that
 * knows the difference, and it was already on hand at the call site.
 *
 * `copied` is repo-relative paths, straight from that report, unmapped. The
 * inbox copy's filename is a flattening of the path (`kb/ingest.py`,
 * `_entry_name`), described in the prompt rather than recomputed here: a stale
 * description degrades into a name the run can fix by listing the directory,
 * while a stale recomputation hands it a confident wrong answer.
 */
export function distillPrompt(source: string, copied: readonly string[]): string {
  const nothingNew = [
    '- **It copied nothing.** Every file this source has is already in inbox/ from an earlier round, and every',
    '  one of them was already distilled into notes by the run that fetched it.',
    '- So there is no material for you. Do NOT distill what is sitting in inbox/: those notes exist, and writing',
    '  them again produces duplicates that the note law has no way to reconcile.',
    '- Say that there was nothing new and stop. Write no notes, commit nothing.',
  ];

  const material = [
    `- It copied ${String(copied.length)} file${copied.length === 1 ? '' : 's'} into strata's inbox/.`,
    '  **These, and only these, are this round\'s material:**',
    '',
    ...copied.map((rel) => `  - ${rel}`),
    '',
    '- inbox/ is never emptied, so it also holds copies from every earlier round. Those already have notes.',
    `  A file that is not on the list above is not yours to distill, even though it also came from "${source}".`,
    `- The inbox copy of each file is its path with the separators flattened to \`--\` and the source name in`,
    `  front: \`a/b.md\` becomes \`${source}--a--b.md\`. If one does not match, list inbox/ and go by what is there.`,
    '- Follow the skill exactly, including the cross-linking pass and the failure list.',
    '- Commit in both repos as the skill instructs. Never push. Both remotes belong to the maintainer.',
  ];

  // The opening line has to agree with the batch. An empty batch that still
  // opens with "Run the /kb-distill skill over the material just ingested"
  // pits the imperative against the stop instructions below it, and in an
  // unattended run nobody arbitrates that contest.
  const opening =
    copied.length === 0
      ? `A distill was requested for the watchlist source "${source}", but this round's ingest found nothing new. Your only job is to confirm that and stop.`
      : `Run the /kb-distill skill over the material just ingested from the watchlist source "${source}".`;

  return [
    opening,
    '',
    'Context you need:',
    `- \`python -m kb ingest ${source}\` has ALREADY been run by the app.`,
    ...(copied.length === 0 ? nothingNew : material),
    '',
    'This is an unattended run fired from the review bench. There is nobody to ask, so if a judgment call comes up, take the conservative option the skill describes and record it in the failure list.',
  ].join('\n');
}

/**
 * The settle run's prompt.
 *
 * It carries the batch and nothing else. What an unattended settle run may and
 * may not do lives in the skill, under the heading 「审理台入口：批量转 settled」
 * including the rule that a note the run rewrote does not settle in place.
 *
 * That split is deliberate and it is the fix for a real defect: this prompt
 * used to spell out the settling rules itself, in words that contradicted the
 * skill it was loading (the skill forbids batch-settling; the prompt ordered
 * it). A prompt does not get to overrule the skill. The skill is the law, the
 * prompt is the work order.
 */
export function settlePrompt(slugs: readonly string[]): string {
  return [
    'Run the /kb-settle skill through its review-bench entry point, over exactly these notes and no others:',
    '',
    ...slugs.map((slug) => `- ${slug}`),
    '',
    'This prompt carries the batch, not the rules. The rules are the skill\'s, in the sections 「两个入口，法都在这里」 and 「审理台入口：批量转 settled」. Read them and follow them. If anything here seems to contradict them, the skill wins and you say so in your closing summary.',
    '',
    'The batch facts you cannot get from the skill:',
    '',
    `- Criterion 3 was recorded in the bench for each of the ${String(slugs.length)} notes above, before this run started. The maintainer read each one in the review screen and pressed approve.`,
    '- Their approval covers the version of each note that is on disk right now. Nothing has touched these notes since they approved them.',
    '',
    'This run is unattended. There is nobody to ask mid-run, so where the skill offers a conservative option, take it. Never push.',
  ].join('\n');
}

/**
 * The rewrite run's prompt.
 *
 * Like the settle prompt, it carries the batch and defers the rules to the
 * skill's 「审理台入口：按留言改写」 section.
 *
 * Every comment on a note is included, not just the newest. A thread is a
 * conversation and the run needs all of it (design doc section 3.3, ruling D17).
 */
export function rewritePrompt(threads: ReadonlyArray<{ slug: string; comments: readonly Comment[] }>): string {
  const blocks = threads.map(({ slug, comments }) => {
    const lines = comments.map((c) => `  - [${c.at}] ${c.author}: ${c.text}`);
    return [`### ${slug}`, ...(lines.length > 0 ? lines : ['  - (no comment recorded)'])].join('\n');
  });

  return [
    'Run the /kb-settle skill through its review-bench rewrite entry point, over exactly these notes and no others.',
    '',
    'This prompt carries the batch, not the rules. The rules are the skill\'s, in the section 「审理台入口：按留言改写」. Read it and follow it. If anything here seems to contradict it, the skill wins and you say so in your closing summary.',
    '',
    'The maintainer read each note in the review screen, declined it, and left comments saying what is wrong. The FULL comment thread for each note is below, oldest first.',
    '',
    ...blocks,
    '',
    'This run is unattended. There is nobody to ask mid-run. Never push.',
  ].join('\n');
}
