from dataclasses import asdict
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from roborock_auto.adapters.roborock_cloud.adapter import RoborockCloudAdapter


@pytest.mark.parametrize(('complete', 'error', 'outcome'), [
    (1, 0, 'completed'), (0, 0, 'incomplete'), (1, 3, 'incomplete'),
    (None, None, 'unknown'),
])
async def test_actual_history_does_not_infer_completion_from_docking(complete, error, outcome):
    trait = SimpleNamespace(refresh=AsyncMock(), last_clean_record=SimpleNamespace(
        begin_datetime=datetime(2026, 9, 25, 6, tzinfo=UTC),
        end_datetime=datetime(2026, 9, 25, 6, 10, tzinfo=UTC),
        duration=600, square_meter_area=12.4, complete=complete, error=error,
    ))
    gw = RoborockCloudAdapter(SimpleNamespace())
    gw._device = SimpleNamespace(v1_properties=SimpleNamespace(clean_summary=trait))
    record = await gw.get_last_clean_record()
    assert asdict(record) == {
        'started_at': datetime(2026, 9, 25, 6, tzinfo=UTC),
        'ended_at': datetime(2026, 9, 25, 6, 10, tzinfo=UTC),
        'duration_s': 600, 'area_m2': 12.4, 'outcome': outcome, 'error_code': error,
    }
    assert trait.refresh.await_count == 1


async def test_no_robot_history_is_not_a_completed_run():
    trait = SimpleNamespace(refresh=AsyncMock(), last_clean_record=None)
    gw = RoborockCloudAdapter(SimpleNamespace())
    gw._device = SimpleNamespace(v1_properties=SimpleNamespace(clean_summary=trait))
    assert await gw.get_last_clean_record() is None
