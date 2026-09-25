"""Run: uv run --with playwright python tests/browser/cleaning_confidence.py.

All network requests are intercepted; no robot commands leave Chrome.
"""
import asyncio
import json
import unittest
from urllib.parse import urlsplit

import map_interactions


class CleaningConfidence(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = map_interactions.MapInteractions.asyncSetUp
    asyncTearDown = map_interactions.MapInteractions.asyncTearDown
    status_response = map_interactions.MapInteractions.status_response

    async def load_controls(self, carpet_modes=None):
        rooms = [{"segment_id": 16, "name": "Study"}, {"segment_id": 17, "name": "Hall"}]
        fixtures = {
            "/capabilities": {"model": "Qrevo", "carpet_modes": carpet_modes or ["rise", "avoid"]},
            "/rooms": rooms, "/map-data": {"rooms": []}, "/routines": [], "/consumables": {},
            "/carpet": {"mode": "rise"}, "/assistant": {"enabled": True, "model": "sonnet"}, "/auth/status": {"mode": "fake", "connected": True},
        }
        async def fixture(route):
            await route.fulfill(content_type="application/json", body=json.dumps(fixtures[urlsplit(route.request.url).path]))
        for path in fixtures:
            await self.page.route("**" + path, fixture)
        await self.page.evaluate("loadAppData()")
        await self.status_response()
        await self.page.get_by_role("button", name="Rooms", exact=True).click()

    async def test_review_before_start_and_verified_settings_request(self):
        await self.load_controls()
        await self.page.locator('#rooms .chip[data-id="16"]').click()
        await self.page.locator('#seg-room-mode [data-v="mop"]').click()
        self.assertEqual(await self.page.locator('#seg-clean-carpet button').count(), 4)
        await self.page.locator('#seg-clean-carpet [data-v="avoid"]').click()
        await self.page.locator('#seg-passes [data-v="2"]').click()
        sent = []

        async def command(route):
            sent.append(route.request.post_data_json)
            await route.fulfill(content_type="application/json", body='{"ok":true}')
        await self.page.route('**/clean/segments', command)
        await self.page.locator('#two-stage-btn').click()
        self.assertEqual(sent, [])
        self.assertEqual(await self.page.locator('#clean-review-details').inner_text(),
                         'Rooms\nStudy\nMode\nMop\nCarpet\nAvoid — stay off carpet\nPasses\n2 per room')
        await self.page.locator('#confirm-clean').click()
        self.assertEqual(sent, [{"segment_ids": [16], "mode": "mop", "carpet_mode": "avoid", "repeat": 2}])
        await self.page.wait_for_function("document.getElementById('clean-request-feedback').textContent.length > 0")
        self.assertEqual(await self.page.locator('#clean-request-feedback').inner_text(), 'Settings verified. Clean requested.')
        self.assertEqual(await self.page.locator('#clean-btn').count(), 0)

    async def test_carpet_supported_options_and_whole_floor_pass_limit(self):
        await self.load_controls(["avoid"])
        self.assertEqual(await self.page.locator('#seg-clean-carpet button').evaluate_all("els=>els.map(el=>({value:el.dataset.v,disabled:el.disabled}))"), [{"value": "rise", "disabled": True}, {"value": "avoid", "disabled": True}, {"value": "ignore", "disabled": True}, {"value": "cross", "disabled": True}])
        await self.page.locator('#seg-room-mode [data-v="vacuum"]').click()
        self.assertEqual(await self.page.locator('#seg-passes button').evaluate_all("els=>els.map(el=>({value:el.dataset.v,disabled:el.disabled}))"), [{"value":"1","disabled":False},{"value":"2","disabled":True},{"value":"3","disabled":True}])
        self.assertEqual(await self.page.locator('#carpet-current').inner_text(), 'Last verified: Rise — include carpets, lift mop')

    async def test_freshness_delayed_and_zoom_percentage(self):
        self.assertEqual(await self.page.locator('#map-zoom-value').count(), 1)
        await self.page.evaluate('zoomAt(200,200,2)')
        self.assertEqual(await self.page.locator('#map-zoom-value').inner_text(), '200%')
        await self.page.evaluate('lastMapAt=Date.now()-25000; lastStatusAt=Date.now()-15000; renderFreshness()')
        self.assertEqual(await self.page.locator('#data-freshness').inner_text(), 'Map: 25s ago · delayed. Robot: 15s ago · delayed.')

    async def test_assistant_plan_requires_review_and_never_starts(self):
        await self.load_controls()
        await self.page.get_by_role('button', name='Clean', exact=True).first.click()
        self.assertEqual(await self.page.locator('#assistant-message').count(), 1)
        plan = {"kind":"plan", "message":"Mop the Study, avoiding carpet.", "plan":{"segment_ids":[16],"mode":"mop","carpet":"avoid","repeat":1}}
        await self.page.route('**/assistant', lambda route: route.fulfill(content_type='application/json',body=json.dumps(plan)))
        await self.page.locator('#assistant-message').fill('Mop study without touching carpet')
        await self.page.locator('#assistant-ask').click()
        await self.page.locator('#assistant-review').click()
        self.assertTrue(await self.page.locator('#clean-review').is_visible())
        self.assertEqual(await self.page.locator('#clean-review-details').inner_text(),
                         'Rooms\nStudy\nMode\nMop\nCarpet\nAvoid — stay off carpet\nPasses\n1 per room')
        await self.page.locator('#cancel-clean-review').click()
        self.assertFalse(await self.page.locator('#clean-review').is_visible())

    async def test_setting_mismatch_keeps_review_open_without_success(self):
        await self.load_controls()
        await self.page.locator('#rooms .chip[data-id="16"]').click()
        await self.page.locator('#seg-room-mode [data-v="mop"]').click()
        await self.page.route('**/clean/segments', lambda route: route.fulfill(status=409,content_type='application/json',body='{"detail":"Carpet setting was not applied; clean not started"}'))
        await self.page.locator('#two-stage-btn').click()
        await self.page.locator('#confirm-clean').click()
        await self.page.wait_for_function("document.getElementById('clean-review-error').textContent.length > 0")
        self.assertEqual(await self.page.evaluate("""() => ({open:document.getElementById('clean-review').open,
          error:document.getElementById('clean-review-error').textContent,
          success:document.getElementById('clean-request-feedback').textContent, active:activeClean})"""),
          {"open":True,"error":"Carpet setting was not applied; clean not started","success":"","active":None})

    async def test_whole_floor_mode_and_two_stage_summary(self):
        await self.load_controls()
        await self.page.locator('#seg-room-mode [data-v="vacuum"]').click()
        await self.page.locator('#two-stage-btn').click()
        self.assertEqual(await self.page.locator('#clean-review-details').inner_text(),
                         'Rooms\nWhole floor\nMode\nVacuum\nCarpet\nAvoid — stay off carpet\nPasses\n1 whole-floor pass')
        await self.page.locator('#cancel-clean-review').click()
        await self.page.locator('#seg-room-mode [data-v="two-stage"]').click()
        await self.page.locator('#two-stage-btn').click()
        self.assertEqual(await self.page.locator('#clean-review-details').inner_text(),
                         'Rooms\nWhole floor\nMode\nVacuum, then mop\nCarpet\nRise during vacuuming; Avoid during mopping\nPasses\n1 vacuum pass, then 1 mop pass')

    async def test_room_colors_follow_segment_metadata_not_list_order(self):
        await self.load_controls()
        await self.page.evaluate("renderMapRegions({rooms:[{segment_id:17,color:'#ffaf97',bbox:[0,0,.5,.5]},{segment_id:16,color:'#16d5cb',bbox:[.5,.5,1,1]}]}, {16:'Study',17:'Hall'})")
        self.assertEqual(await self.page.evaluate("""() => [16,17].map(id => ({id,
          chip:document.querySelector(`#rooms [data-id="${id}"]`).style.getPropertyValue('--rc'),
          map:document.querySelector(`#map-overlay [data-id="${id}"]`).style.getPropertyValue('--rc')}))"""),
                         [{"id":16,"chip":"#16d5cb","map":"#16d5cb"},{"id":17,"chip":"#ffaf97","map":"#ffaf97"}])

    async def test_completion_report_uses_robot_record_not_docked_state(self):
        await self.load_controls()
        record = {"last_record":{"outcome":"incomplete","duration_s":120,"area_m2":8,"ended_at":None,"error_code":7},"record_stale":True,"record_available":True,"recent_requests":[]}
        await self.page.route('**/cleaning/recent',lambda route: route.fulfill(content_type='application/json',body=json.dumps(record)))
        await self.page.evaluate('refreshRecentCleaning()')
        self.assertEqual(await self.page.locator('#recent-clean-record').inner_text(), 'Robot reports incomplete · 8 m² · 2 min · Robot error 7 · Last known record — update delayed')
        self.assertEqual(await self.page.locator('#recent-clean-dock').inner_text(), 'Robot now: Drying mop')
        record['last_record'] = None
        await self.page.evaluate('refreshRecentCleaning()')
        self.assertEqual(await self.page.locator('#recent-clean-record').inner_text(), 'No completion record available.')

    async def test_mobile_review_and_controls_fit_viewport(self):
        await self.page.set_viewport_size({"width":390,"height":844})
        await self.load_controls()
        await self.page.locator('#two-stage-btn').click()
        self.assertEqual(await self.page.evaluate("""() => {const d=document.getElementById('clean-review').getBoundingClientRect();return {overflow:document.documentElement.scrollWidth>innerWidth,inside:d.left>=0&&d.right<=innerWidth&&d.top>=0&&d.bottom<=innerHeight}}"""), {"overflow":False,"inside":True})

    async def test_disabled_assistant_preserves_manual_controls(self):
        await self.load_controls()
        await self.page.route('**/assistant',lambda route:route.fulfill(content_type='application/json',body='{"enabled":false,"model":"sonnet"}'))
        await self.page.evaluate('loadAssistantStatus()')
        self.assertEqual(await self.page.locator('#assistant-response').inner_text(), 'Assistant is turned off. Enable it in server settings, or use the room controls.')
        self.assertTrue(await self.page.locator('#assistant-ask').is_disabled())
        self.assertFalse(await self.page.locator('#two-stage-btn').is_disabled())

    async def test_assistant_whole_floor_plan_and_unsupported_repeat(self):
        await self.load_controls()
        await self.page.evaluate("assistantPlan={segment_ids:null,mode:'vacuum',carpet:'avoid',repeat:1}; reviewAssistantPlan()")
        self.assertEqual(await self.page.locator('#clean-review-details').inner_text(),
                         'Rooms\nWhole floor\nMode\nVacuum\nCarpet\nAvoid — stay off carpet\nPasses\n1 whole-floor pass')
        await self.page.locator('#cancel-clean-review').click()
        await self.page.evaluate("assistantPlan={segment_ids:null,mode:'vacuum',carpet:'avoid',repeat:2}; reviewAssistantPlan()")
        self.assertEqual(await self.page.evaluate("({open:document.getElementById('clean-review').open,message:document.getElementById('assistant-response').textContent})"),
                         {"open":False,"message":"This plan needs clarification. Choose rooms and settings below."})

    async def test_pass_indicator_matches_effective_whole_floor_and_two_stage(self):
        await self.load_controls()
        await self.page.locator('#rooms .chip[data-id="16"]').click()
        await self.page.locator('#seg-room-mode [data-v="vacuum"]').click()
        await self.page.locator('#seg-passes [data-v="2"]').click()
        await self.page.locator('#rooms .chip[data-id="16"]').click()
        self.assertEqual(await self.page.locator('#seg-passes .active').get_attribute('data-v'),'1')
        await self.page.locator('#rooms .chip[data-id="16"]').click()
        await self.page.locator('#seg-passes [data-v="3"]').click()
        await self.page.locator('#seg-room-mode [data-v="two-stage"]').click()
        self.assertEqual(await self.page.locator('#seg-passes .active').get_attribute('data-v'),'1')

    async def test_carpet_last_verified_age_and_post_clean_refresh(self):
        await self.load_controls()
        await self.page.evaluate('carpetVerifiedAt=Date.now()-60000; renderFreshness()')
        self.assertEqual(await self.page.locator('#carpet-verified-age').inner_text(),'Read 60s ago. The robot or its app may have changed it since.')
        await self.status_response(state_name='cleaning',docked=False,cleaning=True,in_cleaning=True)
        await self.page.route('**/carpet',lambda route:route.fulfill(content_type='application/json',body='{"mode":"avoid"}'))
        await self.status_response()
        await self.page.wait_for_function("currentCarpetMode === 'avoid'")
        self.assertEqual(await self.page.locator('#carpet-current').inner_text(),'Last verified: Avoid — stay off carpet')

    async def test_assistant_review_stays_hidden_for_pending_and_answer(self):
        await self.load_controls()
        await self.page.get_by_role('button',name='Clean',exact=True).first.click()
        self.assertFalse(await self.page.locator('#assistant-review').is_visible())
        async def answer(route):
            await asyncio.sleep(.5)
            await route.fulfill(content_type='application/json',body='{"kind":"answer","message":"Robot is charging","plan":null}')
        await self.page.route('**/assistant',answer)
        await self.page.locator('#assistant-message').fill('What is the robot doing?')
        await self.page.locator('#assistant-ask').click()
        self.assertFalse(await self.page.locator('#assistant-review').is_visible())
        await self.page.wait_for_function("document.getElementById('assistant-response').textContent === 'Robot is charging'")
        self.assertFalse(await self.page.locator('#assistant-review').is_visible())

    async def test_overlapping_map_labels_prioritize_selected_room(self):
        await self.load_controls()
        await self.page.evaluate("setRobotMapData(null);renderMapRegions({rooms:[{segment_id:16,name:'Study',bbox:[.2,.2,.6,.6]},{segment_id:17,name:'Hall',bbox:[.2,.2,.6,.6]}]}, {16:'Study',17:'Hall'})")
        await self.page.wait_for_timeout(100)
        self.assertEqual(await self.page.locator('#map-overlay .map-label').evaluate_all("els=>els.filter(el=>getComputedStyle(el).visibility!=='hidden').map(el=>el.textContent)"), ['Study'])
        await self.page.locator('#rooms .chip[data-id="17"]').click()
        await self.page.wait_for_timeout(100)
        self.assertEqual(await self.page.locator('#map-overlay .map-label').evaluate_all("els=>els.filter(el=>getComputedStyle(el).visibility!=='hidden').map(el=>el.textContent)"), ['Hall'])
        self.assertEqual(await self.page.locator('#map-overlay .map-region').evaluate_all("els=>els.map(el=>el.getAttribute('aria-label'))"), ['Study','Hall'])

    async def test_pending_confirmation_shows_progress_and_blocks_duplicate(self):
        await self.load_controls()
        for mode, path, label in [('vacuum','/start','Checking settings…'), ('two-stage','/clean/two-stage','Starting sequence…')]:
            release, received = asyncio.Event(), asyncio.Event()
            sent = []
            async def delayed(route, _request, sent=sent, received=received, release=release):
                sent.append(route.request.post_data_json)
                received.set()
                await release.wait()
                await route.fulfill(status=409,content_type='application/json',body='{"detail":"Settings could not be verified"}')
            await self.page.route('**' + path, delayed)
            await self.page.locator(f'#seg-room-mode [data-v="{mode}"]').click()
            await self.page.locator('#two-stage-btn').click()
            await self.page.locator('#confirm-clean').click()
            await received.wait()
            try:
                self.assertEqual(await self.page.locator('#confirm-clean').evaluate('el=>({label:el.textContent,disabled:el.disabled})'), {"label":label,"disabled":True})
                await self.page.evaluate('confirmClean()')
                self.assertEqual(len(sent),1)
            finally:
                release.set()
            await self.page.wait_for_function('!cleanSubmitting')
            self.assertEqual(await self.page.locator('#confirm-clean').evaluate('el=>({label:el.textContent,disabled:el.disabled})'), {"label":"Confirm and start","disabled":False})
            await self.page.locator('#cancel-clean-review').click()

    async def test_plain_error_responses_keep_review_and_show_useful_messages(self):
        await self.load_controls()
        await self.page.locator('#seg-room-mode [data-v="vacuum"]').click()
        await self.page.route('**/start',lambda route:route.fulfill(status=503,content_type='text/plain',body='upstream unavailable'))
        await self.page.locator('#two-stage-btn').click()
        await self.page.locator('#confirm-clean').click()
        await self.page.wait_for_function("document.getElementById('clean-review-error').textContent.length > 0")
        self.assertEqual(await self.page.evaluate("""() => ({open:document.getElementById('clean-review').open,
          error:document.getElementById('clean-review-error').textContent,
          feedback:document.getElementById('clean-request-feedback').textContent,
          pending:pendingClean, active:activeClean})"""), {"open":True,"error":"Could not confirm the start. Check robot status before retrying.","feedback":"","pending":{"segment_ids":None,"mode":"vacuum","carpet":"avoid","repeat":1},"active":None})
        await self.page.locator('#cancel-clean-review').click()
        await self.page.get_by_role('button',name='Clean',exact=True).first.click()
        await self.page.route('**/assistant',lambda route:route.fulfill(status=503,content_type='text/plain',body='upstream unavailable'))
        await self.page.locator('#assistant-message').fill('What happened?')
        await self.page.locator('#assistant-ask').click()
        await self.page.wait_for_function("!document.getElementById('assistant-ask').disabled")
        self.assertEqual(await self.page.locator('#assistant-response').inner_text(),'Assistant temporarily unavailable. You can use the room controls.')


if __name__ == '__main__':
    unittest.main(verbosity=2)
