"""Repair and restore proposals through the approval gate against a simulated n8n API."""
import asyncio
from copy import deepcopy
import json

import httpx
import pytest

from backend.modules.assistant import repair_contract as contract
from backend.modules.assistant.repair_context import content_hash
from test_fleet import fleet, add

VERSION = '7c1e2d3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f'
HISTORY = '9d2a1b3c-5d7e-4f90-a1b2-c3d4e5f60718'
TOKEN = 'Bearer sk-live-PRIVATE-TOKEN-0123456789abcdef'


def workflow(active=False):
    return {
        'id': 'wf', 'name': 'Order synchronization', 'active': active, 'versionId': VERSION,
        'updatedAt': '2026-09-15T10:00:00.000Z',
        'nodes': [
            {'id': 'n1', 'name': 'Webhook', 'type': 'n8n-nodes-base.webhook', 'typeVersion': 2, 'position': [0, 0],
             'parameters': {'path': 'orders', 'httpMethod': 'POST'}, 'webhookId': 'hook-1'},
            {'id': 'n2', 'name': 'HTTP Request', 'type': 'n8n-nodes-base.httpRequest', 'typeVersion': 4.2, 'position': [200, 0],
             'parameters': {'url': 'https://api.example.com/v1/orders',
                            'headerParameters': {'parameters': [{'name': 'Authorization', 'value': TOKEN}]}},
             'credentials': {'httpHeaderAuth': {'id': 'cred-PRIVATE-1', 'name': 'Orders API'}}},
            {'id': 'n3', 'name': 'Code', 'type': 'n8n-nodes-base.code', 'typeVersion': 2, 'position': [400, 0],
             'parameters': {'jsCode': 'return item;'}},
        ],
        'connections': {'Webhook': {'main': [[{'node': 'HTTP Request', 'type': 'main', 'index': 0}]]},
                        'HTTP Request': {'main': [[{'node': 'Code', 'type': 'main', 'index': 0}]]}},
        'settings': {'executionOrder': 'v1'},
        'pinData': {'Webhook': [{'json': {'secret': 'PINNED-PRIVATE'}}]},
    }


@pytest.fixture
async def repair(fleet):
    fleet.instance = await add(fleet)
    fleet.workflow = workflow()
    fleet.versions = 0

    async def read(request):
        return httpx.Response(200, json=deepcopy(fleet.workflow))

    async def write(request):
        body = json.loads(request.content)
        fleet.workflow.update(deepcopy(body))
        fleet.versions += 1
        fleet.workflow['versionId'] = f'00000000-0000-4000-8000-{fleet.versions:012d}'
        return httpx.Response(200, json=deepcopy(fleet.workflow))

    async def deactivate(request):
        fleet.workflow['active'] = False
        return httpx.Response(200, json=deepcopy(fleet.workflow))

    fleet.replies[('alpha.example', 'GET', '/api/v1/workflows/wf')] = read
    fleet.replies[('alpha.example', 'PUT', '/api/v1/workflows/wf')] = write
    fleet.replies[('alpha.example', 'POST', '/api/v1/workflows/wf/deactivate')] = deactivate
    return fleet


def remote_writes(state, method=None):
    return [call for call in state.calls if call.url.host == 'alpha.example' and call.method in ({method} if method else {'POST', 'PUT', 'DELETE'})]


async def run(state, name, arguments, **extra):
    return await state.client.post('/api/assistant/tools/run', headers=state.headers, json={'name': name, 'arguments': arguments, **extra})


async def read_context(state):
    response = await run(state, 'get_workflow_context', {'instance_id': state.instance, 'workflow_id': 'wf'})
    assert response.status_code == 200, response.text
    return response.json()


def patch_arguments(state, digest, operations=None):
    return {'instance_id': state.instance, 'workflow_id': 'wf', 'base_content_hash': digest,
            'summary': 'Use the v2 orders endpoint and read all items.',
            'operations': operations or [
                {'op': 'set_parameter', 'node': 'HTTP Request', 'path': 'url', 'value': 'https://api.example.com/v2/orders'},
                {'op': 'set_code', 'node': 'Code', 'code': 'return items;'}]}


async def propose_patch(state, **extra):
    digest = (await read_context(state))['workflow']['content_hash']
    response = await run(state, 'propose_workflow_patch', patch_arguments(state, digest), **extra)
    assert response.status_code == 200, response.text
    return response.json()


async def confirm(state, action_id):
    return await state.client.post('/api/assistant/tools/confirm', headers=state.headers, json={'action_id': action_id})


