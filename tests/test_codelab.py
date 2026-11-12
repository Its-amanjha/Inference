import asyncio
from copy import deepcopy
import json
import hashlib

import httpx
import pytest

from test_fleet import fleet, add


@pytest.fixture
async def lab(fleet):
    fleet.instance = await add(fleet)
    fleet.workflow = {'id': 'same', 'name': 'Example', 'active': False,
                      'nodes': [{'id': 'code', 'type': 'n8n-nodes-base.code',
                                 'parameters': {'jsCode': 'return items;', 'mode': 'runOnceForAllItems'},
                                 'position': [0, 0]}], 'connections': {}, 'settings': {},
                      'updatedAt': 'original', 'tags': [{'id': 'tag'}]}
    async def read(request):
        return httpx.Response(200, json=deepcopy(fleet.workflow))
    async def write(request):
        fleet.workflow.update(json.loads(request.content))
        return httpx.Response(200, json=deepcopy(fleet.workflow))
    fleet.replies[('alpha.example', 'GET', '/api/v1/workflows/same')] = read
    fleet.replies[('alpha.example', 'PUT', '/api/v1/workflows/same')] = write
    fleet.url = f'/api/n8n_proxy/workflows/same/nodes/code?instance_id={fleet.instance}'
    return fleet


async def snapshot(lab):
    result = await lab.client.get(lab.url)
    assert result.status_code == 200, result.text
    return result.json()['workflow_hash']


async def save(lab, hash_value, code='return [];'):
    return await lab.client.put(lab.url, headers=lab.headers,
                                json={'expected_hash': hash_value, 'code': code})


async def test_updates_only_code_and_writable_fields(lab):
    before = deepcopy(lab.workflow)
    old_hash = await snapshot(lab)
    result = await save(lab, old_hash)
    assert result.status_code == 200, result.text
    assert result.json()['workflow_hash'] != old_hash
    assert result.json()['code'] == 'return [];'
    before['nodes'][0]['parameters']['jsCode'] = 'return [];'
    assert lab.workflow == before
    sent = [json.loads(call.content) for call in lab.calls if call.method == 'PUT']
    assert sent == [{key: before[key] for key in ('name', 'nodes', 'connections', 'settings')}]
    rows = await lab.app.state.db.fetchall("SELECT outcome FROM audit_events WHERE action='fleet.node_updated' ORDER BY id")
    assert [row['outcome'] for row in rows] == ['intent', 'success']


