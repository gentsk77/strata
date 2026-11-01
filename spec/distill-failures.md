---
what:   The failure log. One line per source that could not be located, could not be read, or was judged not worth keeping
why:    Failures are an output just like notes (note-spec rule 4). Without this log the same unusable material gets retried again and again
status: in effect, append only
date:   2026-08-11
---

# Distill failures

> Each row records: date / source uri / failure type / reason.
> Three failure types: the source could not be located, the source could not be read, the owner judged it not worth keeping.
> Rule numbers and file paths in a row are the ones in force **on the day the row was written**; they are not rewritten when the rules change later.
> **If a row is wrong, do not edit it.** Append a row of type `correction` that names the row it corrects, what was wrong and what is true. The log is useful only as an unedited history.
> Paths like `notes/...` resolve against the knowledge base root that `python -m kb root` prints.

| Date | Source uri | Type | Reason |
|---|---|---|---|
