import asyncio
from types import SimpleNamespace

import httpx
import pytest

from backend.main import create_app


@pytest.fixture
async def fleet(tmp_path, monkeypatch):
    state=SimpleNamespace(calls=[],replies={})

    async def upstream(request):
        state.calls.append(request)
        configured=state.replies.get((request.url.host,request.method,request.url.path))
        if isinstance(configured,Exception):
            raise configured
        if callable(configured):
            return await configured(request)
        if configured is not None:
            return configured
        if request.url.path.endswith(('/workflows','/executions')):
            return httpx.Response(200,json={'data':[],'nextCursor':None})
        if '/executions/' in request.url.path:
            return httpx.Response(200,json={'id':'same','status':'error','data':{'resultData':{'runData':{}}}})
        return httpx.Response(200,json={'id':'same','name':'Workflow','active':request.url.path.endswith('/activate')})

    monkeypatch.setattr('backend.main.create_http_client',lambda *args: httpx.AsyncClient(transport=httpx.MockTransport(upstream),trust_env=False))
    app=create_app(data_dir=tmp_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,client=('127.0.0.1',50000)),base_url='https://localhost') as client:
            result=await client.post('/api/auth/setup',json={'username':'owner','password':'test password of sufficient length'})
            assert result.status_code==200
            state.app=app
            state.client=client
            state.headers={'X-AGD-CSRF':client.cookies['agd_csrf']}
            yield state


async def add(fleet,name='Alpha',url='https://alpha.example',key='alpha-secret-key'):
    r=await fleet.client.post('/api/n8n_proxy/instances',headers=fleet.headers,json={'name':name,'url':url,'api_key':key})
    assert r.status_code==200,r.text
    return r.json()['id']


async def test_workflow_proxy_masks_secret_parameters_for_browsers(fleet):
    instance=await add(fleet)
    document={'id':'wf','name':'Order sync','active':False,'connections':{},'settings':{},'nodes':[
        {'id':'n1','name':'HTTP Request','type':'n8n-nodes-base.httpRequest','typeVersion':4.2,'position':[0,0],
         'parameters':{'url':'https://api.example.com/v1/orders','sendHeaders':True,
                       'headerParameters':{'parameters':[{'name':'Authorization','value':'Bearer sk-live-PROXY-SECRET-TOKEN-1234567890'}]}}},
        {'id':'c1','name':'Code','type':'n8n-nodes-base.code','typeVersion':2,'position':[250,0],
         'parameters':{'jsCode':'return items;'}}]}
    fleet.replies[('alpha.example','GET','/api/v1/workflows/wf')]=httpx.Response(200,json=document)
    fleet.replies[('alpha.example','GET','/api/v1/workflows')]=httpx.Response(200,json={'data':[document],'nextCursor':None})
    detail=await fleet.client.get('/api/n8n_proxy/workflows/wf',params={'instance_id':instance})
    assert detail.status_code==200,detail.text
    assert 'sk-live-PROXY-SECRET-TOKEN' not in detail.text
    body=detail.json()
    assert body['name']=='Order sync' and body['instance_id']==instance
    assert body['nodes'][0]['parameters']['url']=='https://api.example.com/v1/orders'
    assert body['nodes'][1]['parameters']['jsCode']=='return items;'
    listing=await fleet.client.get('/api/n8n_proxy/workflows',params={'instance_id':instance})
    assert listing.status_code==200 and 'sk-live-PROXY-SECRET-TOKEN' not in listing.text
    assert listing.json()['data'][0]['name']=='Order sync'
    # CodeLab reads its own endpoint and still receives the real code it must write back.
    code=await fleet.client.get('/api/n8n_proxy/workflows/wf/nodes/c1',params={'instance_id':instance})
    assert code.status_code==200,code.text
    assert code.json()['code']=='return items;'


