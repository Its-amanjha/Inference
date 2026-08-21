import asyncio
import json

import pytest
from fastapi.testclient import TestClient


def test_health_and_no_import_time_storage(tmp_path, monkeypatch):
    monkeypatch.setenv('INFERENCE_DATA_DIR',str(tmp_path/'unused'))
    from backend.main import create_app
    app = create_app(data_dir=tmp_path/'actual')
    assert not (tmp_path/'actual').exists()
    with TestClient(app, base_url='https://localhost', client=('127.0.0.1',50000)) as c:
        assert c.get('/healthz').json()['status'] == 'ok'
        assert c.get('/api/unknown').status_code == 401
        assert c.get('/api/auth/status').json()['setup_required'] is True
        assert c.get('/healthz').headers['X-Content-Type-Options'] == 'nosniff'


def test_registry_rejects_conflicting_manifests(tmp_path):
    from backend.modules import discover_modules
    for name in ['one','two']:
        folder=tmp_path/name
        folder.mkdir()
        (folder/'manifest.json').write_text(json.dumps({'id':name,'version':'1.0.0','api_version':1,'prefix':'/api/shared','title':name}))
    with pytest.raises(ValueError):
        discover_modules(tmp_path)


def test_websocket_requires_session_and_matching_origin(tmp_path):
    from backend.main import create_app
    from starlette.websockets import WebSocketDisconnect
    with TestClient(create_app(data_dir=tmp_path),base_url='https://localhost',client=('127.0.0.1',50000)) as c:
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect('wss://localhost/ws',headers={'Origin':'https://localhost'}):
                pass
        c.post('/api/auth/setup',json={'username':'owner','password':'a long unique password 2026'})
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect('wss://localhost/ws',headers={'Origin':'https://evil.example'}):
                pass
        with c.websocket_connect('wss://localhost/ws',headers={'Origin':'https://localhost'}) as ws:
            assert ws.receive_json()['event'] == 'connected'


def test_rate_limiter_is_bounded_and_recovers():
    from backend.ratelimit import RateLimiter
    now=[0.0]
    limiter=RateLimiter(max_keys=2,clock=lambda:now[0])
    assert limiter.hit('a',limit=1,window=10)
    assert not limiter.hit('a',limit=1,window=10)
    assert limiter.hit('b',limit=1,window=10)
    assert not limiter.hit('c',limit=1,window=10)
    now[0]=11
    assert limiter.hit('c',limit=1,window=10)
