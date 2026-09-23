import asyncio
import copy

import httpx
import pytest

from test_fleet import fleet, add


@pytest.fixture
async def promotion(fleet):
    fleet.source=await add(fleet,'Source','https://source.example','source-api-key')
    fleet.target=await add(fleet,'Target','https://target.example','target-api-key')
    fleet.source_workflow={'id':'source-wf','name':'Example','active':False,'nodes':[{'id':'node','name':'Start','type':'n8n-nodes-base.manualTrigger','typeVersion':1,'position':[0,0],'parameters':{}}],'connections':{},'settings':{}}
    fleet.target_workflow=None
    fleet.writes=0

    async def source(request):
        return httpx.Response(200,json=copy.deepcopy(fleet.source_workflow))

    async def target(request):
        if fleet.target_workflow is None:
            return httpx.Response(404,json={})
        return httpx.Response(200,json=copy.deepcopy(fleet.target_workflow))

    async def write(request):
        import json
        fleet.writes+=1
        fleet.target_workflow={**json.loads(request.content),'id':'target-wf','active':False}
        return httpx.Response(200,json=copy.deepcopy(fleet.target_workflow))

    fleet.replies[('source.example','GET','/api/v1/workflows/source-wf')]=source
    fleet.replies[('target.example','GET','/api/v1/workflows/target-wf')]=target
    fleet.replies[('target.example','POST','/api/v1/workflows')]=write
    fleet.replies[('target.example','PUT','/api/v1/workflows/target-wf')]=write
    fleet.replies[('target.example','GET','/api/v1/credentials')]=httpx.Response(200,json={'data':[],'nextCursor':None})
    yield fleet


async def preview(p,**extra):
    r=await p.client.post('/api/n8n_promote/preflight',headers=p.headers,json={'source_instance_id':p.source,'target_instance_id':p.target,'workflow_id':'source-wf',**extra})
    assert r.status_code==200,r.text
    return r.json()


async def deploy(p,identifier,key='deploy-key'):
    return await p.client.post('/api/n8n_promote/deploy',headers=p.headers,json={'preview_id':identifier,'idempotency_key':key})


async def test_deploy_is_inactive_mapped_and_idempotent(promotion):
    p=promotion
    before=await preview(p)
    assert before['can_deploy'] is True
    results=await asyncio.gather(deploy(p,before['preview_id']),deploy(p,before['preview_id']))
    assert all(r.status_code==200 for r in results),[r.text for r in results]
    assert results[0].json()['id']==results[1].json()['id']
    assert results[0].json()['status']=='deployed'
    assert p.writes==1 and p.target_workflow['active'] is False
    rows=await p.app.state.db.fetchall('SELECT * FROM workflow_mappings')
    assert rows[0]['target_workflow_id']=='target-wf'
    again=await preview(p)
    updated=await deploy(p,again['preview_id'],'update-key')
    assert updated.json()['status']=='deployed',updated.text
    assert p.writes==2


async def test_changed_source_and_active_target_block_writes(promotion):
    p=promotion
    before=await preview(p)
    p.source_workflow['name']='Changed'
    r=await deploy(p,before['preview_id'])
    assert r.json()['status']=='rejected',r.text
    assert r.json()['error_code']=='stale_preview'
    assert p.writes==0
    fresh=await preview(p)
    assert (await deploy(p,fresh['preview_id'],'fresh-key')).json()['status']=='deployed'
    p.target_workflow['active']=True
    blocked=await preview(p)
    assert blocked['can_deploy'] is False