async def test_instance_crud_keeps_credentials_private_and_removes_only_local_record(fleet):
    instance=await add(fleet,url='https://alpha.example/n8n/')
    row=await fleet.app.state.db.fetchone('SELECT * FROM instances WHERE id=?',(instance,))
    assert row['api_key_enc']!='alpha-secret-key'
    assert fleet.app.state.vault.decrypt(row['api_key_enc'])=='alpha-secret-key'
    r=await fleet.client.get('/api/n8n_proxy/instances')
    assert r.json()['data'][0]['url']=='https://alpha.example/n8n'
    assert 'api_key_enc' not in r.text and 'alpha-secret-key' not in r.text
    assert r.json()['data'][0]['api_key_configured'] is True
    updated=await fleet.client.patch(f'/api/n8n_proxy/instances/{instance}',headers=fleet.headers,json={'name':'Renamed','api_key':'rotated-key-value'})
    assert updated.status_code==200,updated.text
    assert updated.json()['name']=='Renamed'
    assert (await fleet.client.delete(f'/api/n8n_proxy/instances/{instance}',headers=fleet.headers)).status_code==200
    assert (await fleet.client.get(f'/api/n8n_proxy/instances/{instance}')).status_code==404
    assert not fleet.calls


@pytest.mark.parametrize('url',['https://user:pass@alpha.example','http://169.254.169.254','http://127.0.0.1','https://alpha.example/../admin','https://alpha.example/%2e%2e/admin','https://alpha.example?url=other','https://alpha.example/#frag','file:///etc/passwd'])
async def test_invalid_instance_destinations_rejected(fleet,url):
    r=await fleet.client.post('/api/n8n_proxy/instances',headers=fleet.headers,json={'name':'Unsafe','url':url,'api_key':'test-secret'})
    assert r.status_code==422,r.text
    assert not fleet.calls


async def test_control_characters_in_api_key_are_rejected(fleet):
    r=await fleet.client.post('/api/n8n_proxy/instances',headers=fleet.headers,json={'name':'Unsafe','url':'https://alpha.example','api_key':'bad\r\nAuthorization: bearer injected'})
    assert r.status_code==422
    assert 'injected' not in r.text


async def test_missing_instance_and_missing_csrf_never_send_mutation(fleet):
    r=await fleet.client.post('/api/n8n_proxy/instances',json={'name':'Alpha','url':'https://alpha.example','api_key':'alpha-key'})
    assert r.status_code==403
    instance=await add(fleet)
    r=await fleet.client.post('/api/n8n_proxy/workflows/same/activate',headers=fleet.headers)
    assert r.status_code==422
    r=await fleet.client.get('/api/n8n_proxy/workflows',params={'instance_id':'missing'})
    assert r.status_code==404
    assert not fleet.calls


async def test_concurrent_instances_use_isolated_keys_and_preserve_pagination(fleet):
    alpha=await add(fleet)
    beta=await add(fleet,'Beta','https://beta.example','beta-secret-key')
    both=asyncio.Event()
    count=0

    async def answer(request):
        nonlocal count
        count+=1
        if count==2:
            both.set()
        await asyncio.wait_for(both.wait(),timeout=3)
        expected='alpha-secret-key' if request.url.host=='alpha.example' else 'beta-secret-key'
        assert request.headers['X-N8N-API-KEY']==expected
        assert request.url.params['cursor']=='opaque+/=' and request.url.params['limit']=='2'
        assert request.headers.get('cookie') is None
        return httpx.Response(200,json={'data':[{'id':'same','name':request.url.host}],'nextCursor':'second-page'})

    fleet.replies[('alpha.example','GET','/api/v1/workflows')]=answer
    fleet.replies[('beta.example','GET','/api/v1/workflows')]=answer
    results=await asyncio.gather(*(fleet.client.get('/api/n8n_proxy/workflows',params={'instance_id':i,'cursor':'opaque+/=','limit':2}) for i in [alpha,beta]))
    for r,instance in zip(results,[alpha,beta]):
        assert r.status_code==200,r.text
        assert r.json()['instance_id']==instance
        assert r.json()['next_cursor']=='second-page'
    assert results[0].json()['data'][0]['name']!=results[1].json()['data'][0]['name']


