---
what:   The six front-matter fields of a note: allowed values and how to judge each
why:    Front matter is the only structured layer in the knowledge base. Each rule must be decidable in one sentence, or the field rots
status: in effect
date:   2026-08-27
---

# Field spec

## Before you read

- **Context**: this file is the field appendix to [note-spec.md](note-spec.md). It is the one source for the closed value sets and the judging rules. **Do not add fields and do not add values.**
- **Terms**: a *source note* holds the substance of one source document, at most one note per source. An *idea note* holds one idea of the owner's own, and its `evidence` is always `inferred` (note-spec rule 2).

## 1. All fields

```yaml
---
id: zettelkasten
title: Zettelkasten: Niklas Luhmann's slip box of numbered, cross-referenced index cards
evidence: revealed
status: unsettled
event_date: 2026-07-30
source: Niklas Luhmann, Kommunikation mit Zettelkaesten (1981), read in English translation
---
```

| Field | Answers | Values | Required |
|---|---|---|---|
| `id` | Which note is this | English kebab-case slug, equal to the file name, **never renamed**. On a collision add a distinguishing word, not a number | yes |
| `title` | What is this | Name style: the name of the thing, a colon, then one clause that says what it is (see the example in [note-spec.md](note-spec.md)). Source notes and idea notes use the same style | yes |
| `evidence` | Why believe it | `revealed` / `stated` / `inferred`, see §2 | yes |
| `status` | Has it been digested | `unsettled` / `settled`, see §3 | yes |
| `event_date` | When did it happen | An absolute date. Not the date it entered the knowledge base; git records that | yes |
| `source` | Where is the original | One line that leads back to the original | yes |
| `artifacts` | Where the living tool it describes is now | A list of workspace-relative paths | no |

## 2. `evidence`: what ultimately supports the note

Three tiers, strongest first. Record what the claim **ultimately rests on**, not the activity that produced it.

| Value | Rule (decidable in one sentence) |
|---|---|
| `revealed` | **Someone did it, or paid for it.** Built a workaround, bought a tool, kept a practice up for years. Behaviour does not lie |
| `stated` | **Someone said it.** An interview answer, a talk, an article, a product claim. The words alone cannot tell sincere from polite |
| `inferred` | **Only in my head.** My own reasoning; nobody outside has done it or said it |

The tier depends on **whether the support is a behaviour or a number**, not on who reports it: a source describing what people actually did is `revealed`; experimental results, survey statistics and reported percentages are numbers, so `stated`. "Something does not exist" is `stated`. This paragraph only draws the line between `revealed` and `stated`; `inferred` is decided by **who produced the claim**, not by what supports it.

Upgrades (`inferred` to `stated` to `revealed`) edit the field directly. There is no upgrade-date field; `git log -p` records every change and when it happened.

## 3. `status`: has the note been digested

| Value | Meaning |
|---|---|
| `unsettled` | Just captured, not yet checked against the rest of the knowledge base |
| `settled` | Reviewed |

Everything written outside `/kb-settle` (that is, by `/kb-note` and `/kb-distill`) is written as `unsettled`. Promotion to `settled` is a deliberate review and **needs all three criteria**:

1. It is **one unit**: a source note covers one source, an idea note holds one idea, nothing is mixed in (note-spec rule 2).
2. Its **relations are right**: what should be linked is linked, wrong links are fixed.
3. **The owner still agrees with it.**

The command that runs this review is `/kb-settle`. It works only on `unsettled` notes already in the knowledge base and never touches outside material. In the review app, criterion 3 is the owner's Approve, recorded against the note's git blob hash: an approval covers exactly the version that was read.

A note the owner no longer wants is removed: an `unsettled` note is deleted (and links to it are removed from other notes); git history keeps it recoverable.

## 4. No topic field

Topics are computed from the link graph by `python -m kb hubs`. Do not add topic, tag or category fields.
