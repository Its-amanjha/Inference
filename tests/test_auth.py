import asyncio
from datetime import datetime, timedelta, timezone

import pyotp
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path):
    from backend.main import create_app
    with TestClient(create_app(data_dir=tmp_path), base_url='https://localhost', client=('127.0.0.1', 50000)) as c:
        yield c


def setup(c):
    r = c.post('/api/auth/setup', json={'username': 'owner', 'password': 'a long unique password 2026'})
    assert r.status_code == 200, r.text
    return {'X-AGD-CSRF': c.cookies['agd_csrf']}


def test_csrf_is_bound_to_session_and_origin(client):
    headers = setup(client)
    bad = {**headers, 'Origin': 'https://evil.example'}
    r = client.post('/api/admin/secrets', headers=bad, json={'name':'TOKEN','value':'secret'})
    assert r.status_code == 403
    assert r.json()['code'] == 'origin_rejected'
    client.cookies.set('agd_csrf', 'attacker', domain='localhost.local', path='/')
    r = client.post('/api/admin/secrets', headers={'X-AGD-CSRF':'attacker'}, json={'name':'TOKEN','value':'secret'})
    assert r.status_code == 403
    assert r.json()['code'] == 'csrf_failed'
    client.get('/api/auth/status')
    assert client.post('/api/admin/secrets', headers={'X-AGD-CSRF':client.cookies['agd_csrf']}, json={'name':'TOKEN','value':'secret'}).status_code == 200
    listing = client.get('/api/admin/secrets')
    assert listing.json()['data'] == ['TOKEN']


def test_remote_bootstrap_and_login_origin(tmp_path):
    from backend.main import create_app
    with TestClient(create_app(data_dir=tmp_path), base_url='https://dashboard.example', client=('203.0.113.5', 4000)) as c:
        assert c.post('/api/auth/setup', json={'username':'owner','password':'a long unique password 2026'}).status_code == 403
        assert c.post('/api/auth/login', headers={'Origin':'https://evil.example'}, json={'username':'owner','password':'whatever'}).status_code == 403


def test_revocation_and_last_admin_protection(client):
    headers = setup(client)
    r = client.patch('/api/admin/users/owner', headers=headers, json={'role':'viewer'})
    assert r.status_code == 409
    assert client.post('/api/admin/users', headers=headers, json={'username':'ops','password':'another long password 2026','role':'operator'}).status_code == 200
    client.post('/api/auth/logout', headers=headers)
    assert client.post('/api/auth/login', json={'username':'ops','password':'another long password 2026'}).status_code == 200
    assert client.get('/api/admin/secrets').status_code == 403
    assert client.get('/api/admin/users').status_code == 403
    headers = {'X-AGD-CSRF': client.cookies['agd_csrf']}
    old_cookie = client.cookies.get('inference_session')
    assert client.post('/api/auth/logout', headers=headers).status_code == 200
    client.cookies.set('inference_session', old_cookie, domain='localhost.local', path='/')
    assert client.get('/api/auth/status').json()['user'] is None


def test_bootstrap_token_is_read_only_from_the_inference_header(tmp_path, monkeypatch):
    from backend.main import create_app
    monkeypatch.setenv('INFERENCE_BOOTSTRAP_TOKEN', 'bootstrap-value')
    with TestClient(create_app(data_dir=tmp_path), base_url='https://localhost', client=('127.0.0.1', 50000)) as c:
        credentials = {'username': 'owner', 'password': 'a long unique password 2026'}
        for header in ('X-Bootstrap', 'X-Bootstrap-Token', 'X-Setup-Bootstrap'):
            assert c.post('/api/auth/setup', headers={header: 'bootstrap-value'}, json=credentials).status_code == 403
        assert c.post('/api/auth/setup', headers={'X-Inference-Bootstrap': 'wrong'}, json=credentials).status_code == 403
        assert c.post('/api/auth/setup', headers={'X-Inference-Bootstrap': 'bootstrap-value'}, json=credentials).status_code == 200


def test_session_is_read_only_from_the_inference_cookie(client):
    setup(client)
    raw = client.cookies.get('inference_session')
    for name in ('session', 'n8n_session', 'dashboard_session'):
        client.cookies.clear()
        client.cookies.set(name, raw, domain='localhost.local', path='/')
        assert client.get('/api/auth/status').json()['user'] is None
    client.cookies.clear()
    client.cookies.set('inference_session', raw, domain='localhost.local', path='/')
    assert client.get('/api/auth/status').json()['user']['username'] == 'owner'


def test_totp_enrollment_login_replay_and_recovery(client):
    headers = setup(client)
    r = client.post('/api/auth/totp/enroll', headers=headers, json={'password':'a long unique password 2026'})
    assert r.status_code == 200, r.text
    secret = r.json()['secret']
    code = pyotp.TOTP(secret).now()
    r = client.post('/api/auth/totp/confirm', headers=headers, json={'code':code})
    assert r.status_code == 200, r.text
    recovery = r.json()['recovery_codes']
    assert len(recovery) >= 8
    client.post('/api/auth/logout', headers=headers)
    credentials = {'username':'owner','password':'a long unique password 2026'}
    assert client.post('/api/auth/login', json=credentials).status_code == 401
    assert client.post('/api/auth/login', json={**credentials,'code':code}).status_code == 401
    assert client.post('/api/auth/login', json={**credentials,'code':recovery[0]}).status_code == 200
    client.post('/api/auth/logout', headers={'X-AGD-CSRF':client.cookies['agd_csrf']})
    assert client.post('/api/auth/login', json={**credentials,'code':recovery[0]}).status_code == 401


def test_audit_never_contains_secret_or_password(client):
    headers = setup(client)
    client.post('/api/admin/secrets', headers=headers, json={'name':'TOKEN','value':'do-not-record-this'})
    events = client.get('/api/admin/audit').json()['data']
    assert events
    assert 'do-not-record-this' not in str(events)
    assert 'a long unique password 2026' not in str(events)


def test_login_rate_limit_and_validation_redaction(client):
    setup(client)
    for _ in range(8):
        response = client.post('/api/auth/login', json={'username':'unknown','password':'wrong'})
    assert response.status_code == 429
    r = client.post('/api/auth/login',json={'username':None,'password':{'secret':'should-not-echo'}})
    assert 'should-not-echo' not in r.text
