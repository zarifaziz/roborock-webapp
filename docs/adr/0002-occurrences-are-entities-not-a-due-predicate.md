# ADR 0002: Occurrences are entities, not a due-predicate

**Status:** accepted

## Context

The obvious way to build an in-process scheduler is a stateless predicate: every tick,
for every rule, ask `is_due(rule, now)` and fire if true. Nothing about a firing is
stored except a `last_run_at` on the rule.

That design was drafted, reviewed, and found to need five unrelated patches, all of which
turned out to be the same missing idea. A firing existed only as a moment in time that the
tick re-inferred from scratch, and you cannot attach anything to a moment:

- it needed a **state**, so `last_run_at` was bolted onto the rule
- it needed a **reason for not happening**, so `last_skipped_at` / `last_skip_reason` were proposed
- it needed a notion of **staleness**, so a `GRACE` constant was invented, and the value had
  to be tuned around how long a clean takes, which is unrelated to scheduling
- it needed **mutual exclusion**, so "fire at most one rule per tick" was imposed as a rule
  of the loop rather than a property of the work
- it needed a **lock**, so running more than one process had to be forbidden by documentation

The rule was modelled. The firing was not.

## Decision

Model the Occurrence as a first-class entity. A Schedule Rule is materialised forward over
a rolling horizon into Occurrence rows, each keyed uniquely by `(rule_id, scheduled_for)`
so materialising is idempotent, and each carrying its own state, attempt count, and
deadline.

The scheduler stops deciding and starts claiming. Firing is
`UPDATE occurrences SET state='RUNNING' WHERE id=? AND state='PENDING'`, and a row count of
one is the right to fire.

Each of the five patches becomes a property of that row. A busy robot leaves an Occurrence
`PENDING` instead of consuming a window. Skips are rows, so "did my 09:30 run yesterday" is
a query rather than a field someone remembered to add. Two rules due at once are two rows
that fire in sequence. A crash mid-fire leaves a row `RUNNING`, so it cannot storm a robot
that has just faulted. Two processes race on the claim and exactly one wins, which makes
multiple workers safe rather than forbidden.

Deciding due-ness backwards is no longer needed at all: Occurrences are only ever generated
forward, so the tricky "most recent occurrence at or before now" computation is never
written.

## Considered options

**Stateless due-predicate.** Fewer lines and no extra table. Rejected: the five patches
above are more total design than the entity is, and each one hides its state somewhere
different.

**A scheduling library (APScheduler, Quartz-style).** Rejected separately for its own
clock, which fights this repo's injected-clock test style, and for modelling recurrence far
beyond the weekly grid this app deliberately scoped itself to.

## Consequences

Editing or disabling a Schedule Rule must invalidate its outstanding `PENDING` Occurrences
and re-materialise. That is the one piece of real bookkeeping this design adds.

Occurrence rows accumulate and are deliberately never pruned. Three rules firing three
times a week is roughly 160 rows a year, so a decade is under two thousand rows in SQLite.
Retention code would delete data that answers "what did this thing do last winter" in order
to save nothing.
