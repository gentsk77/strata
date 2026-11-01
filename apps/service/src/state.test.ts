import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { describe, it } from 'node:test';

import { blobHash, splitFrontMatter, wikilinks } from './notes.ts';
import { DEFAULT_BUDGET, StateStore, type ReviewRecord } from './state.ts';
import { tempStateDir } from './testing.ts';

describe('the review key', () => {
  it('matches git for a note with CRLF line endings', () => {
    // strata runs core.autocrlf=true, so the file on disk has CRLF while the
    // blob git stored has LF. Hashing the disk bytes yields an id that matches
    // nothing in git. Both spellings must hash the same.
    const lf = Buffer.from('---\nid: x\n---\n\nbody\n', 'utf8');
    const crlf = Buffer.from('---\r\nid: x\r\n---\r\n\r\nbody\r\n', 'utf8');
    assert.equal(blobHash(crlf), blobHash(lf));
  });

  it('changes when the note text changes', () => {
    const before = blobHash(Buffer.from('one claim\n', 'utf8'));
    const after = blobHash(Buffer.from('two claims\n', 'utf8'));
    assert.notEqual(before, after);
  });

  it('is a 40-char sha1, the shape git uses', () => {
    assert.match(blobHash(Buffer.from('x')), /^[0-9a-f]{40}$/);
  });
});

describe('front matter and links', () => {
  it('reads flat scalars and leaves the body alone', () => {
    const { frontMatter, body } = splitFrontMatter(
      '---\nid: a-slug\nstatus: unsettled\n---\n\n正文在这里 [[other-note]]\n',
    );
    assert.equal(frontMatter['id'], 'a-slug');
    assert.equal(frontMatter['status'], 'unsettled');
    assert.match(body, /^正文在这里/);
  });

  it('survives a note with no front matter', () => {
    const { frontMatter, body } = splitFrontMatter('just a body\n');
    assert.deepEqual(frontMatter, {});
    assert.equal(body, 'just a body\n');
  });

  it('finds wikilinks, deduplicated, and handles the pipe form', () => {
    assert.deepEqual(wikilinks('see [[a]] and [[b|label]] and [[a]] again'), ['a', 'b']);
  });
});

