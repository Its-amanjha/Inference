"""Promotion concurrency, mapping authorization and replay regressions."""
import asyncio
from contextlib import asynccontextmanager
import importlib
import json

import httpx

from test_fleet import fleet
from test_promotion import promotion, preview, deploy
from backend.modules.n8n_credentials.service import mapping_key


async def test_reconcile_waits_for_inflight_write_and_preserves_completed_run(promotion, monkeypatch):
    p = promotion
    writing = asyncio.Event()
    release = asyncio.Event()
    reconciling = asyncio.Event()
    original_write = p.replies[('target.example', 'POST', '/api/v1/workflows')]
    module = importlib.import_module('backend.modules.n8n_promote.router')
    original_guard = module.instance_guard

    async def held_write(request):
        writing.set()
        await asyncio.wait_for(release.wait(), 5)
        return await original_write(request)

    @asynccontextmanager
    async def observed_guard(request, identifier):
        if request.url.path.endswith('/reconcile') and identifier.startswith('run:'):
            reconciling.set()
        async with original_guard(request, identifier):
            yield

    monkeypatch.setattr(module, 'instance_guard', observed_guard)
    p.replies[('target.example', 'POST', '/api/v1/workflows')] = held_write
    before = await preview(p)
    deploying = asyncio.create_task(deploy(p, before['preview_id']))
    reconciliation = None
    try:
        await asyncio.wait_for(writing.wait(), 5)
        row = await p.app.state.db.fetchone('SELECT * FROM promotion_runs')
        assert row['status'] == 'writing'
        reconciliation = asyncio.create_task(p.client.post(
            f"/api/n8n_promote/runs/{row['id']}/reconcile", headers=p.headers,
            json={'target_workflow_id': 'target-wf'},
        ))
        await asyncio.wait_for(reconciling.wait(), 5)
        assert not reconciliation.done()
        release.set()
        completed, reconciled = await asyncio.wait_for(asyncio.gather(deploying, reconciliation), 5)
        assert completed.status_code == reconciled.status_code == 200
        assert completed.json() == reconciled.json()
        assert completed.json()['status'] == 'deployed'
        assert p.writes == 1
        saved = await p.app.state.db.fetchone('SELECT * FROM promotion_runs WHERE id=?', (row['id'],))
        state = json.loads(p.app.state.vault.decrypt(saved['state_enc']))
        assert saved['status'] == 'deployed'
        assert state['target_workflow_id'] == 'target-wf'
        assert state['deployed_hash'] == module.fingerprint(p.target_workflow)
        mappings = await p.app.state.db.fetchall('SELECT * FROM workflow_mappings')
        assert len(mappings) == 1
        assert mappings[0]['target_workflow_id'] == 'target-wf'
        events = await p.app.state.db.fetchall("SELECT outcome FROM audit_events WHERE action='promotion.deployed'")
        assert events == [{'outcome': 'deployed'}]
    finally:
        release.set()
        tasks = [task for task in (deploying, reconciliation) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_operator_cannot_reuse_mapping_for_different_credential_type(promotion):
    p = promotion
    p.source_workflow['nodes'][0]['credentials'] = {'httpHeaderAuth': {'id': 'source-cred', 'name': 'Service'}}
    await p.app.state.db.execute(
        'INSERT INTO credential_mappings(key,source_instance_id,target_instance_id,source_credential_id,target_credential_id,credential_type,target_name) VALUES(?,?,?,?,?,?,?)',
        (mapping_key(p.source, p.target, 'source-cred'), p.source, p.target,
         'source-cred', 'target-cred', 'httpBasicAuth', 'Service'),
    )
    await p.app.state.db.execute("UPDATE users SET role='operator' WHERE username='owner'")
    response = await p.client.post('/api/n8n_promote/preflight', headers=p.headers, json={
        'source_instance_id': p.source, 'target_instance_id': p.target, 'workflow_id': 'source-wf',
        'credential_map': {'source-cred': 'target-cred'},
    })
    assert response.status_code == 409, response.text
    assert response.json()['code'] == 'credential_binding_invalid'
    assert p.writes == 0
    assert not [call for call in p.calls if call.method in ('POST', 'PUT', 'PATCH', 'DELETE')]
    assert await p.app.state.db.fetchall('SELECT * FROM promotion_runs') == []


async def test_identical_connection_credentials_allow_uncertain_create_reconciliation(promotion):
    p = promotion
    original_write = p.replies[('target.example', 'POST', '/api/v1/workflows')]

    async def lost_response(request):
        await original_write(request)
        raise httpx.ReadTimeout('accepted write, lost response')

    p.replies[('target.example', 'POST', '/api/v1/workflows')] = lost_response
    before = await preview(p)
    deployed = await deploy(p, before['preview_id'])
    assert deployed.status_code == 200, deployed.text
    assert deployed.json()['status'] == 'uncertain'
    original = await p.app.state.db.fetchone('SELECT api_key_enc FROM instances WHERE id=?', (p.target,))
    updated = await p.client.patch(f'/api/n8n_proxy/instances/{p.target}', headers=p.headers,
                                  json={'url': 'https://target.example', 'api_key': 'target-api-key'})
    assert updated.status_code == 200, updated.text
    current = await p.app.state.db.fetchone('SELECT api_key_enc FROM instances WHERE id=?', (p.target,))
    assert current['api_key_enc'] != original['api_key_enc']
    assert p.app.state.vault.decrypt(current['api_key_enc']) == p.app.state.vault.decrypt(original['api_key_enc'])
    reconciled = await p.client.post(f"/api/n8n_promote/runs/{deployed.json()['id']}/reconcile",
                                     headers=p.headers, json={'target_workflow_id': 'target-wf'})
    assert reconciled.status_code == 200, reconciled.text
    assert reconciled.json()['status'] == 'deployed'
    assert p.writes == 1


async def test_idempotency_key_cannot_cross_previews_or_reconsume_preview(promotion):
    p = promotion
    first = await preview(p)
    second = await preview(p)
    completed = await deploy(p, first['preview_id'], 'shared-key')
    assert completed.status_code == 200, completed.text
    assert completed.json()['status'] == 'deployed'
    wrong_preview = await deploy(p, second['preview_id'], 'shared-key')
    assert wrong_preview.status_code == 409, wrong_preview.text
    assert wrong_preview.json()['code'] == 'idempotency_conflict'
    wrong_key = await deploy(p, first['preview_id'], 'different-key')
    assert wrong_key.status_code == 409, wrong_key.text
    assert wrong_key.json()['code'] == 'preview_consumed'
    replay = await deploy(p, first['preview_id'], 'shared-key')
    assert replay.status_code == 200
    assert replay.json() == completed.json()
    assert p.writes == 1
    assert len(await p.app.state.db.fetchall('SELECT * FROM promotion_runs')) == 1
