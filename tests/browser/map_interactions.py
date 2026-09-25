"""Run: uv run --with playwright python tests/browser/map_interactions.py.

Uses installed Chrome and intercepts every request; never contacts a robot/server.
"""

import asyncio
import json
import math
import unittest
from pathlib import Path

from playwright.async_api import async_playwright

HTML = Path(__file__).parents[2] / "src/roborock_auto/adapters/web/static/index.html"
MAP = '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="400"><rect width="600" height="400" fill="tan"/></svg>'


class MapInteractions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(channel="chrome")
        self.page = await self.browser.new_page(viewport={"width": 1440, "height": 1000})
        self.map_delay = 0
        self.map_requests = 0
        self.map_error = False

        async def route(request):
            path = request.request.url.split("test.invalid", 1)[-1]
            if path == "/":
                await request.fulfill(content_type="text/html", body=HTML.read_text())
            elif path.startswith("/map.png"):
                self.map_requests += 1
                await asyncio.sleep(self.map_delay)
                if self.map_error:
                    self.map_error = False
                    await request.fulfill(status=503, body="unavailable")
                else:
                    await request.fulfill(content_type="image/svg+xml", body=MAP)
            else:
                await request.fulfill(status=503, content_type="application/json", body="{}")

        await self.page.route("**/*", route)
        await self.page.goto("http://test.invalid/")
        await self.page.wait_for_function("document.getElementById('map').naturalWidth === 600 && !mapLoading")
        self.stage = self.page.locator("#map-stage")

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def view(self):
        return await self.page.evaluate("({scale: zScale, x: zTx, y: zTy})")

    async def wheel(self, delta, modifier=False):
        box = await self.stage.bounding_box()
        x, y = round(box["x"] + box["width"] / 2), round(box["y"] + box["height"] / 2)
        await self.page.mouse.move(x, y)
        if modifier:
            await self.page.keyboard.down("Control")
        await self.page.mouse.wheel(0, delta)
        await self.page.wait_for_timeout(100)
        if modifier:
            await self.page.keyboard.up("Control")
        return x - box["x"], y - box["y"]

    async def test_robot_activity_states_and_missing_position(self):
        await self.page.evaluate("setRobotMapData({robot:{x:300,y:200},image_width:600,image_height:400})")
        for kind, label in [("drying", "Drying mop"), ("washing", "Washing mop"), ("returning", "Heading home"), ("emptying", "Emptying dustbin"), ("vacuuming", "Vacuuming"), ("mopping", "Mopping"), ("charging", "Charging"), ("paused", "Paused"), ("error", "Needs attention")]:
            await self.page.evaluate("activity => setRobotActivity(activity)", {"kind": kind, "label": label})
            self.assertEqual(await self.page.locator("#robot-activity").evaluate("el => ({hidden:el.hidden,kind:el.dataset.kind,label:el.textContent.trim()})"), {"hidden": False, "kind": kind, "label": label})
        await self.page.evaluate("setRobotMapData({robot:null,image_width:600,image_height:400})")
        self.assertTrue(await self.page.locator("#robot-activity").evaluate("el => el.hidden"))

    async def test_robot_activity_follows_position_and_stays_upright(self):
        await self.page.evaluate("setRobotMapData({robot:{x:180,y:160},image_width:600,image_height:400}); setRobotActivity({kind:'mopping',label:'Mopping'})")
        anchor = self.page.locator("#robot-activity")
        self.assertEqual(await anchor.evaluate("el => ({left:el.style.left,top:el.style.top,pointer:getComputedStyle(el).pointerEvents})"), {"left": "30%", "top": "40%", "pointer": "none"})
        await self.page.get_by_title("Rotate map 90°").click()
        await self.wheel(-80)
        await self.page.wait_for_timeout(400)
        matrix = await self.page.locator(".robot-activity-upright").evaluate("el => {const m=new DOMMatrix(getComputedStyle(el).transform);return {a:m.a,b:m.b,c:m.c,d:m.d}}")
        scale = (await self.view())["scale"]
        self.assertAlmostEqual(matrix["a"], 0, places=5)
        self.assertAlmostEqual(matrix["b"], -1 / scale, places=5)
        self.assertAlmostEqual(matrix["c"], 1 / scale, places=5)
        self.assertAlmostEqual(matrix["d"], 0, places=5)

    async def test_activity_bubble_stays_inside_mobile_map_edges(self):
        await self.page.set_viewport_size({"width": 390, "height": 844})
        await self.page.evaluate("setRobotMapData({robot:{x:12,y:5},image_width:600,image_height:400}); setRobotActivity({kind:'vacuuming',label:'Vacuuming & mopping'})")
        await self.page.wait_for_timeout(100)
        result = await self.page.locator("#robot-activity").evaluate("el => {const b=el.querySelector('.robot-activity-bubble').getBoundingClientRect(),s=document.getElementById('map-stage').getBoundingClientRect();return {placement:el.dataset.placement,inside:b.left>=s.left && b.right<=s.right && b.top>=s.top && b.bottom<=s.bottom}}")
        self.assertEqual(result, {"placement": "below", "inside": True})

    async def test_slow_refresh_keeps_map_visible_on_mobile_resize(self):
        self.map_delay = 1.5
        await self.page.evaluate("void refreshMap()")
        await self.page.set_viewport_size({"width": 390, "height": 844})
        self.assertEqual(await self.page.locator("#map").evaluate("img => ({width:img.naturalWidth,complete:img.complete})"), {"width": 600, "complete": True})

    async def test_closed_account_sheet_is_not_visible(self):
        await self.page.set_viewport_size({"width": 390, "height": 844})
        self.assertFalse(await self.page.locator("#connect-sheet").is_visible())

    async def test_offline_activity_stays_hidden_when_map_refreshes(self):
        await self.page.evaluate("setRobotMapData({robot:{x:300,y:200},image_width:600,image_height:400}); setRobotActivity({kind:'mopping',label:'Mopping'}); setOffline(true); setRobotMapData({robot:{x:310,y:210},image_width:600,image_height:400})")
        self.assertTrue(await self.page.locator("#robot-activity").evaluate("el => el.hidden"))

    async def test_activity_uses_actual_mode_and_dock_telemetry(self):
        cases = [
            ({"state_name": "charging", "drying_mop": True}, {"kind": "drying", "label": "Drying mop"}),
            ({"state_name": "washing_the_mop", "drying_mop": True}, {"kind": "washing", "label": "Washing mop"}),
            ({"state_name": "going_to_wash_the_mop"}, {"kind": "returning", "label": "Returning to wash"}),
            ({"state_name": "docking"}, {"kind": "returning", "label": "Heading home"}),
            ({"state_name": "emptying_the_bin"}, {"kind": "emptying", "label": "Emptying dustbin"}),
            ({"state_name": "cleaning", "cleaning": True, "cleaning_mode": "mop"}, {"kind": "mopping", "label": "Mopping"}),
            ({"state_name": "cleaning", "cleaning": True, "cleaning_mode": "vac_and_mop"}, {"kind": "vacuuming", "label": "Vacuuming & mopping"}),
            ({"state_name": "cleaning", "cleaning": True, "cleaning_mode": "vacuum"}, {"kind": "vacuuming", "label": "Vacuuming"}),
            ({"state_name": "charging", "drying_mop": None}, {"kind": "charging", "label": "Charging"}),
            ({"state_name": "charging_complete", "docked": True}, {"kind": "idle", "label": "Ready to clean"}),
            ({"state_name": "cleaning", "cleaning": True, "cleaning_mode": "vacuum", "drying_mop": True}, {"kind": "vacuuming", "label": "Vacuuming"}),
            ({"state_name": "attaching_the_mop", "drying_mop": True}, {"kind": "washing", "label": "Attaching mop"}),
            ({"state_name": "detaching_the_mop", "drying_mop": True}, {"kind": "washing", "label": "Removing mop"}),
            ({"state_name": "paused"}, {"kind": "paused", "label": "Paused"}),
            ({"state_name": "error", "error": "Stuck", "drying_mop": True}, {"kind": "error", "label": "Needs attention"}),
            ({"state_name": "charging", "dock_error": "Empty water tank"}, {"kind": "error", "label": "Needs attention"}),
        ]
        self.assertEqual(await self.page.evaluate("cases => cases.map(s => activityForStatus(s))", [status for status, _ in cases]), [expected for _, expected in cases])

    async def test_wheel_zoom_is_gentle_and_large_events_are_capped(self):
        await self.wheel(-120)
        self.assertAlmostEqual((await self.view())["scale"], math.exp(0.12), places=6)
        await self.wheel(-1000)
        self.assertAlmostEqual((await self.view())["scale"], math.exp(0.27), places=6)

    async def test_plain_wheel_zooms_at_cursor_without_scrolling_page(self):
        before_scroll = await self.page.evaluate("scrollY")
        x, y = await self.wheel(-80)
        view = await self.view()
        self.assertGreater(view["scale"], 1)
        self.assertAlmostEqual(view["x"], x * (1 - view["scale"]), places=4)
        self.assertAlmostEqual(view["y"], y * (1 - view["scale"]), places=4)
        self.assertEqual(await self.page.evaluate("scrollY"), before_scroll)
        await self.wheel(1000)
        self.assertEqual(await self.view(), {"scale": 1, "x": 0, "y": 0})

    async def test_reset_view_is_discoverable_at_default(self):
        self.assertTrue(await self.page.locator("#map-reset").is_visible())

    async def test_reset_restores_zoom_and_center_after_drag(self):
        await self.wheel(-80, modifier=True)
        before = await self.view()
        await self.page.mouse.down()
        await self.page.mouse.move(720, 400, steps=10)
        await self.page.mouse.up()
        self.assertNotEqual(await self.view(), before)
        await self.page.locator("#map-reset").click()
        self.assertEqual(await self.view(), {"scale": 1, "x": 0, "y": 0})

    async def test_refresh_preserves_zoom_and_pan(self):
        await self.wheel(-80, modifier=True)
        before = await self.view()
        await self.page.evaluate("void refreshMap()")
        await self.page.wait_for_timeout(150)
        self.assertEqual(await self.view(), before)

    async def test_map_polls_again_three_seconds_after_completion(self):
        before = self.map_requests
        await self.page.wait_for_timeout(3300)
        self.assertEqual(self.map_requests, before + 1)

    async def test_slow_map_request_is_not_restarted(self):
        self.map_delay = 3.5
        before = self.map_requests
        await self.page.evaluate("void refreshMap()")
        await self.page.wait_for_timeout(100)
        await self.page.evaluate("void refreshMap()")
        await self.page.wait_for_timeout(3100)
        self.assertEqual(self.map_requests, before + 1)
        await self.page.wait_for_timeout(500)
        self.assertFalse(await self.page.evaluate("mapLoading"))

    async def status_response(self, **overrides):
        status = {"state_name": "charging", "battery": 100, "docked": True, "in_cleaning": False, "cleaning": False, "error": None, "dock_error": None, "drying_mop": True, "map_ok": True, **overrides}
        await self.page.route("**/status", lambda route: route.fulfill(content_type="application/json", body=json.dumps(status)))
        await self.page.evaluate("appDataLoaded = true; refreshStatus()")

    async def test_failed_map_badge_does_not_overflow_mobile(self):
        await self.page.set_viewport_size({"width": 393, "height": 852})
        self.map_error = True
        await self.page.evaluate("refreshMap()")
        result = await self.page.evaluate("""() => {
          const head = document.querySelector('.map-head').getBoundingClientRect();
          const controls = [...document.querySelectorAll('.map-head button')].map(el => el.getBoundingClientRect());
          return {overflow: document.documentElement.scrollWidth > innerWidth,
            controlsInside: controls.every(r => r.left >= head.left && r.right <= head.right)};
        }""")
        self.assertEqual(result, {"overflow": False, "controlsInside": True})

    async def test_failed_map_badge_survives_status_and_clears_on_recovery(self):
        self.map_error = True
        await self.page.evaluate("refreshMap()")
        await self.status_response()
        self.assertEqual(await self.page.locator("#map-badge").evaluate("el => ({visible:getComputedStyle(el).display !== 'none',text:el.textContent})"), {"visible": True, "text": "Map update unavailable — retrying"})
        await self.page.evaluate("refreshMap()")
        self.assertFalse(await self.page.locator("#map-badge").is_visible())

    async def test_status_pills_only_show_current_activity(self):
        result = []
        for status in [{}, {"state_name": "cleaning", "docked": False, "cleaning": True, "in_cleaning": True, "drying_mop": False}]:
            await self.status_response(**status)
            result.append(await self.page.locator("#p-cleaning, #p-docked").evaluate_all("els => els.filter(el => !el.hidden).map(el => el.textContent)"))
        self.assertEqual(result, [["docked"], ["cleaning"]])

    async def test_failed_map_request_retries(self):
        self.map_error = True
        before = self.map_requests
        await self.page.evaluate("void refreshMap()")
        await self.page.wait_for_timeout(3300)
        self.assertEqual(self.map_requests, before + 2)
        self.assertEqual(await self.page.locator("#map").evaluate("img => img.naturalWidth"), 600)

    async def test_reset_centers_rotated_map_and_preserves_rotation(self):
        await self.page.get_by_title("Rotate map 90°").click()
        await self.wheel(-80, modifier=True)
        await self.page.locator("#map-reset").click()
        self.assertEqual(await self.view(), {"scale": 1, "x": 0, "y": 0})
        self.assertEqual(await self.page.evaluate("rot"), 90)


if __name__ == "__main__":
    unittest.main(verbosity=2)
