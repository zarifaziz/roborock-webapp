# ADR 0009: Up next is an in-memory queue that advances only after a success

**Status:** accepted

## Context

The user works in requests: "clean the Kitchen", then a minute later "and the Hall". While
the robot was out, the second request got a 409 from every entry point (`/start`,
`/clean/segments`, `/clean/two-stage`, `/sequences/{id}/run`), and the Clean button was a
Pause button. The user had to wait for the robot to come home and remember to ask again.

Two different things make the robot unable to take a request: a run holding the RunSlot
(a Sequence or the Two-Stage Clean, which sits docked between Steps), and a plain Clean,
which releases the slot at once and leaves the robot out cleaning. A queue has to wait on
both.

## Decision

**One in-memory list, Up next, in `application/clean_queue.py`.** An entry is a label, a
tuple of Steps, and the id of the saved Sequence the Steps were copied from, if any. A
plain Clean is one Step (now with `repeat`), the Two-Stage Clean is its two Steps, and a
Sequence contributes a copy of its Steps, so editing the Sequence later does not change
what was queued.

```
POST /queue  {"label": "Kitchen · Vacuum · 2 passes",
              "steps": [{"mode": "vacuum", "carpet": "avoid", "segment_ids": [17], "repeat": 2}]}
POST /queue  {"label": "Kitchen · Vacuum, then mop", "preset": "two-stage", "segment_ids": [17]}
POST /queue  {"sequence_id": 3}
GET  /queue  {"held": false, "hold_reason": null, "waiting_for": "the robot is mid-job",
              "entries": [...]}
```

**The head entry starts when the robot is ready and the slot is free.** The drain loop
polls `readiness_block` (the scheduler's gate) at the configured poll interval, requires
readiness on two consecutive polls like the runner does for departure and return, takes the
slot, and launches the entry through `SequenceLauncher`. Every queued Clean therefore gets
the carpet read-back, the dropped-start re-issue and the depart-then-return wait a
Sequence gets, and shows in the same progress record.

**The queue advances only after a success.** A failed run, a cancelled run, and the Stop,
Dock and Cancel endpoints all put it on hold with the reason. Nothing starts until the user
taps Resume or Clear. The rule exists for one case: the user stops the robot because guests
arrived, and two minutes later it leaves the dock again for the Hall. The first hold reason
wins, so the user's own Stop is what they read, not the cancel it caused.

**In memory, like run progress.** A restart drops the list. Persisting it would mean a
robot leaving the dock for a request the user made before the restart and may have
forgotten; dropping it is the safe failure.

## Considered options

**Auto-queue on 409.** Every start endpoint would queue instead of refusing. Rejected: a
refusal and a deferral are different promises, and the user must know which one they got.
The UI offers the queue explicitly (the Queue button, "Add to Up next" in the review
dialog, and a switch to queueing when a start comes back 409).

**Keep advancing after a cancel.** Simpler, and wrong for the only reason anyone cancels.

**Persist the queue in SQLite.** Rejected for the restart case above; the scheduler
already fails claimed Occurrences on restart for the same reason.

**Run plain queued Cleans through `start_clean` and poll for the return.** A second
copy of the depart and return logic. Rejected; the runner already does it.

## Consequences

`Step` gains `repeat` (1 to 3, chosen rooms only), persisted in the Sequence store and sent
in every Step body, so a saved Sequence can carry passes later without a schema change.
`SequenceLauncher.launch` takes Steps plus a name and an optional Sequence id, and run
progress now carries the `steps` it is running, so the UI labels a queued ad-hoc run
without a saved Sequence to look up.

The hold rule only sees stops made through this app. A queued run sent home from the
official app or the robot's button reads as home and idle, counts as a success, and the
next entry starts. Closing that gap needs a firmware signal for "returned early", which the
adapter does not map yet.

The scheduler and the queue can both see the robot ready at once. The slot's synchronous
`acquire` decides; the loser waits for the next poll.
