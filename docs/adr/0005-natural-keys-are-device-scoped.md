# ADR 0005: Natural keys are device-scoped, without building multi-device support

**Status:** accepted

## Context

Segment numbers come from a robot's own map. Segment 16 on a Qrevo has nothing to do with
segment 16 on any other device. The app has always had exactly one robot, so this
distinction has never mattered and the vocabulary quietly assumed segment ids were global.

Scheduling is the first feature to persist segment ids. `room_intervals PRIMARY KEY
(segment_id)` would bake "globally unique" into a primary key on live data.

Everything else about supporting a second Roborock already works. `get_gateway(config)`
selects the adapter, every port is a Protocol, and `DeviceCapabilities` / `QREVO_L_PRO`
(`ARCHITECTURE.md:177-199`) already models capability variance. A second device is a new
adapter package and a capability profile.

## Decision

Natural keys in persisted tables are `(device_id, segment_id)`, with exactly one device id
present. Nothing else changes: no device registry, no device selection in the UI, no
multi-device wiring, no speculative branching.

This is insurance against the single change in this design that would be genuinely
expensive rather than merely annoying, a primary-key migration on data the user cares
about. It costs one column and no conceptual load.

## Consequences

`ARCHITECTURE.md:293-296` says to resist abstraction on spec, and this decision sits close
to that line. The distinction drawn is between flexibility that is speculative, which is
cut, and a schema shape whose absence forces a migration later, which is bought. Only the
key shape qualifies. Every other multi-device concern is deferred entirely.
