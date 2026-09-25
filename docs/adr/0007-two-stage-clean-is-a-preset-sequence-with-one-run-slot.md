# ADR 0007: The Two-Stage Clean is a preset Sequence; one RunSlot; Sequence is a second Action kind

**Status:** accepted

## Context

The app could run exactly one multi-step clean, the Two-Stage Clean: vacuum with carpet
Rise, then mop with carpet Avoid, on the same Segments both times. The household needed a
different shape. Its motivating case:

```
Step 1  Vacuum          Bathroom, Bathroom1   carpet Rise    (bath mats get vacuumed)
Step 2  Vacuum + mop    whole house           carpet Avoid   (big carpets are never touched)
```

Neither the Two-Stage Clean nor a single Clean can express that. Users now build **Sequences**:
named, ordered lists of Steps, each a Clean with its own Segments, cleaning mode, and carpet
mode. Three questions followed: what happens to the Two-Stage Clean, how many runs may drive
the robot at once, and how a Schedule Rule fires a Sequence.

## Decision

**The Two-Stage Clean is a preset Sequence, built in code, not stored.**
`application/sequence_runner.py` runs any list of Steps and keeps every safety behaviour
the old two-stage code had (carpet set and read back, dropped-start re-issue, recharge
handling, stall timeout, dock + Avoid recovery). `run_two_stage` builds two Steps and
calls it:

```python
steps = (
    Step(mode=CleaningMode.VACUUM, carpet=CarpetMode.RISE, segment_ids=segments),
    Step(mode=CleaningMode.MOP, carpet=CarpetMode.AVOID, segment_ids=segments),
)
```

It is not a saved Sequence row, because the Clean button's two-stage run follows whichever
Rooms are selected at the moment of the tap, which a stored Sequence cannot. Its signature,
its endpoint, the cron entrypoint, and its stage tokens (`vacuuming`, `vacuum_waiting`,
`mopping`, `mop_waiting`, `done`) are unchanged.

**One RunSlot.** At most one run drives the robot. `application/run_slot.py` is a single
object with a synchronous `acquire`, the run's progress, a cancel event, and the task. The
web process creates one and hands the same instance to every endpoint and to the scheduler.
So a Sequence the scheduler starts at 10:00 on Saturday shows up in the app's progress view
and is cancellable there, and pressing the Two-Stage button during it gets a 409 ("another
run is already in progress") instead of a second run fighting over the robot.

**A Sequence is a second Action kind.** The Action union gains `SequenceAction(sequence_id)`,
persisted as `action_type = 'sequence'`, next to `CleanAction`. The HTTP shape stays
`action: {type, ...}`:

```json
{"name": "Saturday bathrooms", "recurrence": {"days": ["saturday"], "at": "10:00"},
 "action": {"type": "sequence", "sequence_id": 3}}
```

The scheduler takes the RunSlot, starts the run in the background, and marks the
Occurrence fired on the first Step's Departure. Deleting a Sequence a Rule fires returns 409
naming the Rule.

## Considered options

**Keep the Two-Stage Clean as its own process beside a new Sequence runner.** Two copies of
the depart/return, recharge, and recovery logic, which is the code that was hardest to get
right on the live robot. Rejected.

**Store the Two-Stage Clean as a seeded Sequence row.** It would lose the "selected Rooms"
behaviour of the Clean button, and a user could edit or delete the preset the cron job
depends on. Rejected.

**One slot per kind of run.** A Sequence and a Two-Stage Clean could then both think they
own the robot. Rejected; the slot is about the robot, not the kind of run.

## Consequences

Every multi-step run shares one progress record. The two status endpoints
(`/clean/two-stage/status`, `/sequences/progress`) read the same slot, and only a Sequence
run writes `sequence_id`, which is how the UI decides which card shows a run.

Every Sequence ends with a verified carpet Avoid, even after a successful one whose last
Step used Rise, because plain Cleans never touch carpet mode.

Run progress lives in memory: it survives closing the app, not a server restart.