async def status(state, action_id):
    return (await state.app.state.db.fetchone('SELECT status FROM approval_actions WHERE id=?', (action_id,)))['status']


async def audit_outcomes(state, action):
    rows = await state.app.state.db.fetchall('SELECT outcome FROM audit_events WHERE action=? ORDER BY id', (action,))
    return [row['outcome'] for row in rows]


async def test_context_is_masked_contract_shaped_and_read_only(repair):
    result = await read_context(repair)
    contract.RepairContextResponse.model_validate(result)
    text = json.dumps(result)
    assert 'sk-live-PRIVATE-TOKEN' not in text and 'PINNED-PRIVATE' not in text and 'cred-PRIVATE-1' not in text
    assert result['workflow']['content_hash'] == content_hash(repair.workflow)
    assert result['trust'] == contract.UNTRUSTED
    assert not remote_writes(repair)


async def test_patch_proposal_never_writes_and_exposes_preview(repair):
    result = await propose_patch(repair, chat_id='c1xFixtureChat')
    action = contract.PendingRepairAction.model_validate(result['pending_action'])
    assert 'linked_actions' not in result
    assert action.preview.kind == 'workflow_patch' and action.preview.preconditions == []
    assert action.chat_id == 'c1xFixtureChat'
    assert action.target.content_hash == content_hash(repair.workflow) and action.target.active is False
    assert action.target.workflow_name == 'Order synchronization' and action.target.instance_name == 'Alpha'
    assert any(change.kind == 'parameter' and change.path == 'parameters.url' for change in action.preview.changes)
    assert not remote_writes(repair)
    listed = (await repair.client.get('/api/assistant/approvals')).json()['data'][0]
    assert listed['chat_id'] == 'c1xFixtureChat' and listed['target']['workflow_id'] == 'wf'
    assert listed['preview_summary'] == action.preview.summary_text
    detail = await repair.client.get(f'/api/assistant/approvals/{action.id}')
    assert detail.status_code == 200 and detail.json() == result['pending_action']
    row = await repair.app.state.db.fetchone('SELECT state_enc FROM approval_actions WHERE id=?', (action.id,))
    assert 'orders' not in row['state_enc']


async def test_confirm_writes_once_snapshots_and_verifies(repair):
    original = deepcopy(repair.workflow)
    action = (await propose_patch(repair))['pending_action']
    response = await confirm(repair, action['id'])
    assert response.status_code == 200, response.text
    result = contract.RepairResult.model_validate(response.json()['result'])
    assert response.json()['status'] == 'succeeded'
    assert result.repair_id == action['id'] and result.verified and result.active is False
    assert result.content_hash == content_hash(repair.workflow) == response.json()['result']['content_hash']
    puts = remote_writes(repair, 'PUT')
    assert len(puts) == 1 and len(remote_writes(repair)) == 1
    body = json.loads(puts[0].content)
    assert set(body) == {'name', 'nodes', 'connections', 'settings'}
    nodes = {node['name']: node for node in repair.workflow['nodes']}
    assert nodes['HTTP Request']['parameters']['url'] == 'https://api.example.com/v2/orders'
    assert nodes['HTTP Request']['credentials'] == original['nodes'][1]['credentials']
    assert nodes['Code']['parameters']['jsCode'] == 'return items;'
    snapshot = await repair.app.state.db.fetchone('SELECT value_json FROM documents WHERE key=?', ('assistant:repair:' + action['id'],))
    assert snapshot and 'orders' not in snapshot['value_json'] and 'encrypted' in json.loads(snapshot['value_json'])
    assert await audit_outcomes(repair, 'assistant.workflow_repaired') == ['intent', 'success']
    assert await status(repair, action['id']) == 'succeeded'
    assert (await confirm(repair, action['id'])).status_code == 409
    assert len(remote_writes(repair, 'PUT')) == 1
    reconciled = await repair.client.get(f"/api/assistant/repair/{action['id']}/reconcile")
    assert reconciled.status_code == 200 and reconciled.json()['state'] == 'matches_desired'


async def test_stale_base_hash_and_content_edit_never_write(repair):
    stale = await run(repair, 'propose_workflow_patch', patch_arguments(repair, 'f' * 64))
    assert stale.status_code == 409 and stale.json()['code'] == 'context_stale'
    action = (await propose_patch(repair))['pending_action']
    repair.workflow['nodes'][2]['parameters']['jsCode'] = 'return edited;'
    response = await confirm(repair, action['id'])
    assert response.status_code == 409 and response.json()['code'] == 'approval_stale'
    assert await status(repair, action['id']) == 'stale'
    assert not remote_writes(repair)


