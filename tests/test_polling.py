import asyncio
import json
from datetime import datetime,timedelta,timezone

import httpx
import pytest

from test_fleet import fleet
from test_observability import issue,send,batch


async def enable(fleet,instance,**values):
    response=await fleet.client.put('/api/otel/polling/'+instance,headers=fleet.headers,json={'enabled':True,'interval_seconds':30,**values})
    assert response.status_code==200,response.text
    return response.json()


async def test_polling_survives_logout_and_caches_terminal_execution(fleet):
    from backend.modules.observability.polling import tick
    instance,token=await issue(fleet)
    attrs=[{'key':'n8n.execution.id','value':{'stringValue':'execution'}}]
    await send(fleet,token['token'],batch(attributes=attrs))
    fleet.replies[('alpha.example','GET','/api/v1/executions')]=httpx.Response(200,json={'data':[{'id':'execution','status':'success'}],'nextCursor':None})
    fleet.replies[('alpha.example','GET','/api/v1/executions/execution')]=httpx.Response(200,json={'status':'success','stoppedAt':(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat(),'data':{'resultData':{'runData':{}}}})
    await enable(fleet,instance,zero_output_nodes={'workflow':['node']})
    await fleet.client.post('/api/auth/logout',headers=fleet.headers)
    await tick(fleet.app,instance,force=True)
    await tick(fleet.app,instance,force=True)
    assert len([call for call in fleet.calls if call.url.path.endswith('/executions/execution')])==1
    assert (await fleet.app.state.db.fetchone('SELECT checked_at FROM otel_spans'))['checked_at']
    saved=await fleet.app.state.db.fetchone('SELECT value_json FROM documents WHERE key=?',('telemetry_polling:'+instance,))
    assert json.loads(saved['value_json'])['zero_output_nodes']=={'workflow':['node']}


async def test_disabled_or_demoted_grant_cannot_send_requests(fleet):
    from backend.modules.observability.polling import tick
    instance,token=await issue(fleet)
    await enable(fleet,instance)
    await fleet.app.state.db.execute("UPDATE users SET role='viewer' WHERE username='owner'")
    await tick(fleet.app,instance,force=True)
    assert not fleet.calls
    await fleet.app.state.db.execute("UPDATE users SET role='admin' WHERE username='owner'")
    r=await fleet.client.put('/api/otel/polling/'+instance,headers=fleet.headers,json={'enabled':False})
    assert r.status_code==200
    await tick(fleet.app,instance,force=True)
    assert not fleet.calls


async def test_disabling_during_read_prevents_classification_commit(fleet):
    from backend.modules.observability.polling import tick
    instance,token=await issue(fleet)
    await send(fleet,token['token'],batch(attributes=[{'key':'n8n.execution.id','value':{'stringValue':'execution'}}]))
    fleet.replies[('alpha.example','GET','/api/v1/executions')]=httpx.Response(200,json={'data':[],'nextCursor':None})
    entered=asyncio.Event()
    release=asyncio.Event()
    async def read(request):
        entered.set()
        await release.wait()
        return httpx.Response(200,json={'status':'success','stoppedAt':'2026-01-01T00:00:00+00:00','data':{'resultData':{'runData':{}}}})
    fleet.replies[('alpha.example','GET','/api/v1/executions/execution')]=read
    await enable(fleet,instance)
    running=asyncio.create_task(tick(fleet.app,instance,force=True))
    await asyncio.wait_for(entered.wait(),3)
    try:
        assert (await fleet.client.put('/api/otel/polling/'+instance,headers=fleet.headers,json={'enabled':False})).status_code==200
    finally:
        release.set()
        await running
    assert (await fleet.app.state.db.fetchone('SELECT checked_at FROM otel_spans'))['checked_at'] is None


async def test_poller_reclassifies_when_arrival_grace_expires(fleet,monkeypatch):
    from backend.modules.observability.polling import tick
    import time
    instance,token=await issue(fleet)
    attrs=[{'key':key,'value':{'stringValue':value}} for key,value in [('n8n.execution.id','execution'),('n8n.node.id','node'),('n8n.node.name','Node')]]
    await send(fleet,token['token'],batch(attributes=attrs))
    fleet.replies[('alpha.example','GET','/api/v1/executions')]=httpx.Response(200,json={'data':[],'nextCursor':None})
    fleet.replies[('alpha.example','GET','/api/v1/executions/execution')]=httpx.Response(200,json={'status':'success','stoppedAt':datetime.now(timezone.utc).isoformat(),'data':{'resultData':{'runData':{'Node':[{'error':{'message':'private'}}]}}}})
    await enable(fleet,instance)
    await tick(fleet.app,instance,force=True)
    assert (await fleet.app.state.db.fetchone('SELECT health_status FROM otel_spans'))['health_status']=='ok'
    now=time.time_ns()
    monkeypatch.setattr(time,'time_ns',lambda:now+40*10**9)
    await tick(fleet.app,instance,force=True)
    assert (await fleet.app.state.db.fetchone('SELECT health_status FROM otel_spans'))['health_status']=='error'
    assert len([call for call in fleet.calls if call.url.path.endswith('/executions/execution')])==1


async def test_missing_execution_does_not_block_other_classification(fleet):
    from backend.modules.observability.polling import tick
    instance,token=await issue(fleet)
    for i,identifier in enumerate(('deleted','healthy')):
        await send(fleet,token['token'],batch(trace=f'{i+1:032x}',attributes=[{'key':'n8n.execution.id','value':{'stringValue':identifier}}]))
    fleet.replies[('alpha.example','GET','/api/v1/executions')]=httpx.Response(200,json={'data':[],'nextCursor':None})
    fleet.replies[('alpha.example','GET','/api/v1/executions/deleted')]=httpx.Response(404,json={})
    fleet.replies[('alpha.example','GET','/api/v1/executions/healthy')]=httpx.Response(200,json={'status':'success','stoppedAt':'2026-01-01T00:00:00Z'})
    await enable(fleet,instance)
    result=await tick(fleet.app,instance,force=True)
    assert result['classified_executions']==1
    assert result['execution_errors']==[{'execution_id':'deleted','code':'upstream_not_found'}]
    await tick(fleet.app,instance,force=True)
    assert len([call for call in fleet.calls if call.url.path.endswith('/deleted')])==1


async def test_completed_history_cannot_hide_pending_execution(fleet):
    from backend.modules.observability.polling import tick
    instance,token=await issue(fleet)
    await enable(fleet,instance)
    config=json.loads((await fleet.app.state.db.fetchone('SELECT value_json FROM documents WHERE key=?',('telemetry_polling:'+instance,)))['value_json'])
    async with fleet.app.state.db.transaction() as tx:
        await tx.execute("WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n<2001) INSERT INTO otel_spans(instance_id,trace_id,span_id,execution_id) SELECT ?,printf('%032x',n),'bbbbbbbbbbbbbbbb','done' || n FROM seq",(instance,))
        await tx.execute("INSERT INTO execution_cursors(key,value_json) SELECT instance_id || ':' || execution_id,json_object('generation',?,'grace_complete',1,'last_span_id',id,'checked_at_epoch',1) FROM otel_spans WHERE instance_id=?",(config['version'],instance))
    await send(fleet,token['token'],batch(trace='f'*32,attributes=[{'key':'n8n.execution.id','value':{'stringValue':'pending'}}]))
    await fleet.app.state.db.execute('INSERT INTO execution_cursors(key,value_json) VALUES(?,?)',(instance+':pending',json.dumps({'checked_at_epoch':2})))
    fleet.replies[('alpha.example','GET','/api/v1/executions')]=httpx.Response(200,json={'data':[],'nextCursor':None})
    fleet.replies[('alpha.example','GET','/api/v1/executions/pending')]=httpx.Response(200,json={'status':'success','stoppedAt':'2026-01-01T00:00:00Z'})
    result=await tick(fleet.app,instance,force=True)
    assert result['classified_executions']==1,result
    assert len(fleet.calls)==2


async def test_polling_client_cannot_mutate_or_switch_instances(fleet):
    from backend.modules.observability.polling import PollingClient
    from starlette.requests import Request
    from fastapi import HTTPException
    instance,_=await issue(fleet)
    await enable(fleet,instance)
    config=json.loads((await fleet.app.state.db.fetchone('SELECT value_json FROM documents WHERE key=?',('telemetry_polling:'+instance,)))['value_json'])
    request=Request({'type':'http','method':'GET','path':'/internal','headers':[],'app':fleet.app})
    client=PollingClient(request,instance,config['version'])
    for target,method,path in ((instance,'POST','/workflows'),(instance,'GET','/credentials'),('other','GET','/executions')):
        with pytest.raises(HTTPException) as error:
            await client.request(target,method,path)
        assert error.value.status_code==403
    assert not fleet.calls
