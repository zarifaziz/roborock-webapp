# ADR 0003: Last-cleaned is derived from Cleaning Runs, not stored per room

**Status:** accepted

## Context

Per-room intervals need to know when each segment was last cleaned. The obvious design is a
`room_cleans` table holding one `last_cleaned_at` per segment, updated whenever a clean
starts.

There are four ways a clean starts in this app: the whole-house button, a room click, a
two-stage clean, and the cron entrypoint, with a scheduled firing about to become a fifth.
A stored `last_cleaned_at` has to be written from every one of them. The first draft of the
scheduling spec wrote it from exactly one, so a two-stage clean of the entire flat left
every room still reading as overdue the next morning. The bug was not an oversight so much
as the predictable outcome of a value that several callers must remember to maintain.

## Decision

Record a Cleaning Run for every clean this app initiates, from any path, carrying its
source, its segments, its mode, and when it started. Per-room last-cleaned becomes a query
over that log rather than a stored value.

One write path, five callers, and no way for the overdue view to disagree with what
actually happened. Cleaning history, previously listed as a non-goal, falls out of the same
table at no extra cost.

This mirrors ADR 0002 one layer down: a clean was an implicit event too, and storing a
derived summary of it instead of the event itself is what let the summary drift.

## Consequences

Last-cleaned is a `MAX(started_at)` grouped by segment rather than a lookup, which is
irrelevant at this scale and worth pinning with a test rather than an index.

A Cleaning Run records that a clean was *started*, not that it finished. A clean that fails
halfway still marks its rooms cleaned. This is an accepted simplification, because knowing
a clean truly completed needs job monitoring the app does not own, and the overdue view
self-corrects on the next successful clean. Because the event is now stored rather than
summarised, adding a completion timestamp later is an additive change.
