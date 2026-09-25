"""Run: uv run --with playwright python tests/browser/shell_freshness.py."""

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

HTML = Path(__file__).parents[2] / 'src/roborock_auto/adapters/web/static/index.html'


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel='chrome')
        page = await browser.new_page()
        await page.route('**/*', lambda route: route.fulfill(body='<html></html>', content_type='text/html'))
        await page.goto('http://localhost/')
        await page.evaluate('''() => {
            window.updateChecks = 0;
            window.updateTimers = [];
            window.showUpdateToast = () => {};
            Object.defineProperty(navigator.serviceWorker, 'register', {value: async () => ({
                installing: null, waiting: null, addEventListener() {},
                update() { window.updateChecks++; return Promise.resolve(); }
            })});
            window.setInterval = (callback, delay) => window.updateTimers.push({callback, delay});
        }''')
        source = HTML.read_text()
        registration = source[source.index('function initServiceWorker()'):source.index('function showUpdateToast(')]
        await page.evaluate('() => {' + registration + '\ninitServiceWorker();}')
        await page.wait_for_timeout(50)
        await page.evaluate('window.dispatchEvent(new Event("focus"))')
        await page.evaluate('document.dispatchEvent(new Event("visibilitychange"))')
        await page.evaluate('window.updateTimers.forEach(t => t.callback())')
        observed = await page.evaluate('({checks:window.updateChecks, timers:window.updateTimers.map(t=>t.delay)})')
        assert observed == {'checks': 3, 'timers': [60000]}, observed
        await page.evaluate('''() => {
            Object.defineProperty(document, 'visibilityState', {configurable: true, value: 'hidden'});
            document.dispatchEvent(new Event('visibilitychange'));
            window.updateTimers.forEach(t => t.callback());
        }''')
        assert await page.evaluate('window.updateChecks') == 3
        await browser.close()
        print('Shell freshness lifecycle checks passed')


asyncio.run(main())
