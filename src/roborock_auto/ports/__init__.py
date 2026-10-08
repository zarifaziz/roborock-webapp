"""The seams: Protocols the domain and application need the world to honor.

Consumers depend on the narrowest port that covers their role; RobotGateway is the
everything-union for wiring and the control panel, which genuinely uses it all.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from roborock_auto.ports.auth import AuthPort
from roborock_auto.ports.cleaning_history import CleaningHistoryPort
from roborock_auto.ports.cleaning_log import CleaningLogPort
from roborock_auto.ports.lifecycle import ConnectionPort
from roborock_auto.ports.maintenance import MaintenancePort
from roborock_auto.ports.mapping import MapPort
from roborock_auto.ports.occurrences import OccurrencePort
from roborock_auto.ports.onboarding import OnboardingPort
from roborock_auto.ports.robot import (
    CleaningControlPort,
    CleaningSettingsPort,
    RobotStatusPort,
    RoutinePort,
)
from roborock_auto.ports.room_intervals import RoomIntervalPort
from roborock_auto.ports.schedule_rules import ScheduleRulePort
from roborock_auto.ports.sequences import SequencePort

__all__ = [
    "AuthPort",
    "CleaningControlPort",
    "CleaningHistoryPort",
    "CleaningLogPort",
    "CleaningSettingsPort",
    "ConnectionPort",
    "MaintenancePort",
    "MapPort",
    "OccurrencePort",
    "OnboardingPort",
    "RobotGateway",
    "RobotStatusPort",
    "RoomIntervalPort",
    "RoutinePort",
    "ScheduleRulePort",
    "SequencePort",
]


@runtime_checkable
class RobotGateway(
    ConnectionPort,
    RobotStatusPort,
    CleaningControlPort,
    CleaningSettingsPort,
    CleaningHistoryPort,
    RoutinePort,
    MapPort,
    MaintenancePort,
    OnboardingPort,
    Protocol,
):
    """Union of all ports; every concrete adapter implements this."""