describe('StateStore', () => {
  it('survives a restart, which is the whole point of it', async () => {
    const dir = await tempStateDir();
    const first = await StateStore.open(dir);

    const record: ReviewRecord = {
      slug: 'cost-driver-is-rounds-times-context',
      blob: 'a'.repeat(40),
      action: 'approve',
      at: '2026-08-13T10:00:00.000Z',
      dispatchedRunId: null,
    };
    await first.recordReview(record);
    await first.appendComment('some-other-note', {
      at: '2026-08-13T10:01:00.000Z',
      author: 'maintainer',
      text: 'the second half is a different claim, split it',
      blob: 'b'.repeat(40),
    });

    // A genuinely separate instance reading the same directory, which is what a
    // service restart is.
    const second = await StateStore.open(dir);
    assert.deepEqual(second.reviewFor('cost-driver-is-rounds-times-context'), record);
    assert.equal(second.thread('some-other-note').length, 1);
    assert.match(second.thread('some-other-note')[0]?.text ?? '', /split it/);
  });

  it('keeps the whole comment thread, not just the latest', async () => {
    const store = await StateStore.open(await tempStateDir());
    for (const text of ['first', 'second', 'third']) {
      await store.appendComment('n', { at: new Date().toISOString(), author: 'maintainer', text, blob: 'x' });
    }
    assert.deepEqual(
      store.thread('n').map((c) => c.text),
      ['first', 'second', 'third'],
    );
  });

  it('drops a judgment from the batch once the note text moved under it', async () => {
    const store = await StateStore.open(await tempStateDir());
    await store.recordReview({
      slug: 'n',
      blob: 'old',
      action: 'approve',
      at: new Date().toISOString(),
      dispatchedRunId: null,
    });

    assert.equal(store.pending('approve', new Map([['n', 'old']])).length, 1);
    // The note was rewritten, so the approval no longer covers what is on disk.
    assert.equal(store.pending('approve', new Map([['n', 'new']])).length, 0);
  });

  it('stops re-batching a judgment a run already consumed', async () => {
    const store = await StateStore.open(await tempStateDir());
    await store.recordReview({
      slug: 'n',
      blob: 'b',
      action: 'approve',
      at: new Date().toISOString(),
      dispatchedRunId: null,
    });
    const blobs = new Map([['n', 'b']]);
    assert.equal(store.pending('approve', blobs).length, 1);

    await store.markDispatched(['n'], 'run-1');
    assert.equal(store.pending('approve', blobs).length, 0);
  });

  it('hands a judgment back when its run fails, so it can be re-batched', async () => {
    const store = await StateStore.open(await tempStateDir());
    await store.recordReview({
      slug: 'n',
      blob: 'b',
      action: 'approve',
      at: new Date().toISOString(),
      dispatchedRunId: null,
    });
    const blobs = new Map([['n', 'b']]);
    await store.markDispatched(['n'], 'run-1');
    assert.equal(store.pending('approve', blobs).length, 0);

    await store.releaseDispatched(['n'], 'run-1');
    assert.equal(store.pending('approve', blobs).length, 1);
  });

  it('only releases a slug still dispatched to the releasing run', async () => {
    const store = await StateStore.open(await tempStateDir());
    await store.recordReview({
      slug: 'n',
      blob: 'b',
      action: 'approve',
      at: new Date().toISOString(),
      dispatchedRunId: null,
    });
    const blobs = new Map([['n', 'b']]);
    await store.markDispatched(['n'], 'run-1');
    // A different run releasing must not clobber this dispatch.
    await store.releaseDispatched(['n'], 'run-2');
    assert.equal(store.pending('approve', blobs).length, 0);
  });

  it('separates approvals from declines', async () => {
    const store = await StateStore.open(await tempStateDir());
    const at = new Date().toISOString();
    await store.recordReview({ slug: 'a', blob: 'x', action: 'approve', at, dispatchedRunId: null });
    await store.recordReview({ slug: 'b', blob: 'y', action: 'reject', at, dispatchedRunId: null });
    const blobs = new Map([
      ['a', 'x'],
      ['b', 'y'],
    ]);
    assert.deepEqual(
      store.pending('approve', blobs).map((r) => r.slug),
      ['a'],
    );
    assert.deepEqual(
      store.pending('reject', blobs).map((r) => r.slug),
      ['b'],
    );
  });

  it('starts on the default budget and persists a change', async () => {
    const dir = await tempStateDir();
    const store = await StateStore.open(dir);
    assert.deepEqual(store.budget(), DEFAULT_BUDGET);

    await store.setBudget({ maxTurns: 3, maxCostUsd: 0.05 });
    assert.deepEqual((await StateStore.open(dir)).budget(), { maxTurns: 3, maxCostUsd: 0.05 });
  });

  it('refuses to silently start empty when a state file is corrupt', async () => {
    const dir = await tempStateDir();
    const store = await StateStore.open(dir);
    await store.recordReview({
      slug: 'n',
      blob: 'b',
      action: 'approve',
      at: new Date().toISOString(),
      dispatchedRunId: null,
    });

    await writeFile(join(dir, 'reviews.json'), '{ this is not json', 'utf8');
    // Losing the maintainer's judgments quietly is worse than failing loudly.
    await assert.rejects(() => StateStore.open(dir), /unreadable/);
  });

  it('writes readable JSON a human can inspect', async () => {
    const dir = await tempStateDir();
    const store = await StateStore.open(dir);
    await store.setBudget({ maxTurns: 9, maxCostUsd: 1.5 });
    const raw = await readFile(join(dir, 'budget.json'), 'utf8');
    assert.match(raw, /"maxTurns": 9/);
  });
});
