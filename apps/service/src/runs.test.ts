import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, it } from 'node:test';

import { REPO_ROOT } from './paths.ts';
import { distillPrompt, isForbiddenCommand, rewritePrompt, settlePrompt, telemetryFrom } from './runs.ts';

describe('the push guard', () => {
  it('refuses a push however it is spelled', () => {
    // An unattended run that pushes cannot be taken back, and the maintainer
    // owns both remotes. Every spelling a run might reach for has to trip.
    for (const command of [
      'git push',
      'git   push origin main',
      'git -C ../elsewhere push',
      'git add . && git commit -m x && git push',
      'cd /d/Repo/strata; git push --force',
    ]) {
      assert.equal(isForbiddenCommand('Bash', command), true, command);
    }
  });

  it('leaves the commands a run legitimately needs alone', () => {
    for (const command of [
      'git add notes/',
      'git commit -m "settle 2026-08-13: 1 reviewed, 1 settled"',
      'python -m kb doctor',
      'git -C ../elsewhere status',
      // The word appears, but not as a git subcommand.
      'echo "do not push"',
    ]) {
      assert.equal(isForbiddenCommand('Bash', command), false, command);
    }
  });

  it('only polices Bash, since no other tool runs a command', () => {
    assert.equal(isForbiddenCommand('Read', 'git push'), false);
  });
});

/**
 * The result message the SDK actually emitted during the probe, trimmed to the
 * fields the run panel reads. Copied from observed output, not invented.
 */
const REAL_RESULT = {
  type: 'result',
  subtype: 'error_max_turns',
  duration_ms: 3585,
  duration_api_ms: 2579,
  is_error: true,
  num_turns: 2,
  session_id: 'ee459424-eb3d-413f-96b2-6794d3f50f3d',
  total_cost_usd: 0.051895,
  usage: {
    input_tokens: 10,
    cache_creation_input_tokens: 25533,
    cache_read_input_tokens: 0,
    output_tokens: 46,
  },
  modelUsage: { 'claude-haiku-4-5-20251001': { inputTokens: 534 } },
  permission_denials: [],
  terminal_reason: 'max_turns',
  errors: ['Reached maximum number of turns (1)'],
};

describe('telemetryFrom', () => {
  it('keeps the run when the iterator threw after emitting the result', () => {
    // The SDK emits the result message and THEN throws on any non-success
    // subtype. Losing the telemetry at that point loses the whole run.
    const thrown = new Error('Claude Code returned an error result: Reached maximum number of turns (1)');
    const patch = telemetryFrom(REAL_RESULT, thrown, false);

    assert.equal(patch.status, 'error');
    assert.equal(patch.numTurns, 2);
    assert.equal(patch.estimatedCostUsd, 0.051895);
    assert.equal(patch.terminalReason, 'max_turns');
    assert.deepEqual(patch.errors, ['Reached maximum number of turns (1)']);
  });

  it('reports cache-read tokens as their own metric, never folded into input', () => {
    const patch = telemetryFrom(
      {
        ...REAL_RESULT,
        usage: { input_tokens: 10, cache_read_input_tokens: 24876, output_tokens: 5 },
        modelUsage: {},
      },
      null,
      false,
    );
    assert.equal(patch.usage?.inputTokens, 10);
    assert.equal(patch.usage?.cacheReadTokens, 24876);
  });

  it('counts subagent tokens, which the top-level usage silently omits', () => {
    // Observed on the first real distill: `usage` said 230,695 cache-read while
    // `modelUsage` said 6,377,219 for the same $1.88 run. The difference is
    // entirely subagent work, and reporting the small number next to the real
    // cost tells the maintainer their cache is free when it is the whole bill.
    const patch = telemetryFrom(
      {
        ...REAL_RESULT,
        usage: { input_tokens: 26, output_tokens: 9466, cache_read_input_tokens: 230_695 },
        modelUsage: {
          'claude-haiku-4-5-20251001': {
            inputTokens: 1510,
            outputTokens: 107_376,
            cacheReadInputTokens: 6_377_219,
            cacheCreationInputTokens: 453_763,
          },
        },
      },
      null,
      false,
    );
    assert.equal(patch.usage?.cacheReadTokens, 6_377_219);
    assert.equal(patch.usage?.outputTokens, 107_376);
  });

  it('sums across models when a run used more than one', () => {
    const patch = telemetryFrom(
      {
        ...REAL_RESULT,
        modelUsage: {
          a: { inputTokens: 100, outputTokens: 10, cacheReadInputTokens: 1, cacheCreationInputTokens: 2 },
          b: { inputTokens: 200, outputTokens: 20, cacheReadInputTokens: 3, cacheCreationInputTokens: 4 },
        },
      },
      null,
      false,
    );
    assert.equal(patch.usage?.inputTokens, 300);
    assert.equal(patch.usage?.outputTokens, 30);
    assert.equal(patch.usage?.cacheReadTokens, 4);
    assert.equal(patch.usage?.cacheCreationTokens, 6);
  });

  it('marks a success', () => {
    const patch = telemetryFrom({ ...REAL_RESULT, subtype: 'success', errors: [] }, null, false);
    assert.equal(patch.status, 'success');
  });

  it('marks an aborted run stopped rather than failed', () => {
    assert.equal(telemetryFrom(REAL_RESULT, null, true).status, 'stopped');
    assert.equal(telemetryFrom(null, null, true).status, 'stopped');
  });

  it('still closes out a run that produced no result at all', () => {
    const patch = telemetryFrom(null, new Error('spawn failed'), false);
    assert.equal(patch.status, 'error');
    assert.deepEqual(patch.errors, ['spawn failed']);
  });
});

