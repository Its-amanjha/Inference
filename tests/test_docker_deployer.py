import asyncio
from copy import deepcopy
import json

import pytest
from fastapi import HTTPException

from test_fleet import fleet


@pytest.fixture
async def managed(fleet, monkeypatch):
    from backend.modules.docker_mgr import deployer
    fleet.effects = []
    fleet.objects = {}
    fleet.failure = None
    class Gateway:
        def __init__(self, request):
            self.request = request
        async def status(self):
            return {'available': True}
        async def ensure_image(self, image):
            fleet.effects.append(('image', image))
        async def create_network(self, config):
            fleet.effects.append(('network', config['Name']))
            fleet.objects[config['Name']] = config
            return config['Name']
        async def create_volume(self, config):
            fleet.effects.append(('volume', config['Name']))
            fleet.objects[config['Name']] = config
            return config['Name']
        async def create_container(self, name, config):
            fleet.effects.append(('create', name))
            if fleet.failure == 'create':
                raise HTTPException(504, {'code': 'docker_outcome_unknown', 'detail': 'Unknown Docker outcome.'})
            fleet.objects[name] = deepcopy(config)
            return name
        async def inspect_container(self, identifier, bundle_id, role):
            if identifier not in fleet.objects:
                raise HTTPException(404, {'code': 'docker_not_found', 'detail': 'Missing.'})
            return {'id': identifier, 'name': identifier, 'bundle_id': bundle_id, 'role': role,
                    'running': True, 'state': 'running', 'health': 'healthy' if fleet.failure != 'health' else 'starting'}
        async def inspect_network(self, identifier, bundle_id):
            if identifier not in fleet.objects:
                raise HTTPException(404, {'code': 'docker_error', 'detail': 'Missing.'})
            return {'id': identifier, 'name': identifier, 'bundle_id': bundle_id}
        async def inspect_volume(self, identifier, bundle_id):
            if identifier not in fleet.objects:
                raise HTTPException(404, {'code': 'docker_error', 'detail': 'Missing.'})
            return {'id': identifier, 'name': identifier, 'bundle_id': bundle_id}
        async def start_container(self, identifier, bundle_id, role):
            fleet.effects.append(('start', role))
        async def stop_container(self, identifier, bundle_id, role):
            fleet.effects.append(('stop', role))
        async def delete_container(self, identifier, bundle_id, role):
            fleet.effects.append(('delete', role))
            fleet.objects.pop(identifier, None)
        async def delete_network(self, identifier, bundle_id):
            fleet.effects.append(('delete_network', identifier))
            fleet.objects.pop(identifier, None)
        async def delete_volume(self, identifier, bundle_id):
            fleet.effects.append(('delete_volume', identifier))
            fleet.objects.pop(identifier, None)
    monkeypatch.setattr(deployer, 'DockerGateway', Gateway)
    fleet.body = {'spec': {'name': 'test', 'version': '2.19.0', 'exporter_base_url': 'https://inference.example/api/otel'},
                  'instance_url': 'https://new-n8n.example', 'idempotency_key': 'once', 'readiness_timeout': 30}
    return fleet


async def provision(state):
    return await state.client.post('/api/docker_mgr/bundles', headers=state.headers, json=state.body)


async def test_deploy_journal_idempotency_and_token_injection(managed):
    result = await provision(managed)
    assert result.status_code == 200, result.text
    data = result.json()
    assert data['status'] == 'ready'
    assert [value for action, value in managed.effects if action == 'start'] == ['n8n', 'runners']
    count = len(managed.effects)
    assert (await provision(managed)).json()['id'] == data['id']
    assert len(managed.effects) == count
    token = await managed.app.state.db.fetchone('SELECT * FROM telemetry_tokens WHERE instance_id=?', (data['instance_id'],))
    assert token and len(token['token_hash']) == 64
    containers = [obj for obj in managed.objects.values() if 'Env' in obj]
    assert len(containers) == 2
    assert any(any('OTLP' in value for value in obj['Env']) for obj in containers)
    raw = await managed.app.state.db.fetchone('SELECT value_json FROM documents WHERE key=?', ('docker:bundle:' + data['id'],))
    assert 'N8N_RUNNERS_AUTH_TOKEN' not in raw['value_json']
    assert 'Env' not in result.text