async def test_secret_provisioning_is_validated_and_journaled(promotion):
    p=promotion
    p.source_workflow['nodes'][0]['credentials']={'httpHeaderAuth':{'id':'source-cred','name':'Service'}}
    p.app.state.vault.set('SERVICE',{'value':'super-private-value','name':'Authorization'})
    p.replies[('target.example','GET','/api/v1/credentials/schema/httpHeaderAuth')]=httpx.Response(200,json={'type':'object','required':['value'],'properties':{'value':{'type':'string'}}})
    created=[]
    async def create(request):
        created.append(request)
        return httpx.Response(200,json={'id':'target-cred','name':'Service','type':'httpHeaderAuth','data':{'value':'super-private-value'}})
    p.replies[('target.example','POST','/api/v1/credentials')]=create
    before=await preview(p,secret_map={'source-cred':'$SERVICE'})
    assert before['can_deploy'] is True
    r=await deploy(p,before['preview_id'])
    assert r.json()['status']=='deployed',r.text
    assert len(created)==1
    assert p.target_workflow['nodes'][0]['credentials']['httpHeaderAuth']['id']=='target-cred'
    assert 'super-private-value' not in r.text
    saved=await p.app.state.db.fetchall('SELECT * FROM promotion_runs')
    assert 'super-private-value' not in str(saved)
    assert (await p.app.state.db.fetchall('SELECT * FROM credential_mappings'))[0]['target_credential_id']=='target-cred'


async def test_secret_rotation_after_preview_blocks_all_effects(promotion):
    p=promotion
    p.source_workflow['nodes'][0]['credentials']={'httpHeaderAuth':{'id':'cred','name':'Service'}}
    p.app.state.vault.set('SERVICE',{'value':'before'})
    p.replies[('target.example','GET','/api/v1/credentials/schema/httpHeaderAuth')]=httpx.Response(200,json={'type':'object'})
    before=await preview(p,secret_map={'cred':'$SERVICE'})
    p.app.state.vault.set('SERVICE',{'value':'after'})
    r=await deploy(p,before['preview_id'])
    assert r.json()['status']=='rejected',r.text
    assert r.json()['error_code']=='stale_preview'
    assert not [call for call in p.calls if call.method in ('POST','PUT')]


async def test_activation_timeout_reconciles_without_second_activation(promotion):
    p=promotion
    before=await preview(p)
    run=(await deploy(p,before['preview_id'])).json()
    count=0
    async def activate(request):
        nonlocal count
        count+=1
        p.target_workflow['active']=True
        raise httpx.ReadTimeout('response lost')
    p.replies[('target.example','POST','/api/v1/workflows/target-wf/activate')]=activate
    r=await p.client.post(f"/api/n8n_promote/runs/{run['id']}/activate",headers=p.headers)
    assert r.status_code==504
    reconciled=await p.client.post(f"/api/n8n_promote/runs/{run['id']}/reconcile",headers=p.headers,json={})
    assert reconciled.json()['status']=='activated',reconciled.text
    assert count==1


async def test_definitely_rejected_activation_keeps_deployment_available(promotion):
    p=promotion
    before=await preview(p)
    run=(await deploy(p,before['preview_id'])).json()
    p.replies[('target.example','POST','/api/v1/workflows/target-wf/activate')]=httpx.Response(400,json={'message':'Invalid binding'})
    r=await p.client.post(f"/api/n8n_promote/runs/{run['id']}/activate",headers=p.headers)
    assert r.status_code==400
    saved=await p.client.get(f"/api/n8n_promote/runs/{run['id']}")
    assert saved.json()['status']=='deployed'
    assert saved.json()['error_code']=='upstream_rejected'
    # The run names its source so a workflow's page can list the promotions made from it.
    listed=(await p.client.get('/api/n8n_promote/runs')).json()['data']
    assert (listed[0]['source_instance_id'],listed[0]['workflow_id'])==(p.source,'source-wf')
    assert listed[0]['created_at']


async def test_restore_previous_version_checks_target_snapshot(promotion):
    p=promotion
    first=await preview(p)
    await deploy(p,first['preview_id'])
    p.source_workflow['name']='New version'
    second=await preview(p)
    run=(await deploy(p,second['preview_id'],'second')).json()
    restored=await p.client.post(f"/api/n8n_promote/runs/{run['id']}/restore",headers=p.headers)
    assert restored.json()['status']=='restored',restored.text
    assert p.target_workflow['name']=='Example'
    events=await p.app.state.db.fetchall("SELECT outcome FROM audit_events WHERE action='promotion.restored'")
    assert events==[{'outcome':'restored'}]


