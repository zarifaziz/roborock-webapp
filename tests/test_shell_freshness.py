"""Every shell deployment must be visible to a running PWA update check."""

import hashlib

import pytest
from fastapi.testclient import TestClient

from roborock_auto.adapters.web import server


@pytest.fixture(autouse=True)
def fake_gateway(monkeypatch):
    monkeypatch.setenv("GATEWAY", "fake")
    monkeypatch.setenv("DATABASE_PATH", ":memory:")


def test_index_revalidates_on_navigation():
    with TestClient(server.app) as client:
        response = client.get('/')
    assert (response.status_code, response.headers.get('cache-control')) == (200, 'no-cache')


def test_html_change_updates_worker_bytes_and_shell_cache(monkeypatch, tmp_path):
    worker = 'const CACHE = "roborock-shell-v4-__SHELL_VERSION__";\n'
    (tmp_path / 'sw.js').write_text(worker)
    monkeypatch.setattr(server, 'STATIC_DIR', tmp_path)
    observed = []
    with TestClient(server.app) as client:
        for html in ['<html>old</html>', '<html>new</html>']:
            (tmp_path / 'index.html').write_text(html)
            response = client.get('/sw.js')
            observed.append((response.status_code, response.headers.get('cache-control'), response.headers.get('service-worker-allowed'), response.text))
    expected = []
    for html in ['<html>old</html>', '<html>new</html>']:
        version = hashlib.sha256(html.encode()).hexdigest()[:16]
        expected.append((200, 'no-cache', '/', worker.replace('__SHELL_VERSION__', version)))
    assert observed == expected
