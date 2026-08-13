"""Adapters and their wiring: pick the concrete gateway from config."""

from __future__ import annotations

from roborock_auto.config import Config
from roborock_auto.ports import RobotGateway
from roborock_auto.ports.auth import AuthPort


def get_gateway(config: Config) -> RobotGateway:
    if config.gateway == "live":
        from roborock_auto.adapters.roborock_cloud import RoborockCloudAdapter

        return RoborockCloudAdapter(config)
    from roborock_auto.adapters.fake import FakeRobot

    return FakeRobot()


def get_auth(config: Config) -> AuthPort:
    if config.gateway == "live":
        from roborock_auto.adapters.roborock_cloud import RoborockCloudAuth

        return RoborockCloudAuth(config)
    from roborock_auto.adapters.fake import FakeAuth

    return FakeAuth()
