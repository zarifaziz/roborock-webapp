"""Run: uv run --with playwright python tests/browser/sequence_editor.py.

All network requests are intercepted; no robot commands leave Chrome.
"""

import json
import unittest

import map_interactions


def step(mode, carpet, segment_ids, repeat):
    return {"mode": mode, "carpet": carpet, "segment_ids": segment_ids, "repeat": repeat,
            "fan_power": None, "water_mode": None, "mop_route": None}


class SequenceEditor(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = map_interactions.MapInteractions.asyncSetUp
    asyncTearDown = map_interactions.MapInteractions.asyncTearDown

    async def test_edit_and_save_keeps_each_steps_passes(self):
        steps = [step("vacuum", "rise", [16, 17], 3), step("mop", "avoid", [16], 2), step("vac_and_mop", "avoid", None, 1)]
        saved = []

        async def sequences(route):
            if route.request.method == "PUT":
                saved.append(route.request.post_data_json)
            await route.fulfill(content_type="application/json", body=json.dumps([{"id": 7, "name": "Deep clean", "steps": steps}]))
        await self.page.route("**/sequences", sequences)
        await self.page.route("**/sequences/7", sequences)
        await self.page.evaluate("roomsCache = [{segment_id: 16, name: 'Study'}, {segment_id: 17, name: 'Hall'}]; refreshSequences()")
        await self.page.get_by_role("button", name="Sequences", exact=True).click()
        await self.page.get_by_title("Edit this Sequence").click()
        await self.page.get_by_role("button", name="Save sequence").click()
        await self.page.wait_for_function("document.getElementById('seq-editor').hidden")
        self.assertEqual(saved, [{"name": "Deep clean", "steps": steps}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
