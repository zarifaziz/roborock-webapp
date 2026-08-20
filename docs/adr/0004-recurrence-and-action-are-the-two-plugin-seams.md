# ADR 0004: Recurrence and Action are the two plugin seams

**Status:** accepted

## Context

A Schedule Rule is structurally a pair: a Recurrence (when) and an Action (what). The first
draft hardcoded both, as days-of-week plus time-of-day, and mode plus segment ids, directly
on the rule and directly in the schema.

Hardcoding one half and abstracting the other would have been worse than hardcoding both,
because the model would then assert something false about the domain. `ARCHITECTURE.md`
names five actions across its ports (clean segments, dust collection, mop wash, drying, run
routine) and exactly one recurrence shape. In this domain, what varies more than when.

## Decision

Both halves are extension points.

**Recurrence** is a `RecurrenceSchedule` protocol with one method,
`occurrences_between(start, end, tz)`. The weekly grid is one implementation. The
materialiser asks for instants in a window and writes rows; it does not know what a week
is. "Every three days", "first Monday of the month", or an RFC 5545 `RRULE` implementation
become new classes rather than edits to the materialiser, the model, the schema, and the UI
together.

**Action** is a discriminated union with exactly one member today, `CleanAction(mode,
segment_ids)`, persisted as `action_type` plus `action_json`. Scheduling a mop wash becomes
additive.

## Considered options

**Hardcode both.** Adding a mop-wash rule then costs a schema migration plus a second
firing path. Not catastrophic, which is why this was a genuine judgement call rather than an
obvious one. Rejected on the symmetry argument above.

**Adopt `dateutil.rrule` now** (rejected separately, on the grounds that RFC 5545's breadth
is scope this app deliberately cut, and that it still hands back naive datetimes so the
timezone work remains ours either way). Note that the Recurrence seam makes that rejection
cheap to revisit: `RRule` would be one more implementation, not a redesign.

## Consequences

Storing the Action as JSON moves payload validation out of the schema and into the
application, which is a real loss. It is contained two ways: a `CHECK` constraint pins
`action_type` to the known set, and the adapter parses into the typed value object on read,
so a malformed row fails loudly at the boundary instead of leaking a dict into the domain.

The HTTP representation gains one level of nesting, `action: {type: "clean", ...}`, which
the UI must branch on. It would have had to branch the day a second action existed anyway.
