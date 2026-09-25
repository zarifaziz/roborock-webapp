"""Run: uv run --with playwright python tests/browser/map_interactions.py.

Uses installed Chrome and intercepts every request; never contacts a robot/server.
"""

import asyncio
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
        await self.page.evaluate("refreshMap()")
        await self.page.wait_for_function("document.getElementById('map').naturalWidth === 600")
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
        await self.page.evaluate("refreshMap()")
        await self.page.wait_for_timeout(150)
        self.assertEqual(await self.view(), before)

    async def test_map_polls_again_three_seconds_after_completion(self):
        before = self.map_requests
        await self.page.wait_for_timeout(3300)
        self.assertEqual(self.map_requests, before + 1)

    async def test_slow_map_request_is_not_restarted(self):
        self.map_delay = 3.5
        before = self.map_requests
        await self.page.evaluate("refreshMap()")
        await self.page.wait_for_timeout(100)
        await self.page.evaluate("refreshMap()")
        await self.page.wait_for_timeout(3100)
        self.assertEqual(self.map_requests, before + 1)
        await self.page.wait_for_timeout(500)
        self.assertFalse(await self.page.evaluate("mapLoading"))

    async def test_failed_map_request_retries(self):
        self.map_error = True
        before = self.map_requests
        await self.page.evaluate("refreshMap()")
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
