import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { copiedPaths, type KbIngest } from './kb.ts';

describe('reading an ingest report', () => {
  it('returns what this round copied, sorted', () => {
    const report = {
      repos: [
        { repo: 'learning-agent', copied: ['evals/MISSION.md', 'CLAUDE.md'], unchanged: false, error: null },
      ],
      copied: 2,
      skipped: 0,
    } satisfies KbIngest;
    assert.deepEqual(copiedPaths(report, 'learning-agent'), ['CLAUDE.md', 'evals/MISSION.md']);
  });

  it('leaves other sources out of it', () => {
    // `kb ingest` with no argument walks the whole watchlist. A distill fired
    // for one source that swept up another's files would put material in the
    // prompt that this run has no business distilling.
    const report = {
      repos: [
        { repo: 'learning-agent', copied: ['evals/MISSION.md'], unchanged: false, error: null },
        { repo: 'startup-studies', copied: ['a-deal/NOTES.md'], unchanged: false, error: null },
      ],
      copied: 2,
      skipped: 0,
    } satisfies KbIngest;
    assert.deepEqual(copiedPaths(report, 'learning-agent'), ['evals/MISSION.md']);
  });

  it('reads an unchanged source as nothing copied, not as an error', () => {
    // The cursor not moving is the ordinary second-press case, and kb exits 0
    // for it. It has to arrive as an empty batch so the prompt can say so.
    const report = {
      repos: [{ repo: 'learning-agent', copied: [], unchanged: true, error: null }],
      copied: 0,
      skipped: 0,
    } satisfies KbIngest;
    assert.deepEqual(copiedPaths(report, 'learning-agent'), []);
  });

  it('survives a report it could not parse rather than throwing into the endpoint', () => {
    assert.deepEqual(copiedPaths(null, 'learning-agent'), []);
    assert.deepEqual(copiedPaths({} as KbIngest, 'learning-agent'), []);
  });
});
