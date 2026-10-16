"""The Overdue view and the catch-up clean it drives.

Joins the two stores the view needs: the Room Intervals the user set, and the last-cleaned
times derived from Cleaning Runs (docs/adr/0003). The scoring itself is a domain policy;
this use case only fetches, and starts the catch-up clean through the shared clean use
case so it is recorded like any other.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from roborock_auto.application.start_clean import CleanPorts, start_clean, utc_now
from roborock_auto.domain.models import CleaningMode, CleanSource, SegmentOverdue
from roborock_auto.domain.policies import overdue_report
from roborock_auto.ports.cleaning_log import CleaningLogPort
from roborock_auto.ports.room_intervals import RoomIntervalPort
from roborock_auto.ports.setup_settings import SetupSettingsPort


async def overdue_view(
    *,
    intervals: RoomIntervalPort,
    cleaning_log: CleaningLogPort,
    now: Callable[[], datetime] = utc_now,
) -> list[SegmentOverdue]:
    configured = await intervals.list_intervals()
    last_cleaned = await cleaning_log.last_cleaned(i.segment_id for i in configured)
    return overdue_report(configured, last_cleaned, now())


async def clean_overdue(
    gw: CleanPorts,
    *,
    intervals: RoomIntervalPort,
    cleaning_log: CleaningLogPort,
    setup_store: SetupSettingsPort,
    source: CleanSource,
    mode: CleaningMode,
    now: Callable[[], datetime] = utc_now,
) -> list[int]:
    """Cleans every currently Overdue Segment, returning the ones sent to the robot."""
    due = [
        entry.segment_id
        for entry in await overdue_view(intervals=intervals, cleaning_log=cleaning_log, now=now)
        if entry.overdue
    ]
    if due:
        await start_clean(
            gw,
            cleaning_log=cleaning_log,
            setup_store=setup_store,
            source=source,
            mode=mode,
            segment_ids=due,
            now=now,
        )
    return due
