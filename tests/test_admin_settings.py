from datetime import datetime
import asyncio
import json

import pytest

from test_fleet import fleet


async def update(fleet, **values):
    current = (await fleet.client.get('/api/admin/settings')).json()
    return await fleet.client.put('/api/admin/settings', headers=fleet.headers,
        json={'expected_revision': current.get('revision', '0' * 64), 'session_hours': 2,
              'default_preferences': {'theme': 'light', 'page_size': 50, 'error_notifications': False}, **values})


async def test_settings_control_new_sessions_and_default_preferences(fleet):
    response = await update(fleet)
    assert response.status_code == 200, response.text
    assert fleet.app.state.settings.session_hours == 2
    assert (await fleet.client.get('/api/admin/preferences')).json()['theme'] == 'light'
    login = await fleet.client.post('/api/auth/login', json={'username': 'owner', 'password': 'test password of sufficient length'})
    assert login.status_code == 200
    rows = await fleet.app.state.db.fetchall('SELECT created_at,expires_at FROM auth_sessions')
    assert any((datetime.fromisoformat(row['expires_at']) - datetime.fromisoformat(row['created_at'])).total_seconds() == 7200 for row in rows)


async def test_settings_stale_write_and_existing_preferences(fleet):
    initial = (await fleet.client.get('/api/admin/settings')).json()
    await fleet.client.put('/api/admin/preferences', headers=fleet.headers, json={'theme': 'dark', 'page_size': 25})
    assert (await update(fleet)).status_code == 200
    stale = await update(fleet, expected_revision=initial['revision'])
    assert stale.status_code == 409
    assert (await fleet.client.get('/api/admin/preferences')).json()['theme'] == 'dark'


async def test_n8n_theme_persists_for_personal_and_default_preferences(fleet):
    from backend.modules.admin.settings import initialize
    value = {'theme': 'n8n', 'page_size': 25, 'error_notifications': True}
    response = await fleet.client.put('/api/admin/preferences', headers=fleet.headers, json=value)
    assert response.status_code == 200
    assert (await fleet.client.get('/api/admin/preferences')).json() == value
    assert (await update(fleet, default_preferences=value)).status_code == 200
    await initialize(fleet.app)
    assert fleet.app.state.system_overlay.default_preferences.theme == 'n8n'
    assert (await fleet.client.put('/api/admin/preferences', headers=fleet.headers,
        json={**value, 'theme': '../unknown'})).status_code == 422


async def test_settings_reject_security_fields_and_operator(fleet):
    assert (await update(fleet, secure_cookies=False)).status_code == 422
    assert (await update(fleet, session_hours=0)).status_code == 422
    await fleet.app.state.db.execute("UPDATE users SET role='operator' WHERE username='owner'")
    assert (await update(fleet)).status_code == 403


async def test_settings_survive_new_application_lifespan(fleet):
    from backend.main import create_app
    assert (await update(fleet)).status_code == 200
    # Use a backup snapshot so only one application owns each database.
    destination = fleet.app.state.settings.data_dir / 'restart'
    destination.mkdir()
    await fleet.app.state.db.backup(destination / 'dashboard.db')
    for name in ('.secret_key', 'secrets.json'):
        (destination / name).write_bytes((fleet.app.state.settings.data_dir / name).read_bytes())
    app = create_app(data_dir=destination)
    async with app.router.lifespan_context(app):
        assert app.state.settings.session_hours == 2
        assert app.state.system_overlay.default_preferences.theme == 'light'


@pytest.mark.parametrize('variable', ['INFERENCE_SESSION_HOURS', 'VECTIS_SESSION_HOURS'])
async def test_environment_session_policy_overrides_saved_setting(fleet, monkeypatch, variable):
    from backend.modules.admin.settings import initialize
    from dataclasses import replace
    assert (await update(fleet)).status_code == 200
    monkeypatch.setenv(variable, '5')
    fleet.app.state.settings = replace(fleet.app.state.settings, session_hours=5)
    await initialize(fleet.app)
    result = (await fleet.client.get('/api/admin/settings')).json()
    assert result['session_hours'] == 5 and result['saved']['session_hours'] == 2
    assert result['environment_locked_fields'] == ['session_hours']
    assert (await update(fleet, session_hours=3)).status_code == 409
    assert (await update(fleet, session_hours=2)).status_code == 200
    assert fleet.app.state.settings.session_hours == 5


async def test_revoke_role_while_settings_write_waits(fleet):
    current = (await fleet.client.get('/api/admin/settings')).json()
    lock = fleet.app.state.system_settings_lock
    await lock.acquire()
    task = asyncio.create_task(fleet.client.put('/api/admin/settings', headers=fleet.headers,
        json={'expected_revision': current['revision'], 'session_hours': 2}))
    try:
        await asyncio.sleep(0.01)
        await fleet.app.state.db.execute("UPDATE users SET role='viewer' WHERE username='owner'")
    finally:
        lock.release()
    assert (await task).status_code == 403
    assert fleet.app.state.settings.session_hours == 12


async def test_cancelled_settings_write_settles_database_and_runtime(fleet, monkeypatch):
    from backend.database import Transaction
    started, release = asyncio.Event(), asyncio.Event()
    original = Transaction.execute
    async def blocked(self, sql, params=()):
        if sql.startswith('INSERT INTO documents') and params[0] == 'admin:system_settings':
            started.set()
            await release.wait()
        return await original(self, sql, params)
    monkeypatch.setattr(Transaction, 'execute', blocked)
    task = asyncio.create_task(update(fleet))
    try:
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        assert fleet.app.state.system_settings_lock.locked()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fleet.app.state.settings.session_hours == 2
    row = await fleet.app.state.db.fetchone("SELECT value_json FROM documents WHERE key='admin:system_settings'")
    assert json.loads(row['value_json'])['session_hours'] == 2
    assert not fleet.app.state.system_settings_lock.locked()


async def test_concurrent_settings_updates_reject_stale_revision(fleet):
    revision = (await fleet.client.get('/api/admin/settings')).json()['revision']
    responses = await asyncio.gather(*(fleet.client.put('/api/admin/settings', headers=fleet.headers,
        json={'expected_revision': revision, 'session_hours': hours}) for hours in (2, 3)))
    assert sorted(response.status_code for response in responses) == [200, 409]


async def test_settings_require_csrf(fleet):
    revision = (await fleet.client.get('/api/admin/settings')).json()['revision']
    response = await fleet.client.put('/api/admin/settings', json={'expected_revision': revision, 'session_hours': 2})
    assert response.status_code == 403


async def test_failed_settings_audit_does_not_publish_policy(fleet, monkeypatch):
    from backend.database import Transaction
    import sqlite3
    original = Transaction.execute
    async def broken(self, sql, params=()):
        if sql.startswith('INSERT INTO audit_events') and params[1] == 'admin.settings_updated':
            raise sqlite3.OperationalError('fixture audit failure')
        return await original(self, sql, params)
    monkeypatch.setattr(Transaction, 'execute', broken)
    with pytest.raises(sqlite3.OperationalError):
        await update(fleet)
    assert fleet.app.state.settings.session_hours == 12
    assert await fleet.app.state.db.fetchone("SELECT value_json FROM documents WHERE key='admin:system_settings'") is None
