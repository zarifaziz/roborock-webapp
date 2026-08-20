"""Driven persistence adapter: the only package that imports sqlite3."""

from roborock_auto.adapters.sqlite.cleaning_log import SqliteCleaningLog
from roborock_auto.adapters.sqlite.database import MIGRATIONS, apply_migrations, open_database
from roborock_auto.adapters.sqlite.room_intervals import SqliteRoomIntervals

__all__ = [
    "MIGRATIONS",
    "SqliteCleaningLog",
    "SqliteRoomIntervals",
    "apply_migrations",
    "open_database",
]
