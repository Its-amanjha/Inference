from datetime import datetime, timedelta, timezone
import httpx
from test_fleet import fleet, add


async def test_dashboard_scopes_window_and_excludes_running_from_rate(fleet):
    instance = await add(fleet)
    await add(fleet,name='Other',url='https://other.example')
    now = datetime.now(timezone.utc)
    rows = [{'id': str(i), 'status': status, 'startedAt': now.isoformat()}
            for i, status in enumerate(['success', 'error', 'running'])]
    rows.append({'id':'old','status':'success','startedAt':(now-timedelta(days=2)).isoformat()})
    fleet.replies[('alpha.example','GET','/api/v1/executions')] = httpx.Response(200,json={'data':rows})
    fleet.replies[('alpha.example','GET','/api/v1/workflows')] = httpx.Response(200,json={'data':[{'id':'one'}]})
    result = await fleet.client.get('/api/n8n_proxy/dashboard',params={'instance_id':instance})
    assert result.status_code == 200, result.text
    data = result.json()
    assert data['workflows'] == 1 and data['executions_24h'] == 3
    assert data['success_rate_percent'] == 50
    assert data['terminal_executions_24h'] == 2
    assert {row['instance_id'] for row in data['data']} == {instance}


async def test_dashboard_partial_failure_never_reports_complete_totals(fleet):
    await add(fleet)
    await add(fleet,name='Beta',url='https://beta.example')
    fleet.replies[('beta.example','GET','/api/v1/executions')] = httpx.ConnectError('private details')
    result = await fleet.client.get('/api/n8n_proxy/dashboard')
    assert result.status_code == 200
    data = result.json()
    assert data['executions_24h'] is None and data['success_rate_percent'] is None
    assert data['workflows'] == 0
    assert 'private details' not in result.text


async def test_dashboard_repeated_cursor_and_invalid_dates_are_incomplete(fleet):
    await add(fleet)
    fleet.replies[('alpha.example','GET','/api/v1/workflows')] = httpx.Response(200,json={'data':[{'id':'one'}],'nextCursor':'repeat'})
    fleet.replies[('alpha.example','GET','/api/v1/executions')] = httpx.Response(200,json={'data':[{'id':'bad','status':'success','startedAt':'invalid'}]})
    data = (await fleet.client.get('/api/n8n_proxy/dashboard')).json()
    assert data['workflows'] is None and data['executions_24h'] is None
    assert len([call for call in fleet.calls if call.url.path.endswith('/workflows')]) <= 2


async def test_dashboard_caps_scan_and_unknown_status_does_not_invent_rate(fleet):
    await add(fleet)
    async def pages(request):
        page = int(request.url.params.get('cursor', '0'))
        return httpx.Response(200,json={'data':[{'id':str(page)}],'nextCursor':str(page+1)})
    fleet.replies[('alpha.example','GET','/api/v1/workflows')] = pages
    fleet.replies[('alpha.example','GET','/api/v1/executions')] = httpx.Response(200,json={'data':[{'id':'one','startedAt':datetime.now(timezone.utc).isoformat(),'status':'unsupported'}]})
    data = (await fleet.client.get('/api/n8n_proxy/dashboard')).json()
    assert data['workflows'] is None and data['success_rate_percent'] is None
    assert len([call for call in fleet.calls if call.url.path.endswith('/workflows')]) == 10


async def test_dashboard_empty_fleet_and_missing_instance(fleet):
    data = (await fleet.client.get('/api/n8n_proxy/dashboard')).json()
    assert data['workflows'] == data['executions_24h'] == 0
    assert data['success_rate_percent'] is None
    assert (await fleet.client.get('/api/n8n_proxy/dashboard?instance_id=missing')).status_code == 404


async def test_dashboard_silent_count_uses_confirmed_recent_scoped_spans(fleet):
    instance = await add(fleet)
    other = await add(fleet,name='Other',url='https://other.example')
    now = int(datetime.now(timezone.utc).timestamp()*1_000_000_000)
    for index, (target, stamp, health, silent) in enumerate([
        (instance,now,'error',1), (instance,now,'low',1),
        (instance,now-3*86400*1_000_000_000,'error',1),
        (other,now,'error',1), (instance,now,'error',0)]):
        await fleet.app.state.db.execute('INSERT INTO otel_spans(instance_id,trace_id,span_id,start_ns,health_status,silent) VALUES(?,?,?,?,?,?)',
            (target,'trace',str(index),stamp,health,silent))
    data = (await fleet.client.get('/api/n8n_proxy/dashboard',params={'instance_id':instance})).json()
    assert data['confirmed_silent_error_spans_24h'] == 1


async def test_dashboard_counts_active_workflows_per_instance(fleet):
    instance = await add(fleet)
    fleet.replies[('alpha.example','GET','/api/v1/executions')] = httpx.Response(200,json={'data':[]})
    fleet.replies[('alpha.example','GET','/api/v1/workflows')] = httpx.Response(200,json={'data':[
        {'id':'one','active':True},{'id':'two','active':False},{'id':'three'},{'id':'four','active':'true'}]})
    row = (await fleet.client.get('/api/n8n_proxy/dashboard',params={'instance_id':instance})).json()['data'][0]
    # Only a real boolean true counts; a missing or string flag is not proof of activation.
    assert row['observed_workflows'] == 4 and row['active_workflows'] == 1