async def test_metadata_changes_do_not_stale_but_connection_changes_do(repair):
    action = (await propose_patch(repair))['pending_action']
    repair.workflow.update(updatedAt='later', versionId=HISTORY, pinData={})
    assert (await confirm(repair, action['id'])).status_code == 200
    operations = [{'op': 'set_code', 'node': 'Code', 'code': 'return items.slice(0, 10);'}]
    response = await run(repair, 'propose_workflow_patch', patch_arguments(repair, content_hash(repair.workflow), operations))
    assert response.status_code == 200, response.text
    action = response.json()['pending_action']
    await repair.app.state.db.execute('UPDATE instances SET api_key_enc=? WHERE id=?', (repair.app.state.vault.encrypt('rotated'), repair.instance))
    response = await confirm(repair, action['id'])
    assert response.status_code == 409 and response.json()['code'] == 'approval_stale'
    assert len(remote_writes(repair, 'PUT')) == 1


async def test_active_workflow_links_deactivation_and_apply_waits_for_it(repair):
    repair.workflow['active'] = True
    result = await propose_patch(repair, chat_id='chat-1')
    patch, linked = result['pending_action'], result['linked_actions'][0]
    assert linked['name'] == 'deactivate_workflow' and linked['linked_to'] == patch['id'] and linked['chat_id'] == 'chat-1'
    assert patch['preview']['preconditions'] == [{'code': 'workflow_active', 'satisfied': False, 'tool': 'deactivate_workflow', 'action_id': linked['id']}]
    assert patch['target']['active'] is True and not remote_writes(repair)
    blocked = await confirm(repair, patch['id'])
    assert blocked.status_code == 409 and blocked.json()['code'] == 'workflow_active'
    assert not remote_writes(repair) and await status(repair, patch['id']) == 'pending'
    deactivated = await confirm(repair, linked['id'])
    assert deactivated.status_code == 200, deactivated.text
    detail = (await repair.client.get(f"/api/assistant/approvals/{patch['id']}")).json()
    assert detail['preview']['preconditions'][0]['satisfied'] is True
    applied = await confirm(repair, patch['id'])
    assert applied.status_code == 200, applied.text
    assert len(remote_writes(repair, 'POST')) == 1 and len(remote_writes(repair, 'PUT')) == 1


async def test_linked_records_are_stored_atomically(repair, monkeypatch):
    from backend.modules.assistant import approvals
    monkeypatch.setattr(approvals, 'MAX_OWN_PENDING', 1)
    repair.workflow['active'] = True
    digest = (await read_context(repair))['workflow']['content_hash']
    response = await run(repair, 'propose_workflow_patch', patch_arguments(repair, digest))
    assert response.status_code == 429 and response.json()['code'] == 'approval_limit'
    assert await repair.app.state.db.fetchall('SELECT id FROM approval_actions') == []


async def test_concurrent_confirmations_write_once(repair):
    action = (await propose_patch(repair))['pending_action']
    responses = await asyncio.gather(confirm(repair, action['id']), confirm(repair, action['id']))
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert len(remote_writes(repair, 'PUT')) == 1


async def test_put_timeout_is_uncertain_and_never_retried(repair):
    action = (await propose_patch(repair))['pending_action']
    repair.replies[('alpha.example', 'PUT', '/api/v1/workflows/wf')] = httpx.ReadTimeout('secret detail')
    response = await confirm(repair, action['id'])
    assert response.status_code == 504 and response.json()['code'] == 'upstream_outcome_unknown'
    assert 'secret detail' not in response.text
    assert await status(repair, action['id']) == 'uncertain'
    assert await audit_outcomes(repair, 'assistant.workflow_repaired') == ['intent', 'unknown']
    assert (await confirm(repair, action['id'])).status_code == 409
    assert len(remote_writes(repair, 'PUT')) == 1
    reconciled = await repair.client.get(f"/api/assistant/repair/{action['id']}/reconcile")
    assert reconciled.json()['state'] == 'matches_before'


async def test_readback_mismatch_is_uncertain(repair):
    action = (await propose_patch(repair))['pending_action']
    repair.replies[('alpha.example', 'PUT', '/api/v1/workflows/wf')] = httpx.Response(200, json={'id': 'wf'})
    response = await confirm(repair, action['id'])
    assert response.status_code == 502 and response.json()['code'] == 'upstream_outcome_unknown'
    assert await status(repair, action['id']) == 'uncertain'
    assert len(remote_writes(repair, 'PUT')) == 1


