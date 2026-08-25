import httpx
import pytest
from fastapi import HTTPException, Request

from backend.auth_gate import token_hash

from test_fleet import fleet, add


async def test_discovery_metadata_and_explicit_mapping(fleet):
    source = await add(fleet)
    target = await add(fleet, 'Beta', 'https://beta.example', 'beta-key')
    for host, identifier in [('alpha.example', 'src'), ('beta.example', 'dst')]:
        fleet.replies[(host, 'GET', '/api/v1/credentials')] = httpx.Response(200, json={'data': [{'id': identifier, 'name': 'Same name', 'type': 'httpBasicAuth', 'data': {'password': 'never-export'}}]})
    response = await fleet.client.get('/api/n8n_credentials/', params={'instance_id': target})
    assert response.status_code == 200, response.text
    assert response.json()['data'] == [{'id': 'dst', 'name': 'Same name', 'type': 'httpBasicAuth'}]
    assert 'never-export' not in response.text
    params = {'source_instance_id': source, 'target_instance_id': target}
    assert (await fleet.client.get('/api/n8n_credentials/mappings', params=params)).json()['data'] == []
    body = {**params, 'source_credential_id': 'src', 'target_credential_id': 'dst', 'credential_type': 'httpBasicAuth', 'target_name': 'Same name'}
    response = await fleet.client.put('/api/n8n_credentials/mappings', headers=fleet.headers, json=body)
    assert response.status_code == 200, response.text
    key = response.json()['key']
    assert (await fleet.client.get('/api/n8n_credentials/mappings', params=params)).json()['data'][0]['target_credential_id'] == 'dst'
    assert (await fleet.client.delete(f'/api/n8n_credentials/mappings/{key}', headers=fleet.headers)).status_code == 200


@pytest.mark.parametrize('status,supported,expected', [(403, False, 200), (404, False, 200), (401, None, 502), (500, None, 502)])
async def test_discovery_distinguishes_unavailable_from_auth_failure(fleet, status, supported, expected):
    target = await add(fleet)
    fleet.replies[('alpha.example', 'GET', '/api/v1/credentials')] = httpx.Response(status, json={'message': 'never-export'})
    response = await fleet.client.get('/api/n8n_credentials/', params={'instance_id': target})
    assert response.status_code == expected, response.text
    assert 'never-export' not in response.text
    if expected == 200:
        assert response.json()['supported'] is supported
        assert response.json()['diagnostics']


async def test_discovery_repeated_cursor_is_failure(fleet):
    target = await add(fleet)
    fleet.replies[('alpha.example', 'GET', '/api/v1/credentials')] = httpx.Response(200, json={'data': [], 'nextCursor': 'repeat'})
    response = await fleet.client.get('/api/n8n_credentials/', params={'instance_id': target})
    assert response.status_code == 502
    assert len(fleet.calls) == 2


async def test_credential_routes_admin_only(fleet):
    target = await add(fleet)
    await fleet.app.state.db.execute("UPDATE users SET role='operator' WHERE username='owner'")
    response = await fleet.client.get('/api/n8n_credentials/', params={'instance_id': target})
    assert response.status_code == 403
    assert not fleet.calls


def service_request(fleet):
    request = Request({'type': 'http', 'app': fleet.app, 'headers': []})
    request.state.user = {'username': 'owner', 'role': 'admin', 'session_id': token_hash(fleet.client.cookies['inference_session'])}
    return request


async def test_provision_validates_compound_secret_and_returns_only_metadata(fleet):
    import hashlib
    import json
    from backend.modules.n8n_credentials.service import provision
    target = await add(fleet)
    fleet.app.state.vault.set('BASIC', {'password': 'private-value'})
    fleet.replies[('alpha.example', 'GET', '/api/v1/credentials/schema/httpBasicAuth')] = httpx.Response(200, json={'type': 'object', 'required': ['password'], 'properties': {'password': {'type': 'string'}}, 'additionalProperties': False})
    fleet.replies[('alpha.example', 'POST', '/api/v1/credentials')] = httpx.Response(200, json={'id': 'created', 'name': 'safe', 'type': 'httpBasicAuth', 'data': {'password': 'private-value'}})
    expected_hash = hashlib.sha256(json.dumps({'password': 'private-value'}, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':')).encode()).hexdigest()
    result = await provision(service_request(fleet), target, 'httpBasicAuth', 'safe', '${BASIC}', expected_hash=expected_hash)
    assert result == {'id': 'created', 'name': 'safe', 'type': 'httpBasicAuth'}
    assert json.loads(fleet.calls[-1].content)['data'] == {'password': 'private-value'}