describe('the prompts the app hands to the skills', () => {
  it('tells a distill run the ingest already happened', () => {
    const prompt = distillPrompt('learning-agent', ['evals/MISSION.md']);
    assert.match(prompt, /\/kb-distill/);
    assert.match(prompt, /ALREADY been run/);
    assert.match(prompt, /learning-agent/);
    assert.match(prompt, /Never push/);
  });

  it('names this round\'s files, because inbox/ holds every earlier round too', () => {
    // The defect this pins: the prompt used to carry the source name alone. On
    // a source ingested more than once that reads as "re-distill everything you
    // can see", because `kb ingest` never empties inbox/, so a second press
    // burned a full run rewriting notes that already existed.
    const prompt = distillPrompt('learning-agent', [
      'evals/MISSION.md',
      'wearable-hardware/NOTES.md',
    ]);
    assert.match(prompt, /copied 2 files/);
    assert.match(prompt, /- evals\/MISSION\.md/);
    assert.match(prompt, /- wearable-hardware\/NOTES\.md/);
    // Naming them is only half of it. Without this line the run still has no
    // reason to think the other files in inbox/ are off limits.
    assert.match(prompt, /not on the list above is not yours to distill/);
  });

  it('tells a distill run with nothing new to stop rather than re-distill', () => {
    // `kb ingest` exits 0 when the cursor did not move, so the endpoint fires a
    // run either way. With an empty list the prompt is the only thing standing
    // between a stray second click and a duplicate-note run.
    const prompt = distillPrompt('learning-agent', []);
    assert.match(prompt, /copied nothing/);
    assert.match(prompt, /Write no notes, commit nothing/);
    // It must not also be telling the run to go do the distillation, neither
    // in the material list nor in the opening imperative.
    assert.doesNotMatch(prompt, /this round's material/);
    assert.doesNotMatch(prompt, /^Run the \/kb-distill skill/);
    assert.match(prompt, /confirm that and stop/);
  });

  it('hands a settle run the batch and leaves the rules to the skill', () => {
    const prompt = settlePrompt(['a-slug', 'b-slug']);
    assert.match(prompt, /\/kb-settle/);
    assert.match(prompt, /- a-slug/);
    assert.match(prompt, /- b-slug/);
    // The approve button IS criterion 3, and the count is the batch fact the
    // skill cannot know.
    assert.match(prompt, /Criterion 3 was recorded in the bench for each of the 2 notes/);
    // The point of this test: the prompt must not restate the settling rules.
    // It used to, in words that contradicted the skill it loads (the skill
    // forbids batch-settling outright). Whoever edits this prompt next has to
    // put the rule in the skill instead.
    assert.match(prompt, /the skill wins/);
    assert.doesNotMatch(prompt, /flip it to settled/i);
    assert.doesNotMatch(prompt, /Do not stop to ask/i);
  });

  it('hands a rewrite run the whole thread, oldest first', () => {
    const prompt = rewritePrompt([
      {
        slug: 'n',
        comments: [
          { at: '2026-08-13T01:00:00Z', author: 'maintainer', text: 'first point', blob: 'x' },
          { at: '2026-08-13T02:00:00Z', author: 'maintainer', text: 'second point', blob: 'x' },
        ],
      },
    ]);
    assert.match(prompt, /first point/);
    assert.match(prompt, /second point/);
    assert.ok(prompt.indexOf('first point') < prompt.indexOf('second point'));
    // A rewritten note goes back to the queue because the skill says so, not
    // because this prompt orders it. The prompt must not restate the rule.
    assert.doesNotMatch(prompt, /stays unsettled/);
    assert.match(prompt, /the skill wins/);
  });

  it('does not crash on a declined note with no comment recorded', () => {
    assert.match(rewritePrompt([{ slug: 'n', comments: [] }]), /no comment recorded/);
  });
});

/**
 * The prompts point at sections of the skill by name. A rename in the skill
 * would leave the prompts citing law that no longer exists, and an unattended
 * run has nobody to notice. This is the mechanized version of "the prompt and
 * the skill agree" (design doc section 7.6).
 */
describe('the settle skill and the prompts that cite it', () => {
  const skill = readFileSync(join(REPO_ROOT, '.claude', 'skills', 'kb-settle', 'SKILL.md'), 'utf8');

  it('still carries both app entry points the design requires', () => {
    assert.match(skill, /^## 两个入口，法都在这里$/m);
    assert.match(skill, /^### 审理台入口：批量转 settled$/m);
    assert.match(skill, /^### 审理台入口：按留言改写$/m);
  });

  it('still forbids the agent judging criterion 3 itself', () => {
    assert.match(skill, /本 skill 最大的违规/);
    assert.match(skill, /不要拿运行提示词当法/);
  });

  it('still requires a rewritten note to go back to the queue unsettled', () => {
    assert.match(skill, /被你改写过的笔记不当场转 settled/);
  });

  it('cites section headings that exist in the skill', () => {
    for (const heading of ['两个入口，法都在这里', '审理台入口：批量转 settled']) {
      assert.ok(settlePrompt(['x']).includes(heading), `settle prompt cites ${heading}`);
      assert.ok(skill.includes(heading), `skill defines ${heading}`);
    }
    assert.ok(rewritePrompt([{ slug: 'x', comments: [] }]).includes('审理台入口：按留言改写'));
    assert.ok(skill.includes('审理台入口：按留言改写'));
  });
});
