"""Driven persistence adapter: the only package that imports sqlite3."""

from roborock_auto.adapters.sqlite.cleaning_log import SqliteCleaningLog
from roborock_auto.adapters.sqlite.database import MIGRATIONS, apply_migrations, open_database
from roborock_auto.adapters.sqlite.occurrences import SqliteOccurrences
from roborock_auto.adapters.sqlite.room_intervals import SqliteRoomIntervals
from roborock_auto.adapters.sqlite.schedule_rules import SqliteScheduleRules
from roborock_auto.adapters.sqlite.sequences import SqliteSequences
from roborock_auto.adapters.sqlite.setup_settings import SqliteSetupSettings

__all__ = [
    "MIGRATIONS",
    "SqliteCleaningLog",
    "SqliteOccurrences",
    "SqliteRoomIntervals",
    "SqliteScheduleRules",
    "SqliteSequences",
    "SqliteSetupSettings",
    "apply_migrations",
    "open_database",
]
