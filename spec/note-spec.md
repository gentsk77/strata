---
what:   The rules for notes/: what a legal note looks like
why:    Loaded in full at the moment a note is written, so it fits on one page
status: in effect
date:   2026-08-24
---

# Note spec

`notes/` is the knowledge base: one flat folder of markdown files, one note per file. Ask for its path with `python -m kb root` instead of hardcoding it. The six fields and how to judge each one are in [fields.md](fields.md); `python -m kb doctor` enforces them.

## Four rules

1. **Record only the owner's content.** What the owner said, agreed with, or what is in material the owner handed over. Your own opinions are proposals: ask, and write them only on a yes.
2. **One source gives at most one note, never split.** Everything worth keeping from a source lives in the body of that one note: verbatim excerpts, numbers, mechanisms, the points the owner said mattered, and at the end what the owner said while reading it. The owner's own ideas are separate: one note per idea, in the owner's own words (`evidence: inferred`), linking to the source note when the idea is about something read.
3. **Links only for real relations.** Link only to notes that exist, and be able to say in one sentence why each link is there. Zero links is normal, not a defect.
4. **When a note cannot be written, log it.** If the source cannot be located, cannot be read, or the owner judges it not worth keeping, add one line to [distill-failures.md](distill-failures.md).

## Example (one article, two ways to record it)

An article explains how a library card catalog works: one card per access point, cards filed under author, title and subject, a standard card size, and a century of manual upkeep.

| Bad | Good |
|---|---|
| Four notes: "Cards per access point", "Author and title files", "Standard card size", "Manual upkeep", each linking to the other three. Each holds one point and none of them holds the article; months later a search finds fragments, not the piece that was read. | One note: "Library card catalog: one card for every way a reader might look a book up". Two or three opening sentences say what it is; the body carries the article's excerpts and mechanisms in its own order; the end records what the owner said after reading it. If that remark later deserves to be cited on its own, it becomes a separate idea note that links back. |

How to lay out a note body is in the section on writing the body in [../.claude/skills/kb-distill/SKILL.md](../.claude/skills/kb-distill/SKILL.md).