async def test_uncertain_create_is_not_retried_or_rolled_back(managed):
    managed.failure = 'create'
    result = await provision(managed)
    assert result.status_code == 504
    bundles = (await managed.client.get('/api/docker_mgr/bundles')).json()['data']
    assert bundles[0]['status'] == 'uncertain'
    count = len(managed.effects)
    repeated = await provision(managed)
    assert repeated.status_code == 200 and repeated.json()['status'] == 'uncertain'
    assert len(managed.effects) == count
    assert not any(action.startswith('delete') for action, _ in managed.effects)


async def test_teardown_preserves_volume_unless_explicitly_requested(managed):
    created = (await provision(managed)).json()
    result = await managed.client.delete('/api/docker_mgr/bundles/' + created['id'], headers=managed.headers)
    assert result.status_code == 200, result.text
    assert [value for action, value in managed.effects if action == 'delete'] == ['runners', 'n8n']
    assert not any(action == 'delete_volume' for action, _ in managed.effects)
    token = await managed.app.state.db.fetchone('SELECT revoked_at FROM telemetry_tokens WHERE instance_id=?', (created['instance_id'],))
    assert token['revoked_at'] is not None


async def test_role_csrf_and_unknown_bundle_block_before_effect(managed):
    result = await managed.client.post('/api/docker_mgr/bundles', json=managed.body)
    assert result.status_code == 403
    result = await managed.client.delete('/api/docker_mgr/bundles/unowned', headers=managed.headers)
    assert result.status_code == 404
    await managed.app.state.db.execute("UPDATE users SET role='operator' WHERE username='owner'")
    assert (await provision(managed)).status_code == 403
    assert managed.effects == []


async def test_idempotency_payload_conflict_never_creates_second_bundle(managed):
    await provision(managed)
    count = len(managed.effects)
    managed.body['spec']['name'] = 'changed'
    assert (await provision(managed)).status_code == 409
    assert len(managed.effects) == count


async def test_readiness_deadline_bounds_slow_inspection():
    from backend.modules.docker_mgr.deployer import wait_ready
    class Slow:
        async def inspect_container(self, *args):
            await asyncio.sleep(1)
            return {'running': True, 'health': 'healthy'}
    with pytest.raises(HTTPException) as exc:
        await wait_ready(Slow(), 'id', 'bundle', 'n8n', 0.01)
    assert exc.value.detail['code'] == 'docker_readiness_timeout'


async def test_reconcile_missing_storage_never_reports_ready(managed):
    result = (await provision(managed)).json()
    managed.objects.pop(result['resources']['volume'])
    count = len(managed.effects)
    response = await managed.client.post('/api/docker_mgr/bundles/' + result['id'] + '/reconcile', headers=managed.headers)
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'partial'
    assert len(managed.effects) == count
    response = await managed.client.post('/api/docker_mgr/bundles/' + result['id'] + '/start', headers=managed.headers)
    assert response.status_code == 409
    assert len(managed.effects) == count


async def test_restart_retains_owner_and_marks_interrupted_bundle_uncertain(managed):
    from backend.modules.docker_mgr import deployer
    from test_assistant_tools import actor
    result = (await provision(managed)).json()
    request = await actor(managed)
    state = await deployer.load(request, result['id'])
    state['status'] = 'starting'
    await deployer.save(request, state)
    original_owner = managed.app.state.docker_owner_id
    await deployer.initialize(managed.app)
    assert managed.app.state.docker_owner_id == original_owner
    assert (await deployer.load(request, result['id']))['status'] == 'uncertain'
    count = len(managed.effects)
    denied = await managed.client.post('/api/docker_mgr/bundles/' + result['id'] + '/start', headers=managed.headers)
    assert denied.status_code == 409
    assert len(managed.effects) == count


