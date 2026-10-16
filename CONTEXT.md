# Roborock Automation

One household, one robot (Qrevo L Pro). The app reads the robot's state, decides what
should happen, and commands it. The robot's firmware owns everything about the robot;
this app owns only what the firmware cannot answer, which is what *we* intended and when.

## Language

### Cleaning

**Segment**:
A room, as one robot's map numbers it. Segment numbers are scoped to a device, never
global: segment 16 on one robot has nothing to do with segment 16 on another.
_Avoid_: Room ID, zone

**Room**:
A segment plus its human name, as shown in the UI.
_Avoid_: Area

**Clean**:
One instruction to the robot to start cleaning, whole-house or a chosen set of segments.
_Avoid_: Job, run, task

**Cleaning Run**:
A record that this app started a clean, with its source, its segments, and when. The
durable trace of a Clean.
_Avoid_: Clean history, clean log

**Two-Stage Clean**:
The house-specific procedure that never wets carpet: a vacuum pass with carpet mode
Rise, then a mop pass with carpet mode Avoid, waiting for the robot to genuinely return
between them. It is the built-in two-Step Sequence, defined in code rather than saved,
because it follows whichever Rooms are selected at the time.
_Avoid_: Deep clean, full clean

**Step**:
One Clean inside a Sequence: whole-house or a chosen set of segments, a cleaning mode,
a carpet mode (Rise or Avoid), and its own fan power, water flow and mop route, each of
which may be Same as Setup. Example: "Vacuum · Master bedroom · carpet Rise · Turbo".

**Setup choice**:
The fan power, water flow and mop route the user picked in the Setup tab, stored by this
app. The robot cannot keep it: every Clean's mode switch resets all three, a vacuum-only
Clean leaves water off and a mop-only Clean leaves suction off. A setting never picked
here falls back to what the robot reports. A robot value with no button in this app (Max+,
Deep+, custom, smart) is not kept.

**Same as Setup**:
A Step setting, and every plain Clean, takes the Setup choice. Every Clean sends it again
after the mode switch: fan power only when the mode vacuums, water flow and mop route only
when it mops. Example: Setup is Turbo, Intense, Deep; a vacuum-then-mop Sequence leaves on
Turbo for the vacuum and Intense, Deep for the mop, on its first run and every run after.

**Sequence**:
A named, ordered list of Steps, each starting only once the robot is home and idle from
the one before. Every Sequence ends with carpet mode Avoid. The Two-Stage Clean is the
two-Step preset: vacuum with Rise, then mop with Avoid.
_Avoid_: Routine (the official app's saved routines), Recipe, Program, Chain

**Up next**:
The ordered list of Cleans the user asked for while the robot was busy. Each entry is a
list of Steps copied at the moment of the request (a plain Clean, the Two-Stage Clean, or a
saved Sequence's Steps). The head entry starts once the robot is home, idle and error-free
and no run holds the RunSlot. The list advances only after a success; a failure, a cancel,
or the user sending the robot home puts it on hold until the user resumes or clears it.
_Avoid_: Queue (in the UI), backlog, pending jobs, stack

**Departure**:
The robot having actually left the dock for the current job, as distinct from the dock
servicing itself after the previous one.

### Scheduling

**Schedule Rule**:
A user's standing intent, made of a Recurrence and an Action: when it should happen and
what should happen. It describes what should happen repeatedly; it never records what did.
_Avoid_: Schedule, cron, timer, trigger

**Recurrence**:
The "when" half of a Schedule Rule: a rule for generating instants. A weekly grid of
days plus a time of day is the only shape the app understands today.
_Avoid_: Frequency, pattern, cron expression

**Action**:
The "what" half of a Schedule Rule: a single Clean of a set of segments in a given mode, or
running a saved Sequence. In principle any other standing instruction to the robot.
_Avoid_: Job, command, task, payload

**Occurrence**:
One specific firing of one Schedule Rule at one instant, as a durable record with its own
lifecycle. "The 09:30 on Wednesday 26 August", not "the 09:30 rule".
_Avoid_: Fire, tick, event, instance

**Materialise**:
To generate the Occurrences a Schedule Rule implies over a forward horizon, so that a
firing is something the system holds rather than something it re-infers.

**Deadline**:
How stale an Occurrence may become before the system gives up on it rather than firing it
late. Belongs to the Occurrence, not to the scheduler.
_Avoid_: Grace, grace window, misfire window

**Room Interval**:
How often a segment should be cleaned, as a duration. A user's standing intent about a room,
the per-room counterpart to a Schedule Rule.
_Avoid_: Frequency, cadence, schedule

**Overdue**:
A segment whose time since its last Cleaning Run exceeds its Room Interval.

**Priority**:
How overdue a segment is, normalised as days elapsed divided by its Room Interval, so
rooms with different intervals compare directly. A never-cleaned room is maximally
overdue.
_Avoid_: Score, urgency, rank
