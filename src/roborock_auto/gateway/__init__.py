"""Gateway factory: picks the implementation from config."""

from __future__ import annotations

from roborock_auto.config import Config
from roborock_auto.gateway.base import Gateway


def get_gateway(config: Config) -> Gateway:
    if config.gateway == "live":
        from roborock_auto.gateway.live import LiveGateway

        return LiveGateway(config)
    from roborock_auto.gateway.fake import FakeGateway

    return FakeGateway()
