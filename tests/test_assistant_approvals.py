import asyncio
from copy import deepcopy

import httpx
import pytest

from test_fleet import fleet, add


@pytest.fixture
async def assistant(fleet):
    fleet.instance = await add(fleet)
    fleet.workflow = {'id': 'same', 'name': 'Example', 'active': False,
                      'nodes': [], 'connections': {}, 'settings': {}}
    async def read(request):
        return httpx.Response(200, json=deepcopy(fleet.workflow))
    async def activate(request):
        fleet.workflow['active'] = True
        return httpx.Response(200, json=deepcopy(fleet.workflow))
    fleet.replies[('alpha.example', 'GET', '/api/v1/workflows/same')] = read
    fleet.replies[('alpha.example', 'POST', '/api/v1/workflows/same/activate')] = activate
    return fleet


async def propose(state):
    response = await state.client.post('/api/assistant/tools/run', headers=state.headers,
        json={'name': 'activate_workflow', 'arguments': {'instance_id': state.instance, 'workflow_id': 'same'}})
    assert response.status_code == 200, response.text
    return response.json()['pending_action']['id']


async def confirm(state, action):
    return await state.client.post('/api/assistant/tools/confirm', headers=state.headers, json={'action_id': action})


async def test_proposal_never_writes_and_confirm_is_single_use(assistant):
    action = await propose(assistant)
    assert not any(c.method == 'POST' for c in assistant.calls)
    responses = await asyncio.gather(confirm(assistant, action), confirm(assistant, action))
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert sum(c.method == 'POST' for c in assistant.calls) == 1
    row = await assistant.app.state.db.fetchone('SELECT * FROM approval_actions WHERE id=?', (action,))
    assert row['status'] == 'succeeded'
    assert 'workflow_id' not in row['state_enc']


@pytest.mark.parametrize('change', ['workflow', 'connection', 'expired', 'cancel', 'restart'])
async def test_stale_expired_cancelled_restart_cannot_execute(assistant, change):
    action = await propose(assistant)
    if change == 'workflow':
        assistant.workflow['name'] = 'Changed'
    elif change == 'connection':
        await assistant.app.state.db.execute("UPDATE instances SET url='https://other.example' WHERE id=?", (assistant.instance,))
    elif change == 'expired':
        await assistant.app.state.db.execute('UPDATE approval_actions SET expires_at=0 WHERE id=?', (action,))
    elif change == 'cancel':
        result = await assistant.client.post('/api/assistant/tools/cancel', headers=assistant.headers, json={'action_id': action})
        assert result.status_code == 200, result.text
    else:
        from backend.modules.assistant.approvals import initialize
        await initialize(assistant.app)
    assert (await confirm(assistant, action)).status_code == 409
    assert not any(c.method == 'POST' for c in assistant.calls)


async def test_wrong_session_and_demoted_actor(assistant):
    action = await propose(assistant)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=assistant.app), base_url='https://localhost') as other:
        login = await other.post('/api/auth/login', json={'username': 'owner', 'password': 'test password of sufficient length'})
        assert login.status_code == 200
        response = await other.post('/api/assistant/tools/confirm', headers={'X-AGD-CSRF': other.cookies['agd_csrf']}, json={'action_id': action})
        assert response.status_code == 404
    await assistant.app.state.db.execute("UPDATE users SET role='viewer' WHERE username='owner'")
    assert (await confirm(assistant, action)).status_code == 403
    assert not any(c.method == 'POST' for c in assistant.calls)


async def test_timeout_is_durable_uncertain_and_not_replayed(assistant):
    action = await propose(assistant)
    assistant.replies[('alpha.example', 'POST', '/api/v1/workflows/same/activate')] = httpx.ReadTimeout('private-key')
    response = await confirm(assistant, action)
    assert response.status_code == 504
    assert 'private-key' not in response.text
    row = await assistant.app.state.db.fetchone('SELECT status FROM approval_actions WHERE id=?', (action,))
    assert row['status'] == 'uncertain'
    assert (await confirm(assistant, action)).status_code == 409
    assert sum(c.method == 'POST' for c in assistant.calls) == 1


async def test_confirmation_requires_csrf_and_arguments_cannot_be_replaced(assistant):
    action = await propose(assistant)
    response = await assistant.client.post('/api/assistant/tools/confirm', json={'action_id': action})
    assert response.status_code == 403
    response = await assistant.client.post('/api/assistant/tools/confirm', headers=assistant.headers,
        json={'action_id': action, 'arguments': {'workflow_id': 'other'}})
    assert response.status_code == 422
    assert not any(c.method == 'POST' for c in assistant.calls)


async def test_cancelled_remote_write_is_uncertain(assistant):
    from test_assistant_tools import actor
    from backend.modules.assistant import approvals
    action = await propose(assistant)
    started = asyncio.Event()
    async def slow(request):
        started.set()
        await asyncio.Event().wait()
    assistant.replies[('alpha.example', 'POST', '/api/v1/workflows/same/activate')] = slow
    task = asyncio.create_task(approvals.confirm(await actor(assistant), action))
    await asyncio.wait_for(started.wait(), timeout=3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = await assistant.app.state.db.fetchone('SELECT status FROM approval_actions WHERE id=?', (action,))
    assert row['status'] == 'uncertain'


async def test_cancellation_after_claim_commit_is_settled(assistant, monkeypatch):
    from test_assistant_tools import actor
    from backend.modules.assistant import approvals
    from backend import database
    action = await propose(assistant)
    request = await actor(assistant)
    original = database._settle
    interrupted = False
    async def settled(operation):
        nonlocal interrupted
        committing = getattr(getattr(operation, 'cr_code', None), 'co_name', '') == 'commit'
        result = await original(operation)
        if committing and not interrupted:
            interrupted = True
            raise asyncio.CancelledError()
        return result
    monkeypatch.setattr(database, '_settle', settled)
    with pytest.raises(asyncio.CancelledError):
        await approvals.confirm(request, action)
    row = await assistant.app.state.db.fetchone('SELECT status FROM approval_actions WHERE id=?', (action,))
    assert interrupted and row['status'] == 'failed'
    assert not any(c.method == 'POST' for c in assistant.calls)


async def test_expiry_while_rechecking_target_prevents_write(assistant, monkeypatch):
    from types import SimpleNamespace
    from backend.modules.assistant import approvals
    action = await propose(assistant)
    now = approvals.time.time()
    original = approvals.fingerprint
    async def delayed(*args):
        result = await original(*args)
        monkeypatch.setattr(approvals, 'time', SimpleNamespace(time=lambda: now + 1000))
        return result
    monkeypatch.setattr(approvals, 'fingerprint', delayed)
    result = await confirm(assistant, action)
    assert result.status_code == 409 and result.json()['code'] == 'approval_expired'
    assert not any(c.method == 'POST' for c in assistant.calls)
    row = await assistant.app.state.db.fetchone('SELECT status FROM approval_actions WHERE id=?', (action,))
    assert row['status'] == 'expired'


async def test_pending_cache_and_durable_limit(assistant, monkeypatch):
    from backend.modules.assistant import approvals
    monkeypatch.setattr(approvals, 'MAX_PENDING', 1)
    await propose(assistant)
    response = await assistant.client.post('/api/assistant/tools/run', headers=assistant.headers,
        json={'name': 'activate_workflow', 'arguments': {'instance_id': assistant.instance, 'workflow_id': 'same'}})
    assert response.status_code == 429
    assert len(assistant.app.state.assistant_pending) == 1
    assert len(await assistant.app.state.db.fetchall('SELECT * FROM approval_actions')) == 1
