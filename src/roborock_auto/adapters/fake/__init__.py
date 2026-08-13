"""In-memory driven adapter: test double and no-account demo backend."""

from roborock_auto.adapters.fake.fake_auth import FakeAuth
from roborock_auto.adapters.fake.fake_robot import FakeRobot

__all__ = ["FakeAuth", "FakeRobot"]
