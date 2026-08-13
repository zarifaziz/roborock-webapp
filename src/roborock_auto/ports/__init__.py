"""The seams: Protocols the domain and application need the world to honor.

Consumers depend on the narrowest port that covers their role; RobotGateway is the
everything-union for wiring and the control panel, which genuinely uses it all.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from roborock_auto.ports.auth import AuthPort
from roborock_auto.ports.lifecycle import ConnectionPort
from roborock_auto.ports.maintenance import MaintenancePort
from roborock_auto.ports.mapping import MapPort
from roborock_auto.ports.robot import (
    CleaningControlPort,
    CleaningSettingsPort,
    RobotStatusPort,
    RoutinePort,
)

__all__ = [
    "AuthPort",
    "CleaningControlPort",
    "CleaningSettingsPort",
    "ConnectionPort",
    "MaintenancePort",
    "MapPort",
    "RobotGateway",
    "RobotStatusPort",
    "RoutinePort",
]


@runtime_checkable
class RobotGateway(
    ConnectionPort,
    RobotStatusPort,
    CleaningControlPort,
    CleaningSettingsPort,
    RoutinePort,
    MapPort,
    MaintenancePort,
    Protocol,
):
    """Union of all ports; every concrete adapter implements this."""