@pytest.mark.parametrize('change', ['stale', 'active', 'unknown_active', 'duplicate', 'non_code', 'language'])
async def test_invalid_targets_never_write(lab, change):
    old_hash = await snapshot(lab)
    if change == 'stale':
        lab.workflow['name'] = 'Changed elsewhere'
    elif change == 'active':
        lab.workflow['active'] = True
    elif change == 'unknown_active':
        del lab.workflow['active']
    elif change == 'duplicate':
        lab.workflow['nodes'].append(deepcopy(lab.workflow['nodes'][0]))
    elif change == 'non_code':
        lab.workflow['nodes'][0]['type'] = 'n8n-nodes-base.httpRequest'
    else:
        lab.workflow['nodes'][0]['parameters']['language'] = 'shell'
    if change != 'stale':
        old_hash = hashlib.sha256(json.dumps(lab.workflow, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    result = await save(lab, old_hash)
    assert result.status_code in (409, 422)
    assert not any(call.method == 'PUT' for call in lab.calls)


async def test_viewer_cannot_read_or_write_and_csrf_required(lab):
    old_hash = await snapshot(lab)
    result = await lab.client.put(lab.url, json={'expected_hash': old_hash, 'code': ''})
    assert result.status_code == 403
    await lab.app.state.db.execute("UPDATE users SET role='viewer' WHERE username='owner'")
    # The editor's read is unmasked, so it needs the role that may edit; viewers read masked code elsewhere.
    assert (await lab.client.get(lab.url)).status_code == 403
    assert (await save(lab, old_hash)).status_code == 403
    assert not any(call.method == 'PUT' for call in lab.calls)


async def test_timeout_is_unknown_and_never_retried(lab):
    old_hash = await snapshot(lab)
    lab.replies[('alpha.example', 'PUT', '/api/v1/workflows/same')] = httpx.ReadTimeout('secret detail')
    result = await save(lab, old_hash)
    assert result.status_code == 504
    assert result.json()['code'] == 'upstream_outcome_unknown'
    assert sum(call.method == 'PUT' for call in lab.calls) == 1
    rows = await lab.app.state.db.fetchall("SELECT outcome FROM audit_events WHERE action='fleet.node_updated' ORDER BY id")
    assert [row['outcome'] for row in rows] == ['intent', 'unknown']


async def test_concurrent_updates_serialize_and_one_is_stale(lab):
    old_hash = await snapshot(lab)
    results = await asyncio.gather(save(lab, old_hash, 'first'), save(lab, old_hash, 'second'))
    assert sorted(result.status_code for result in results) == [200, 409]
    assert sum(call.method == 'PUT' for call in lab.calls) == 1


async def test_readback_mismatch_is_unknown(lab):
    old_hash = await snapshot(lab)
    lab.replies[('alpha.example', 'PUT', '/api/v1/workflows/same')] = httpx.Response(200, json={'id': 'same'})
    result = await save(lab, old_hash)
    assert result.status_code == 502
    assert result.json()['code'] == 'upstream_outcome_unknown'


@pytest.mark.parametrize('language',['python','pythonNative'])
async def test_python_updates_python_field(lab,language):
    lab.workflow['nodes'][0]['parameters'].update(language=language, pythonCode='return items')
    old_hash = await snapshot(lab)
    result = await save(lab, old_hash, 'return []')
    assert result.status_code == 200, result.text
    assert result.json()['language'] == language
    assert lab.workflow['nodes'][0]['parameters']['jsCode'] == 'return items;'


async def test_code_limit_counts_utf8_bytes(lab):
    old_hash = await snapshot(lab)
    result = await save(lab, old_hash, '\u20ac' * 100000)
    assert result.status_code == 422
    assert not any(call.method == 'PUT' for call in lab.calls)


async def test_upstream_rejection_records_failed(lab):
    old_hash = await snapshot(lab)
    lab.replies[('alpha.example', 'PUT', '/api/v1/workflows/same')] = httpx.Response(422, json={'message': 'secret detail'})
    result = await save(lab, old_hash)
    assert result.status_code == 422
    rows = await lab.app.state.db.fetchall("SELECT outcome FROM audit_events WHERE action='fleet.node_updated' ORDER BY id")
    assert [row['outcome'] for row in rows] == ['intent', 'failed']


async def test_role_demoted_during_read_cannot_write(lab):
    old_hash = await snapshot(lab)
    async def read_and_demote(request):
        await lab.app.state.db.execute("UPDATE users SET role='viewer' WHERE username='owner'")
        return httpx.Response(200, json=lab.workflow)
    lab.replies[('alpha.example', 'GET', '/api/v1/workflows/same')] = read_and_demote
    assert (await save(lab, old_hash)).status_code == 403
    assert not any(call.method == 'PUT' for call in lab.calls)


async def test_other_instance_is_explicit_and_separate(lab):
    beta = await add(lab, 'Beta', 'https://beta.example', 'beta-secret-key')
    other = deepcopy(lab.workflow)
    other['nodes'][0]['parameters']['jsCode'] = 'beta code'
    lab.replies[('beta.example', 'GET', '/api/v1/workflows/same')] = httpx.Response(200, json=other)
    response = await lab.client.get(lab.url.replace(lab.instance, beta))
    assert response.status_code == 200
    assert response.json()['code'] == 'beta code'
    assert lab.calls[-1].headers['X-N8N-API-KEY'] == 'beta-secret-key'
    assert (await lab.client.get('/api/n8n_proxy/workflows/same/nodes/code')).status_code == 422


async def test_cancellation_after_write_started_records_unknown(lab):
    old_hash = await snapshot(lab)
    started = asyncio.Event()
    async def blocked_write(request):
        started.set()
        await asyncio.Event().wait()
    lab.replies[('alpha.example', 'PUT', '/api/v1/workflows/same')] = blocked_write
    task = asyncio.create_task(save(lab, old_hash))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    rows = await lab.app.state.db.fetchall("SELECT outcome FROM audit_events WHERE action='fleet.node_updated' ORDER BY id")
    assert [row['outcome'] for row in rows] == ['intent', 'unknown']


async def test_unexpected_activation_on_readback_is_unknown(lab):
    old_hash = await snapshot(lab)
    async def activates_on_write(request):
        lab.workflow.update(json.loads(request.content))
        lab.workflow['active'] = True
        return httpx.Response(200, json=lab.workflow)
    lab.replies[('alpha.example', 'PUT', '/api/v1/workflows/same')] = activates_on_write
    response = await save(lab, old_hash)
    assert response.status_code == 502
    assert response.json()['code'] == 'upstream_outcome_unknown'
