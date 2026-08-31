import asyncio
import json
import sqlite3
import threading

import pytest

from test_fleet import fleet


async def apply(state, name='SERVICE', values=None, template='api_key'):
    return await state.client.post(f'/api/admin/secret-templates/{template}/apply',
        headers={**state.headers, 'Content-Type': 'application/json'},
        content=json.dumps({'name': name, 'values': values if values is not None else {'api_key': 'private-api-value'}}))


async def test_template_catalog_and_encrypted_compound_creation(fleet):
    catalog = await fleet.client.get('/api/admin/secret-templates')
    assert catalog.status_code == 200
    assert {item['id'] for item in catalog.json()['data']} == {'api_key', 'basic_auth', 's3'}
    result = await apply(fleet)
    assert result.status_code == 200, result.text
    assert result.json() == {'name': 'SERVICE', 'template_id': 'api_key'}
    assert fleet.app.state.vault.resolve('$SERVICE.api_key') == 'private-api-value'
    assert 'private-api-value' not in (fleet.app.state.settings.data_dir / 'secrets.json').read_text()
    events = await fleet.app.state.db.fetchall("SELECT * FROM audit_events WHERE action='admin.secret_template_applied'")
    assert len(events) == 1 and 'private-api-value' not in str(events)


async def test_s3_template_omits_blank_optional_session_token(fleet):
    result = await apply(fleet, template='s3', values={'access_key_id': 'EXAMPLE',
        'secret_access_key': 'private-s3-secret', 'session_token': ''})
    assert result.status_code == 200
    assert fleet.app.state.vault.resolve('$SERVICE') == {'access_key_id': 'EXAMPLE', 'secret_access_key': 'private-s3-secret'}


@pytest.mark.parametrize('template,values,status', [
    ('unknown', {'api_key': 'private'}, 404),
    ('api_key', {}, 422), ('api_key', {'api_key': ''}, 422),
    ('api_key', {'api_key': 'private', 'unexpected': 'private'}, 422),
    ('basic_auth', {'username': 'user'}, 422),
    ('api_key', {'api_key': 123}, 422),
    ('api_key', {'api_key': 'x' * 65536}, 413),
    ('api_key', {'api_key': '\ud800'}, 422),
])
async def test_template_validation_never_writes(fleet, template, values, status):
    result = await apply(fleet, template=template, values=values)
    assert result.status_code == status, result.text
    assert fleet.app.state.vault.list() == []
    assert 'private' not in result.text


async def test_template_roles_and_csrf(fleet):
    result = await fleet.client.post('/api/admin/secret-templates/api_key/apply',
        json={'name': 'SERVICE', 'values': {'api_key': 'private'}})
    assert result.status_code == 403
    for role in ('operator', 'viewer'):
        await fleet.app.state.db.execute("UPDATE users SET role=? WHERE username='owner'", (role,))
        assert (await apply(fleet)).status_code == 403
        assert (await fleet.client.get('/api/admin/secret-templates')).status_code == 403
    assert fleet.app.state.vault.list() == []


async def test_template_collision_preserves_secret_and_concurrent_create(fleet):
    fleet.app.state.vault.set('EXISTING', {'original': 'keep'})
    assert (await apply(fleet, name='EXISTING')).status_code == 409
    assert fleet.app.state.vault.resolve('$EXISTING') == {'original': 'keep'}
    results = await asyncio.gather(apply(fleet), apply(fleet))
    assert sorted(r.status_code for r in results) == [200, 409]


async def test_basic_and_s3_optional_fields(fleet):
    for template, values in [('basic_auth', {'username': 'user', 'password': 'private-pass'}),
                             ('s3', {'access_key_id': 'key', 'secret_access_key': 'private-s3'}),
                             ('s3', {'access_key_id': 'key', 'secret_access_key': 'private-s3', 'session_token': 'session-value'})]:
        name = f'SECRET_{len(fleet.app.state.vault.list())}'
        result = await apply(fleet, name=name, template=template, values=values)
        assert result.status_code == 200, result.text
        assert fleet.app.state.vault.resolve(f'${name}') == values


async def test_template_cancel_settles_write_and_audit(fleet, monkeypatch):
    original = fleet.app.state.vault.set
    started, release = threading.Event(), threading.Event()
    def delayed(name, value):
        started.set()
        assert release.wait(5)
        original(name, value)
    monkeypatch.setattr(fleet.app.state.vault, 'set', delayed)
    task = asyncio.create_task(apply(fleet))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fleet.app.state.vault.resolve('$SERVICE.api_key') == 'private-api-value'
    events = await fleet.app.state.db.fetchall("SELECT * FROM audit_events WHERE action='admin.secret_template_applied'")
    assert len(events) == 1


async def test_template_audit_failure_reports_unknown_without_leaking(fleet, monkeypatch):
    from backend.database import Transaction
    original = Transaction.execute
    async def broken(self, sql, params=()):
        if 'INSERT INTO audit_events' in sql:
            raise sqlite3.OperationalError('private database path')
        return await original(self, sql, params)
    monkeypatch.setattr(Transaction, 'execute', broken)
    result = await apply(fleet)
    assert result.status_code == 503
    assert result.json()['code'] == 'secret_outcome_unknown'
    assert 'private' not in result.text
    assert fleet.app.state.vault.resolve('$SERVICE.api_key') == 'private-api-value'


async def test_template_live_role_recheck_before_file_write(fleet, monkeypatch):
    from backend.modules.admin import templates
    original = templates.require_current_role
    async def revoked(request, role, tx):
        await tx.execute("UPDATE users SET role='viewer' WHERE username='owner'")
        return await original(request, role, tx)
    monkeypatch.setattr(templates, 'require_current_role', revoked)
    assert (await apply(fleet)).status_code == 403
    assert fleet.app.state.vault.list() == []