async def test_probe_records_only_read_capabilities_and_reports_partial_access(fleet):
    instance=await add(fleet)
    fleet.replies[('alpha.example','GET','/api/v1/executions')]=httpx.Response(403,json={'message':'alpha-secret-key forbidden'})
    r=await fleet.client.post(f'/api/n8n_proxy/instances/{instance}/probe',headers=fleet.headers)
    assert r.status_code==200,r.text
    assert r.json()['capabilities']=={'workflow_read':'supported','execution_read':'forbidden'}
    assert 'alpha-secret-key' not in r.text
    assert r.json()['reachable'] is True
    assert all(call.method=='GET' for call in fleet.calls)
    assert await fleet.app.state.db.fetchone('SELECT key FROM documents WHERE key=?',(f'instance_probe:{instance}',))
    # Every page reads the same saved check with the instance, not a per-browser copy.
    listed=(await fleet.client.get('/api/n8n_proxy/instances')).json()['data'][0]['last_probe']
    single=(await fleet.client.get(f'/api/n8n_proxy/instances/{instance}')).json()['last_probe']
    assert listed==single
    assert listed['reachable'] is True and listed['checked_at']==r.json()['checked_at']
    assert listed['capabilities']=={'workflow_read':'supported','execution_read':'forbidden'}


async def test_mutations_and_execution_details_target_exact_instance_and_are_audited(fleet):
    instance=await add(fleet,url='https://alpha.example/n8n')
    for action in ('activate','deactivate'):
        r=await fleet.client.post(f'/api/n8n_proxy/workflows/same/{action}',params={'instance_id':instance},headers=fleet.headers)
        assert r.status_code==200,r.text
        assert r.json()['instance_id']==instance
        assert fleet.calls[-1].url.path==f'/n8n/api/v1/workflows/same/{action}'
    detail=await fleet.client.get('/api/n8n_proxy/executions/same',params={'instance_id':instance})
    assert detail.status_code==200
    assert fleet.calls[-1].url.params['includeData']=='true'
    deleted=await fleet.client.delete('/api/n8n_proxy/executions/same',params={'instance_id':instance},headers=fleet.headers)
    assert deleted.status_code==200
    events=await fleet.app.state.db.fetchall("SELECT * FROM audit_events WHERE action LIKE 'fleet.%'")
    assert len([event for event in events if event['outcome']=='intent'])==3
    assert len([event for event in events if event['outcome']=='success' and event['action']!='fleet.instance_created'])>=3


async def test_unknown_mutation_outcome_is_not_retried(fleet):
    instance=await add(fleet)
    fleet.replies[('alpha.example','POST','/api/v1/workflows/same/activate')]=httpx.ReadTimeout('alpha-secret-key must never escape')
    r=await fleet.client.post('/api/n8n_proxy/workflows/same/activate',params={'instance_id':instance},headers=fleet.headers)
    assert r.status_code in (502,504),r.text
    assert r.json()['code']=='upstream_outcome_unknown'
    assert len(fleet.calls)==1
    assert 'alpha-secret-key' not in r.text
    events=await fleet.app.state.db.fetchall('SELECT outcome FROM audit_events ORDER BY id DESC LIMIT 1')
    assert events[0]['outcome']=='unknown'