@pytest.mark.parametrize('value', [{'password': 123}, {}, 'plaintext'])
async def test_invalid_secret_prevents_remote_write(fleet, value):
    from backend.modules.n8n_credentials.service import provision
    target = await add(fleet)
    fleet.app.state.vault.set('BASIC', value)
    fleet.replies[('alpha.example', 'GET', '/api/v1/credentials/schema/httpBasicAuth')] = httpx.Response(200, json={'type': 'object', 'required': ['password'], 'properties': {'password': {'type': 'string'}}})
    with pytest.raises(HTTPException) as error:
        await provision(service_request(fleet), target, 'httpBasicAuth', 'safe', '$BASIC')
    assert error.value.status_code == 422
    assert all(call.method == 'GET' for call in fleet.calls)


async def test_schema_external_references_are_rejected(fleet):
    target = await add(fleet)
    fleet.replies[('alpha.example', 'GET', '/api/v1/credentials/schema/httpBasicAuth')] = httpx.Response(200, json={'$ref': 'http://169.254.169.254/schema'})
    response = await fleet.client.get('/api/n8n_credentials/schema/httpBasicAuth', params={'instance_id': target})
    assert response.status_code == 502
    assert response.json()['code'] == 'credential_schema_unsupported'
    assert len(fleet.calls) == 1


async def test_malformed_provision_response_is_uncertain_and_never_retried(fleet):
    from backend.modules.n8n_credentials.service import provision
    target = await add(fleet)
    fleet.app.state.vault.set('BASIC', {'password': 'private-value'})
    fleet.replies[('alpha.example', 'GET', '/api/v1/credentials/schema/httpBasicAuth')] = httpx.Response(200, json={'type': 'object'})
    fleet.replies[('alpha.example', 'POST', '/api/v1/credentials')] = httpx.Response(200, json={'data': {'password': 'private-value'}})
    with pytest.raises(HTTPException) as error:
        await provision(service_request(fleet), target, 'httpBasicAuth', 'safe', '$BASIC')
    assert error.value.detail['code'] == 'upstream_outcome_unknown'
    assert 'private-value' not in str(error.value.detail)
    assert len([call for call in fleet.calls if call.method == 'POST']) == 1


async def test_mapping_rechecks_role_after_discovery(fleet):
    source = await add(fleet)
    target = await add(fleet, 'Beta', 'https://beta.example', 'beta-key')
    fleet.replies[('alpha.example', 'GET', '/api/v1/credentials')] = httpx.Response(404)
    async def demote(request):
        await fleet.app.state.db.execute("UPDATE users SET role='operator' WHERE username='owner'")
        return httpx.Response(404)
    fleet.replies[('beta.example', 'GET', '/api/v1/credentials')] = demote
    response = await fleet.client.put('/api/n8n_credentials/mappings', headers=fleet.headers, json={'source_instance_id': source, 'target_instance_id': target, 'source_credential_id': 'src', 'target_credential_id': 'dst', 'credential_type': 'httpBasicAuth', 'target_name': 'safe'})
    assert response.status_code == 403
    assert await fleet.app.state.db.fetchall('SELECT * FROM credential_mappings') == []


async def test_provision_rejects_changed_secret_before_post(fleet):
    from backend.modules.n8n_credentials.service import provision
    import hashlib
    import json
    target = await add(fleet)
    expected_hash = hashlib.sha256(json.dumps({'password': 'original'}, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':')).encode()).hexdigest()
    fleet.app.state.vault.set('BASIC', {'password': 'changed'})
    fleet.replies[('alpha.example', 'GET', '/api/v1/credentials/schema/httpBasicAuth')] = httpx.Response(200, json={'type': 'object'})
    with pytest.raises(HTTPException) as error:
        await provision(service_request(fleet), target, 'httpBasicAuth', 'safe', '$BASIC', expected_hash=expected_hash)
    assert error.value.status_code == 409
    assert error.value.detail['code'] == 'stale_preview'
    assert all(call.method == 'GET' for call in fleet.calls)


async def test_mapping_delete_waits_for_promotion_instance_guard(fleet):
    import asyncio
    from backend.modules.n8n_credentials.service import mapping_key
    from backend.modules.n8n_proxy.client import instance_guard
    source = await add(fleet)
    target = await add(fleet, 'Beta', 'https://beta.example', 'beta-key')
    key = mapping_key(source, target, 'src')
    await fleet.app.state.db.execute('INSERT INTO credential_mappings VALUES(?,?,?,?,?,?,?)', (key, source, target, 'src', 'dst', 'httpBasicAuth', 'safe'))
    request = service_request(fleet)
    async with instance_guard(request, min(source, target)):
        deletion = asyncio.create_task(fleet.client.delete(f'/api/n8n_credentials/mappings/{key}', headers=fleet.headers))
        try:
            await asyncio.wait_for(asyncio.shield(deletion), timeout=0.1)
        except asyncio.TimeoutError:
            pass
        assert not deletion.done(), 'Deletion bypassed the promotion instance lock'
        assert await fleet.app.state.db.fetchone('SELECT key FROM credential_mappings WHERE key=?', (key,))
    response = await deletion
    assert response.status_code == 200
    assert await fleet.app.state.db.fetchone('SELECT key FROM credential_mappings WHERE key=?', (key,)) is None