async def test_explicit_purge_removes_only_owned_volume(managed):
    result = (await provision(managed)).json()
    response = await managed.client.delete('/api/docker_mgr/bundles/' + result['id'] + '?purge_data=true', headers=managed.headers)
    assert response.status_code == 200, response.text
    assert response.json()['volume_retained'] is False
    assert [value for action, value in managed.effects if action == 'delete_volume'] == [result['resources']['volume']]


async def test_cancelled_remote_create_is_durable_uncertain(managed, monkeypatch):
    from backend.modules.docker_mgr import deployer
    from test_assistant_tools import actor
    started = asyncio.Event()
    async def slow(self, *args):
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(deployer.DockerGateway, 'create_container', slow)
    task = asyncio.create_task(deployer.provision(await actor(managed), deployer.Provision.model_validate(managed.body)))
    await asyncio.wait_for(started.wait(), timeout=3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    rows = (await managed.client.get('/api/docker_mgr/bundles')).json()['data']
    assert rows[0]['status'] == 'uncertain'
    assert rows[0]['resources']['volume'] in managed.objects


async def test_status_unavailable_requires_admin_and_creates_no_state(fleet):
    fleet.app.state.docker = None
    response = await fleet.client.get('/api/docker_mgr/status')
    assert response.status_code == 503 and response.json()['code'] == 'docker_unavailable'
    assert await fleet.app.state.db.fetchall("SELECT key FROM documents WHERE key LIKE 'docker:bundle:%'") == []


async def test_cancelled_staging_commit_does_not_leave_deploying_state(managed, monkeypatch):
    from backend import database
    from backend.modules.docker_mgr import deployer
    from test_assistant_tools import actor
    request = await actor(managed)
    original = database._settle
    commits = 0
    async def settle(operation):
        nonlocal commits
        committing = getattr(getattr(operation, 'cr_code', None), 'co_name', '') == 'commit'
        result = await original(operation)
        if committing:
            commits += 1
            if commits == 2:
                raise asyncio.CancelledError()
        return result
    monkeypatch.setattr(database, '_settle', settle)
    with pytest.raises(asyncio.CancelledError):
        await deployer.provision(request, deployer.Provision.model_validate(managed.body))
    rows = (await managed.client.get('/api/docker_mgr/bundles')).json()['data']
    assert rows[0]['status'] == 'uncertain'
    assert managed.effects == []


async def test_revocation_after_remote_deletes_blocks_token_mutation(managed, monkeypatch):
    from backend.modules.docker_mgr import deployer
    result = (await provision(managed)).json()
    original = deployer.DockerGateway.delete_network
    async def revoke(self, *args):
        await original(self, *args)
        await managed.app.state.db.execute("UPDATE users SET role='operator' WHERE username='owner'")
    monkeypatch.setattr(deployer.DockerGateway, 'delete_network', revoke)
    response = await managed.client.delete('/api/docker_mgr/bundles/' + result['id'], headers=managed.headers)
    assert response.status_code == 403
    token = await managed.app.state.db.fetchone('SELECT revoked_at FROM telemetry_tokens WHERE instance_id=?', (result['instance_id'],))
    assert token['revoked_at'] is None


async def test_replaced_network_blocks_container_creation(managed, monkeypatch):
    from backend.modules.docker_mgr import deployer
    async def replaced(self, *args):
        raise HTTPException(409, {'code': 'docker_ownership', 'detail': 'Ownership changed.'})
    monkeypatch.setattr(deployer.DockerGateway, 'inspect_network', replaced)
    response = await provision(managed)
    assert response.status_code == 409
    assert not any(action == 'create' for action, _ in managed.effects)
