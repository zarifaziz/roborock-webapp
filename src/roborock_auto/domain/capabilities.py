"""What this device can do. One profile: we target the Qrevo L Pro, not a generic model."""

from __future__ import annotations

from dataclasses import dataclass

from roborock_auto.domain.models import CarpetMode


@dataclass(frozen=True)
class DeviceCapabilities:
    model: str
    has_camera: bool
    has_mop_wash_dock: bool
    has_drying: bool
    carpet_modes: frozenset[CarpetMode]


QREVO_L_PRO = DeviceCapabilities(
    model="Qrevo L Pro",
    has_camera=False,
    has_mop_wash_dock=True,
    has_drying=True,
    carpet_modes=frozenset({CarpetMode.RISE, CarpetMode.AVOID}),
)