async def test_viewer_can_read_but_cannot_modify_instances_or_workflows(fleet):
    instance=await add(fleet)
    await fleet.client.post('/api/admin/users',headers=fleet.headers,json={'username':'viewer','password':'test password of sufficient length','role':'viewer'})
    await fleet.client.post('/api/auth/logout',headers=fleet.headers)
    await fleet.client.post('/api/auth/login',json={'username':'viewer','password':'test password of sufficient length'})
    headers={'X-AGD-CSRF':fleet.client.cookies['agd_csrf']}
    assert (await fleet.client.get('/api/n8n_proxy/workflows',params={'instance_id':instance})).status_code==200
    before=len(fleet.calls)
    assert (await fleet.client.post('/api/n8n_proxy/workflows/same/activate',params={'instance_id':instance},headers=headers)).status_code==403
    assert (await fleet.client.delete(f'/api/n8n_proxy/instances/{instance}',headers=headers)).status_code==403
    assert len(fleet.calls)==before


async def test_a_viewer_never_reads_a_key_written_into_code(fleet):
    # Reported 2026-11-13: CodeLab's read returned unmasked code to viewers, while Workflows masked it.
    instance=await add(fleet)
    secret='sk-live-CODELAB-VIEWER-LEAK-0123456789'
    document={'id':'wf','name':'Order sync','active':False,'connections':{},'settings':{},'nodes':[
        {'id':'c1','name':'Code','type':'n8n-nodes-base.code','typeVersion':2,'position':[0,0],
         'parameters':{'jsCode':f'const apiKey = "{secret}";\nreturn items;'}}]}
    fleet.replies[('alpha.example','GET','/api/v1/workflows/wf')]=httpx.Response(200,json=document)
    fleet.replies[('alpha.example','GET','/api/v1/workflows')]=httpx.Response(200,json={'data':[document],'nextCursor':None})
    await fleet.client.post('/api/admin/users',headers=fleet.headers,json={'username':'reader','password':'test password of sufficient length','role':'viewer'})
    await fleet.client.post('/api/auth/logout',headers=fleet.headers)
    await fleet.client.post('/api/auth/login',json={'username':'reader','password':'test password of sufficient length'})
    before=len(fleet.calls)
    code=await fleet.client.get('/api/n8n_proxy/workflows/wf/nodes/c1',params={'instance_id':instance})
    assert code.status_code==403 and secret not in code.text
    assert len(fleet.calls)==before  # refused before n8n is asked
    for path in ('/api/n8n_proxy/workflows/wf','/api/n8n_proxy/workflows'):
        masked=await fleet.client.get(path,params={'instance_id':instance})
        assert masked.status_code==200 and secret not in masked.text


async def test_fleet_health_keeps_healthy_instances_when_one_fails(fleet):
    alpha=await add(fleet)
    beta=await add(fleet,'Beta','https://beta.example','beta-secret-key')
    for endpoint in ('workflows','executions'):
        fleet.replies[('beta.example','GET',f'/api/v1/{endpoint}')]=httpx.ConnectError('offline')
    r=await fleet.client.get('/api/n8n_proxy/health')
    assert r.status_code==200,r.text
    states={item['instance_id']:item['reachable'] for item in r.json()['data']}
    assert states=={alpha:True,beta:False}
    # The scan's checks are saved like a probe's, so the instance list agrees with Fleet health.
    listed=(await fleet.client.get('/api/n8n_proxy/instances')).json()
    assert {item['id']:item['last_probe']['reachable'] for item in listed['data']}=={alpha:True,beta:False}
    assert 'beta-secret-key' not in str(listed)


async def test_probe_does_not_save_results_for_changed_connection(fleet,monkeypatch):
    from backend.modules.n8n_proxy.client import N8nClient
    instance=await add(fleet)
    original=N8nClient.request

    async def change_after_read(self,identifier,method,path,**kwargs):
        result=await original(self,identifier,method,path,**kwargs)
        if path=='/executions':
            await fleet.app.state.db.execute('UPDATE instances SET url=? WHERE id=?',('https://changed.example',identifier))
        return result

    monkeypatch.setattr(N8nClient,'request',change_after_read)
    r=await fleet.client.post(f'/api/n8n_proxy/instances/{instance}/probe',headers=fleet.headers)
    assert r.status_code==409,r.text
    assert r.json()['code']=='instance_changed'
    assert await fleet.app.state.db.fetchone('SELECT * FROM documents WHERE key=?',(f'instance_probe:{instance}',)) is None


