# ADR 0006: No fake store; port contracts are tested against real adapters

**Status:** accepted

## Context

This repo treats its fake adapter as a first-class citizen (`ARCHITECTURE.md:241-244`), and
`FakeRobot` is what makes the two-stage tests and the account-free demo mode possible. A
reader would reasonably expect the new store port to get the same treatment, and would
wonder why it did not.

It does not, because the reason `FakeRobot` exists does not transfer. The real robot is
slow, remote, and stateful in wall-clock time. A local SQLite file is none of those.

The decisive argument is stronger than convenience. The store port's contract includes an
atomicity guarantee: claiming an Occurrence is
`UPDATE occurrences SET state='CLAIMED' WHERE id=? AND state='PENDING'`, and a row count of
one is the right to fire (ADR 0002). A dictionary-backed fake cannot honour that; Python
dict mutation is not a compare-and-swap. Such a fake would be substitutable in type but not
in behaviour, which is a Liskov violation whose failure mode is the worst available: a green
test suite over a scheduler that double-fires a physical robot.

## Decision

No fake store. Tests run against the real SQLite adapter with in-memory or temporary-file
databases, which also means datetime round-tripping, the enum and CSV encodings, and the
migration itself are exercised rather than bypassed.

The portability this would have bought is bought properly instead, with a port contract
suite parameterised over adapters. It runs against one adapter today. Adding Postgres, or an
in-memory store once it can genuinely honour the claim, means running the existing suite
against it.

## Consequences

Any adapter that cannot satisfy the claim semantics fails the contract suite rather than
passing quietly, which is the point. `test_fake_robot.py` already asserts a similar
"honors the full contract" idea informally; parameterising it is a natural follow-on and is
not done here.