async def test_activation_during_write_is_uncertain(repair):
    action = (await propose_patch(repair))['pending_action']
    async def activates(request):
        repair.workflow.update(json.loads(request.content), active=True)
        return httpx.Response(200, json=deepcopy(repair.workflow))
    repair.replies[('alpha.example', 'PUT', '/api/v1/workflows/wf')] = activates
    response = await confirm(repair, action['id'])
    assert response.status_code == 502 and await status(repair, action['id']) == 'uncertain'


async def test_upstream_rejection_is_failed_not_uncertain(repair):
    action = (await propose_patch(repair))['pending_action']
    repair.replies[('alpha.example', 'PUT', '/api/v1/workflows/wf')] = httpx.Response(422, json={'message': 'secret detail'})
    response = await confirm(repair, action['id'])
    assert response.status_code == 422 and response.json()['code'] == 'upstream_rejected'
    assert await status(repair, action['id']) == 'failed'
    assert await audit_outcomes(repair, 'assistant.workflow_repaired') == ['intent', 'failed']


async def test_cancelled_write_is_uncertain(repair):
    from test_assistant_tools import actor
    from backend.modules.assistant import approvals
    action = (await propose_patch(repair))['pending_action']
    started = asyncio.Event()
    async def blocked(request):
        started.set()
        await asyncio.Event().wait()
    repair.replies[('alpha.example', 'PUT', '/api/v1/workflows/wf')] = blocked
    task = asyncio.create_task(approvals.confirm(await actor(repair), action['id']))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await status(repair, action['id']) == 'uncertain'
    assert await audit_outcomes(repair, 'assistant.workflow_repaired') == ['intent', 'unknown']


async def test_restore_from_snapshot_undoes_repair(repair):
    original_hash = content_hash(repair.workflow)
    patch = (await propose_patch(repair))['pending_action']
    assert (await confirm(repair, patch['id'])).status_code == 200
    arguments = {'instance_id': repair.instance, 'workflow_id': 'wf', 'base_content_hash': content_hash(repair.workflow), 'repair_id': patch['id']}
    response = await run(repair, 'propose_restore_workflow', arguments)
    assert response.status_code == 200, response.text
    restore = contract.PendingRepairAction.model_validate(response.json()['pending_action'])
    assert restore.preview.kind == 'workflow_restore' and patch['id'] in restore.preview.summary_text
    assert restore.preview.apply.label == 'Restore version'
    assert len(remote_writes(repair, 'PUT')) == 1
    confirmed = await confirm(repair, restore.id)
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()['result']['tool'] == 'propose_restore_workflow'
    assert content_hash(repair.workflow) == original_hash
    assert len(remote_writes(repair, 'PUT')) == 2
    assert await audit_outcomes(repair, 'assistant.workflow_restored') == ['intent', 'success']


async def test_restore_from_version_history(repair):
    old = workflow()
    old['nodes'][1]['parameters']['url'] = 'https://api.example.com/v0/orders'
    repair.replies[('alpha.example', 'GET', f'/api/v1/workflows/wf/{HISTORY}')] = httpx.Response(200, json={
        'versionId': HISTORY, 'workflowId': 'wf', 'nodes': old['nodes'], 'connections': old['connections'], 'authors': 'owner'})
    repair.workflow['settings']['timezone'] = 'Europe/Berlin'
    arguments = {'instance_id': repair.instance, 'workflow_id': 'wf', 'base_content_hash': content_hash(repair.workflow), 'version_id': HISTORY}
    response = await run(repair, 'propose_restore_workflow', arguments)
    assert response.status_code == 200, response.text
    action = response.json()['pending_action']
    assert 'settings keep their current values' in action['warning']
    assert any(change['path'] == 'parameters.url' and change['after'] == 'https://api.example.com/v0/orders' for change in action['preview']['changes'])
    assert (await confirm(repair, action['id'])).status_code == 200
    body = json.loads(remote_writes(repair, 'PUT')[0].content)
    assert body['nodes'] == old['nodes'] and body['settings'] == {'executionOrder': 'v1', 'timezone': 'Europe/Berlin'}