async def test_health_cancels_other_workers_after_session_failure(fleet,monkeypatch):
    from backend.modules.n8n_proxy import router as routes
    from backend.auth_gate import fail
    await add(fleet)
    await add(fleet,'Beta','https://beta.example')
    started=asyncio.Event()
    stopped=asyncio.Event()
    calls=0

    async def probe(request,identifier):
        nonlocal calls
        calls+=1
        if calls==1:
            await started.wait()
            fail(401,'session_revoked','Session revoked.')
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(routes,'probe_result',probe)
    r=await fleet.client.get('/api/n8n_proxy/health')
    assert r.status_code==401
    assert stopped.is_set()


async def test_cancelled_mutation_records_unknown_outcome(fleet,monkeypatch):
    from backend.modules.n8n_proxy.client import N8nClient
    instance=await add(fleet)

    async def cancel(*args,**kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(N8nClient,'request',cancel)
    with pytest.raises(asyncio.CancelledError):
        await fleet.client.post('/api/n8n_proxy/workflows/same/activate',params={'instance_id':instance},headers=fleet.headers)
    events=await fleet.app.state.db.fetchall("SELECT outcome FROM audit_events WHERE action='fleet.workflow_activated' ORDER BY id")
    assert [event['outcome'] for event in events]==['intent','unknown']


async def test_cancellation_during_final_audit_preserves_confirmed_success(fleet,monkeypatch):
    from backend.modules.n8n_proxy import router as routes
    instance=await add(fleet)
    original=routes.audit.record
    writing=asyncio.Event()
    release=asyncio.Event()

    async def delayed(request,action,target,details=None,outcome='success'):
        if action=='fleet.workflow_activated' and outcome=='success':
            writing.set()
            await release.wait()
        await original(request,action,target,details,outcome)

    monkeypatch.setattr(routes.audit,'record',delayed)
    task=asyncio.create_task(fleet.client.post('/api/n8n_proxy/workflows/same/activate',params={'instance_id':instance},headers=fleet.headers))
    await asyncio.wait_for(writing.wait(),3)
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    events=await fleet.app.state.db.fetchall("SELECT outcome FROM audit_events WHERE action='fleet.workflow_activated' ORDER BY id")
    assert [event['outcome'] for event in events]==['intent','success']


async def test_a_slow_fleet_scan_never_replaces_a_newer_check(fleet):
    alpha=await add(fleet)
    await add(fleet,'Beta','https://beta.example','beta-secret-key')
    release=asyncio.Event()
    alpha_down=False

    async def alpha_reply(request):
        if alpha_down:
            raise httpx.ConnectError('down')
        return httpx.Response(200,json={'data':[],'nextCursor':None})

    async def beta_reply(request):
        await release.wait()
        return httpx.Response(200,json={'data':[],'nextCursor':None})

    for path in ('/api/v1/workflows','/api/v1/executions'):
        fleet.replies[('alpha.example','GET',path)]=alpha_reply
        fleet.replies[('beta.example','GET',path)]=beta_reply
    scan=asyncio.create_task(fleet.client.get('/api/n8n_proxy/health'))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if sum(1 for call in fleet.calls if call.url.host=='alpha.example')>=2:
            break
    await asyncio.sleep(0.05)
    alpha_down=True
    probe=await fleet.client.post(f'/api/n8n_proxy/instances/{alpha}/probe',headers=fleet.headers)
    assert probe.status_code==200,probe.text
    release.set()
    assert (await scan).status_code==200
    saved=next(row for row in (await fleet.client.get('/api/n8n_proxy/instances')).json()['data'] if row['id']==alpha)['last_probe']
    assert saved['reachable'] is False and saved['checked_at']==probe.json()['checked_at']
