import { spawn } from 'node:child_process';

import { PYTHON, REPO_ROOT } from './paths.ts';

/**
 * One invocation of the `kb` command line.
 *
 * `exitCode` is reported rather than thrown on, because several kb commands use
 * a non-zero exit as information rather than as failure: `kb query` exits 1 on
 * zero hits (grep convention) and `kb doctor` exits 1 when it found violations.
 * Callers decide what a code means; this layer only transports it.
 */
export interface KbInvocation<T> {
  readonly exitCode: number;
  readonly data: T | null;
  readonly stdout: string;
  readonly stderr: string;
  /** Set when stdout was not parseable JSON, which is always a bug worth surfacing. */
  readonly parseError: string | null;
}

export interface KbRunner {
  <T>(args: readonly string[]): Promise<KbInvocation<T>>;
}

/**
 * Runs `python -m kb <args> --json` from the repo root.
 *
 * The repo root is the cwd because that is where `strata.config.json` and
 * `watchlist.json` live; kb resolves strata's notes path from there. This is
 * the app's ONLY route to knowledge-base computation: the app never parses
 * `notes/` to derive a view (design doc section 2, ruling D16).
 */
export const runKb: KbRunner = function runKb<T>(args: readonly string[]): Promise<KbInvocation<T>> {
  return new Promise((resolvePromise) => {
    const child = spawn(PYTHON, ['-m', 'kb', ...args, '--json'], {
      cwd: REPO_ROOT,
      // stdio pipes only; kb is never interactive.
      stdio: ['ignore', 'pipe', 'pipe'],
      env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
    });

    let stdout = '';
    let stderr = '';
    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    child.stdout.on('data', (chunk: string) => {
      stdout += chunk;
    });
    child.stderr.on('data', (chunk: string) => {
      stderr += chunk;
    });

    child.on('error', (error: Error) => {
      resolvePromise({
        exitCode: -1,
        data: null,
        stdout,
        stderr: `${stderr}${error.message}`,
        parseError: null,
      });
    });

    child.on('close', (code) => {
      let data: T | null = null;
      let parseError: string | null = null;
      const trimmed = stdout.trim();
      if (trimmed !== '') {
        try {
          data = JSON.parse(trimmed) as T;
        } catch (error) {
          parseError = error instanceof Error ? error.message : String(error);
        }
      }
      resolvePromise({ exitCode: code ?? -1, data, stdout, stderr, parseError });
    });
  });
};

// ---------------------------------------------------------------------------
// Shapes kb returns. Field names copied from the real `--json` output, not
// guessed: `python -m kb <cmd> --json` is the contract between the two repos.
// ---------------------------------------------------------------------------

export interface KbRoots {
  readonly strata_root: string;
  readonly notes: string;
  readonly inbox: string;
  readonly ingest_cursor: string;
  readonly bench_root: string;
  readonly watchlist: string;
  readonly source: string;
}

export interface KbListNote {
  readonly slug: string;
  readonly title: string;
  readonly status: string;
  readonly event_date: string;
  readonly links: readonly string[];
}

/** `kb list --json` is keyed by evidence tier: `stated`, `observed`, ... */
export type KbList = Record<string, readonly KbListNote[]>;

export interface KbDoctor {
  readonly notes: number;
  readonly violations: readonly unknown[];
  readonly dangling: readonly unknown[];
  readonly orphans: readonly unknown[];
  readonly stubs: readonly unknown[];
  readonly stale_artifacts: readonly unknown[];
}

export interface KbWatchEntry {
  readonly name: string;
  readonly path: string;
  readonly exists: boolean;
  readonly git_root: boolean;
}

export interface KbWatch {
  readonly watched: readonly KbWatchEntry[];
  readonly count: number;
}

/** One watched repo's share of an `kb ingest --json` report. */
export interface KbIngestRepo {
  /** The repo's directory name, which is also the watchlist entry's name. */
  readonly repo: string;
  /** Repo-relative paths copied into inbox/ this round. Empty when nothing changed. */
  readonly copied: readonly string[];
  readonly unchanged: boolean;
  readonly error: string | null;
}

export interface KbIngest {
  readonly repos: readonly KbIngestRepo[];
  readonly copied: number;
  readonly skipped: number;
}

/**
 * The paths one ingest round copied for one source.
 *
 * Filtered by name rather than taking the whole report: `kb ingest` with no
 * argument walks the entire watchlist, and a caller that fired it for one
 * source must not be handed another source's files.
 */
export function copiedPaths(report: KbIngest | null, source: string): string[] {
  if (report === null || !Array.isArray(report.repos)) return [];
  const out: string[] = [];
  for (const entry of report.repos) {
    if (entry.repo !== source || !Array.isArray(entry.copied)) continue;
    out.push(...entry.copied);
  }
  return out.sort();
}

/** Flattens `kb list`'s evidence grouping into one array, evidence carried on each note. */
export function flattenList(list: KbList): Array<KbListNote & { evidence: string }> {
  const out: Array<KbListNote & { evidence: string }> = [];
  for (const [evidence, notes] of Object.entries(list)) {
    if (!Array.isArray(notes)) continue;
    for (const note of notes) {
      out.push({ ...note, evidence });
    }
  }
  out.sort((a, b) => a.slug.localeCompare(b.slug));
  return out;
}