@pytest.mark.parametrize('source', ['missing_version', 'forbidden_version', 'unknown_repair', 'foreign_repair'])
async def test_unavailable_history_is_reported_without_state(repair, source):
    other = '11111111-2222-4333-8444-555555555555'
    arguments = {'instance_id': repair.instance, 'workflow_id': 'wf', 'base_content_hash': content_hash(repair.workflow)}
    if source in ('missing_version', 'forbidden_version'):
        repair.replies[('alpha.example', 'GET', f'/api/v1/workflows/wf/{other}')] = httpx.Response(404 if source == 'missing_version' else 403)
        arguments['version_id'] = other
    elif source == 'unknown_repair':
        arguments['repair_id'] = 'NoSuchRepair'
    else:
        encrypted = repair.app.state.vault.encrypt(json.dumps({'instance_id': repair.instance, 'workflow_id': 'other', 'before': workflow()}))
        await repair.app.state.db.execute('INSERT INTO documents(key,value_json) VALUES(?,?)', ('assistant:repair:Foreign', json.dumps({'encrypted': encrypted})))
        arguments['repair_id'] = 'Foreign'
    response = await run(repair, 'propose_restore_workflow', arguments)
    assert response.status_code == 404 and response.json()['code'] == 'history_unavailable'
    assert await repair.app.state.db.fetchall('SELECT id FROM approval_actions') == []
    assert not remote_writes(repair)


def test_blocked_paths_use_views_the_model_saw():
    from backend.modules.assistant.repair_actions import blocked_paths
    document = workflow()
    digest = content_hash(document)
    assert blocked_paths(document, digest) == frozenset()
    views = [{'content_hash': digest, 'truncated_paths': ['Code.parameters.jsCode', 'HTTP Request.parameters.url']},
             {'content_hash': digest, 'truncated_paths': ['Code.parameters.jsCode']},
             {'content_hash': 'f' * 64, 'truncated_paths': []}]
    assert blocked_paths(document, digest, views) == frozenset({'Code.parameters.jsCode'})
    assert blocked_paths(document, digest, [{'content_hash': 'f' * 64, 'truncated_paths': []}]) == frozenset()


async def test_code_truncated_in_context_cannot_be_replaced(repair):
    repair.workflow['nodes'][2]['parameters']['jsCode'] = 'return items;\n' + '// padding\n' * 2000
    context = await read_context(repair)
    assert 'Code.parameters.jsCode' in context['workflow']['truncated_paths']
    operations = [{'op': 'set_code', 'node': 'Code', 'code': 'return [];'}]
    response = await run(repair, 'propose_workflow_patch', patch_arguments(repair, context['workflow']['content_hash'], operations))
    assert response.status_code == 422 and response.json()['code'] == 'invalid_patch' and 'truncated' in response.json()['detail']
    assert await repair.app.state.db.fetchall('SELECT id FROM approval_actions') == []


async def test_invalid_patches_create_nothing(repair):
    digest = (await read_context(repair))['workflow']['content_hash']
    for operations in ([{'op': 'set_parameter', 'node': 'Missing', 'path': 'url', 'value': 'x'}],
                       [{'op': 'set_parameter', 'node': 'HTTP Request', 'path': 'url', 'value': 'https://api.example.com/v1/orders'}]):
        response = await run(repair, 'propose_workflow_patch', patch_arguments(repair, digest, operations))
        assert response.status_code == 422 and response.json()['code'] == 'invalid_patch'
    assert await repair.app.state.db.fetchall('SELECT id FROM approval_actions') == []


async def test_roles_sessions_and_live_recheck(repair):
    action = (await propose_patch(repair))['pending_action']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=repair.app), base_url='https://localhost') as other:
        login = await other.post('/api/auth/login', json={'username': 'owner', 'password': 'test password of sufficient length'})
        assert login.status_code == 200
        headers = {'X-AGD-CSRF': other.cookies['agd_csrf']}
        assert (await other.post('/api/assistant/tools/confirm', headers=headers, json={'action_id': action['id']})).status_code == 404
        assert (await other.get(f"/api/assistant/approvals/{action['id']}")).status_code == 404
        assert (await other.get(f"/api/assistant/repair/{action['id']}/reconcile")).status_code == 404
    await repair.app.state.db.execute("UPDATE users SET role='viewer' WHERE username='owner'")
    assert (await confirm(repair, action['id'])).status_code == 403
    assert (await read_context(repair))['workflow']['id'] == 'wf'
    digest = content_hash(repair.workflow)
    assert (await run(repair, 'propose_workflow_patch', patch_arguments(repair, digest))).status_code == 403
    assert not remote_writes(repair)


async def test_reconcile_reports_missing_workflow(repair):
    action = (await propose_patch(repair))['pending_action']
    repair.replies[('alpha.example', 'GET', '/api/v1/workflows/wf')] = httpx.Response(404)
    response = await repair.client.get(f"/api/assistant/repair/{action['id']}/reconcile")
    assert response.status_code == 200 and response.json()['state'] == 'not_found'
    assert not remote_writes(repair)