async def test_partial_credential_failure_preserves_residuals_without_retry(promotion):
    p=promotion
    p.source_workflow['nodes'][0]['credentials']={'firstAuth':{'id':'first','name':'First'},'secondAuth':{'id':'second','name':'Second'}}
    p.app.state.vault.set('FIRST',{'value':'first-secret'})
    p.app.state.vault.set('SECOND',{'value':'second-secret'})
    for kind in ('firstAuth','secondAuth'):
        p.replies[('target.example','GET',f'/api/v1/credentials/schema/{kind}')]=httpx.Response(200,json={'type':'object'})
    calls=[]
    async def create(request):
        import json
        data=json.loads(request.content)
        calls.append(data['type'])
        if data['type']=='secondAuth':
            return httpx.Response(400,json={'message':'second-secret rejected'})
        return httpx.Response(200,json={'id':'first-target','name':'First','type':'firstAuth'})
    p.replies[('target.example','POST','/api/v1/credentials')]=create
    before=await preview(p,secret_map={'first':'$FIRST','second':'$SECOND'})
    r=await deploy(p,before['preview_id'])
    assert r.json()['status']=='partial',r.text
    assert r.json()['created_credentials'][0]['id']=='first-target'
    assert p.writes==0
    assert (await deploy(p,before['preview_id'])).json()['status']=='partial'
    assert calls==['firstAuth','secondAuth']
    assert 'second-secret' not in r.text


async def test_application_restart_marks_interrupted_runs_uncertain(promotion):
    from backend.main import create_app
    p=promotion
    before=await preview(p)
    result=(await deploy(p,before['preview_id'])).json()
    await p.app.state.db.execute("UPDATE promotion_runs SET status='writing' WHERE id=?",(result['id'],))
    restarted=create_app(data_dir=p.app.state.settings.data_dir)
    async with restarted.router.lifespan_context(restarted):
        row=await restarted.state.db.fetchone('SELECT * FROM promotion_runs WHERE id=?',(result['id'],))
        assert row['status']=='uncertain'
    assert p.writes==1


async def test_cancelled_workflow_write_is_durable_and_never_retried(promotion):
    p=promotion
    accepted=asyncio.Event()
    original=p.replies[('target.example','POST','/api/v1/workflows')]
    async def stall(request):
        await original(request)
        accepted.set()
        await asyncio.Event().wait()
    p.replies[('target.example','POST','/api/v1/workflows')]=stall
    before=await preview(p)
    task=asyncio.create_task(deploy(p,before['preview_id']))
    await asyncio.wait_for(accepted.wait(),3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    result=await deploy(p,before['preview_id'])
    assert result.json()['status']=='uncertain',result.text
    assert p.writes==1


async def test_unknown_create_is_not_retried_and_can_be_reconciled(promotion):
    p=promotion
    original=p.replies[('target.example','POST','/api/v1/workflows')]
    async def timeout(request):
        await original(request)
        raise httpx.ReadTimeout('write accepted but response lost')
    p.replies[('target.example','POST','/api/v1/workflows')]=timeout
    before=await preview(p)
    r=await deploy(p,before['preview_id'])
    assert r.json()['status']=='uncertain',r.text
    assert p.writes==1
    assert (await deploy(p,before['preview_id'])).json()['status']=='uncertain'
    assert p.writes==1
    checked=await p.client.post(f"/api/n8n_promote/runs/{r.json()['id']}/reconcile",headers=p.headers,json={'target_workflow_id':'target-wf'})
    assert checked.json()['status']=='deployed',checked.text
    assert p.writes==1


async def test_preview_hides_inline_workflow_data_and_is_actor_bound(promotion):
    p=promotion
    p.source_workflow['nodes'][0]['parameters']={'literal':'inline-sensitive-value'}
    before=await preview(p)
    assert 'inline-sensitive-value' not in str(before)
    saved=await p.app.state.db.fetchone('SELECT value_json FROM documents WHERE key=?',('promotion_preview:'+before['preview_id'],))
    assert 'inline-sensitive-value' not in saved['value_json']
    await p.client.post('/api/admin/users',headers=p.headers,json={'username':'other','password':'another sufficiently long password','role':'operator'})
    await p.client.post('/api/auth/logout',headers=p.headers)
    await p.client.post('/api/auth/login',json={'username':'other','password':'another sufficiently long password'})
    p.headers={'X-AGD-CSRF':p.client.cookies['agd_csrf']}
    r=await deploy(p,before['preview_id'])
    assert r.status_code==403
    assert p.writes==0
