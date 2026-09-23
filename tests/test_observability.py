import gzip
import json
import time
from decimal import Decimal

import httpx
import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest, ExportTraceServiceResponse

from test_fleet import fleet, add


def batch(trace='a'*32,span='b'*16,status=1,attributes=None):
    now=time.time_ns()
    return {'resourceSpans':[{'resource':{'attributes':[{'key':'instance_id','value':{'stringValue':'forged'}}]},'scopeSpans':[{'spans':[{'traceId':trace,'spanId':span,'name':'Node','startTimeUnixNano':str(now-1000),'endTimeUnixNano':str(now),'status':{'code':status},'attributes':attributes or []}]}]}]}


async def issue(fleet,instance=None):
    instance=instance or await add(fleet)
    r=await fleet.client.post('/api/otel/tokens',headers=fleet.headers,json={'instance_id':instance,'name':'Exporter'})
    assert r.status_code==200,r.text
    return instance,r.json()


async def send(fleet,token,payload):
    return await fleet.client.post('/api/otel/v1/traces',headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'},content=json.dumps(payload))


async def test_machine_token_assigns_instance_and_deduplicates(fleet):
    instance,token=await issue(fleet)
    payload=batch(attributes=[{'key':'gen_ai.prompt','value':{'stringValue':'secret prompt'}}])
    for _ in range(2):
        r=await send(fleet,token['token'],payload)
        assert r.status_code==200,r.text
    rows=await fleet.app.state.db.fetchall('SELECT * FROM otel_spans')
    assert len(rows)==1 and rows[0]['instance_id']==instance
    assert 'secret prompt' not in str(rows)
    assert 'forged' not in rows[0]['instance_id']
    listed=await fleet.client.get('/api/otel/tokens',params={'instance_id':instance})
    assert token['token'] not in listed.text and 'token_hash' not in listed.text
    saved=await fleet.app.state.db.fetchone('SELECT token_hash FROM telemetry_tokens')
    assert saved['token_hash']!=token['token']


async def test_otlp_requires_machine_auth_even_with_session_and_revocation(fleet):
    instance,token=await issue(fleet)
    assert (await fleet.client.post('/api/otel/v1/traces',json=batch())).status_code==401
    assert (await fleet.client.get('/api/otel/v1/traces')).status_code in (404,405)
    revoked=await fleet.client.delete('/api/otel/tokens/'+token['id'],headers=fleet.headers)
    assert revoked.status_code==200
    assert (await send(fleet,token['token'],batch())).status_code==401
    assert await fleet.app.state.db.fetchall('SELECT * FROM otel_spans')==[]


async def test_protobuf_gzip_and_partial_rejection(fleet):
    instance,token=await issue(fleet)
    proto=ExportTraceServiceRequest()
    span=proto.resource_spans.add().scope_spans.add().spans.add()
    span.trace_id=bytes.fromhex('c'*32)
    span.span_id=bytes.fromhex('d'*16)
    span.name='Fixture'
    span.start_time_unix_nano=100
    span.end_time_unix_nano=200
    r=await fleet.client.post('/api/otel/v1/traces',headers={'Authorization':'Bearer '+token['token'],'Content-Type':'application/x-protobuf','Content-Encoding':'gzip'},content=gzip.compress(proto.SerializeToString()))
    assert r.status_code==200,r.text
    assert r.headers['content-type']=='application/x-protobuf'
    response=ExportTraceServiceResponse.FromString(r.content)
    assert response.partial_success.rejected_spans==0
    invalid=batch(span='0'*16)
    r=await send(fleet,token['token'],invalid)
    assert r.status_code==200,r.text
    assert r.json()['partialSuccess']['rejectedSpans']=='1'


async def test_trace_queries_keep_instances_separate_and_unknown_cost_null(fleet):
    first,one=await issue(fleet)
    second=await add(fleet,'Other','https://other.example')
    _,two=await issue(fleet,second)
    # An AI step (it names a model) with no price: the trace's AI cost is unknown, never zero.
    unpriced=[{'key':'gen_ai.request.model','value':{'stringValue':'unpriced-model'}}]
    for token in (one,two):
        assert (await send(fleet,token['token'],batch(attributes=unpriced))).status_code==200
    traces=await fleet.client.get('/api/otel/traces')
    assert len(traces.json()['data'])==2
    assert traces.json()['data'][0]['workflow_name'] is None
    detail=await fleet.client.get('/api/otel/traces/'+'a'*32,params={'instance_id':first})
    assert len(detail.json()['data'])==1
    assert detail.json()['cost_usd'] is None
    assert detail.json()['unknown_cost_spans']==1
    assert (await fleet.client.get('/api/otel/traces/'+'a'*32)).status_code==422


async def test_trace_list_names_the_workflow_and_filters_by_it(fleet):
    instance,token=await issue(fleet)
    named=[{'key':'n8n.workflow.id','value':{'stringValue':'wf-7'}},{'key':'n8n.workflow.name','value':{'stringValue':'Order sync'}},
           {'key':'n8n.execution.id','value':{'stringValue':'4821'}}]
    assert (await send(fleet,token['token'],batch(trace='c'*32,attributes=named))).status_code==200
    assert (await send(fleet,token['token'],batch(trace='d'*32))).status_code==200
    rows=(await fleet.client.get('/api/otel/traces',params={'instance_id':instance})).json()['data']
    labelled=next(row for row in rows if row['trace_id']=='c'*32)
    assert (labelled['workflow_id'],labelled['workflow_name'],labelled['execution_id'])==('wf-7','Order sync','4821')
    only=(await fleet.client.get('/api/otel/traces',params={'instance_id':instance,'workflow_id':'wf-7'})).json()['data']
    assert [row['trace_id'] for row in only]==['c'*32]
    # A step without the workflow attribute still belongs to its trace when filtering by workflow.
    assert (await send(fleet,token['token'],batch(trace='c'*32,span='e'*16))).status_code==200
    only=(await fleet.client.get('/api/otel/traces',params={'instance_id':instance,'workflow_id':'wf-7'})).json()['data']
    assert only[0]['span_count']==2 and only[0]['workflow_name']=='Order sync'
    last=(await fleet.client.get('/api/otel/last-span',params={'instance_id':instance})).json()
    starts=[int(row['start_ns']) for row in (await fleet.client.get('/api/otel/traces',params={'instance_id':instance})).json()['data']]
    assert last['start_ns'] is not None and int(last['start_ns'])>=max(starts)
    assert (await fleet.client.get('/api/otel/last-span',params={'instance_id':'missing'})).json()['start_ns'] is None


async def test_confirmed_span_errors_are_triaged_once(fleet):
    instance,token=await issue(fleet)
    payload=batch(status=2)
    await send(fleet,token['token'],payload)
    await send(fleet,token['token'],payload)
    errors=await fleet.client.get('/api/errors',params={'instance_id':instance})
    assert errors.status_code==200,errors.text
    assert len(errors.json()['data'])==1
    identifier=errors.json()['data'][0]['id']
    resolved=await fleet.client.post(f'/api/errors/{identifier}/resolve',headers=fleet.headers)
    assert resolved.status_code==200
    assert (await fleet.client.get('/api/errors',params={'status':'open'})).json()['data']==[]
    assert (await fleet.client.get('/api/errors',params={'status':'resolved','instance_id':instance,'workflow_id':'no-such-workflow'})).json()['data']==[]
    reopened=await fleet.client.post(f'/api/errors/{identifier}/reopen',headers=fleet.headers)
    assert reopened.status_code==200 and reopened.json()['status']=='open'
    assert [row['id'] for row in (await fleet.client.get('/api/errors',params={'status':'open'})).json()['data']]==[identifier]
    assert (await fleet.client.post('/api/errors/999999/reopen',headers=fleet.headers)).status_code==404


async def test_revocation_during_parse_prevents_insert(fleet,monkeypatch):
    from backend.modules.observability import router
    instance,token=await issue(fleet)
    # Revoke precisely after body read, before parser result is committed.
    original_read=router.read_body
    async def body_then_revoke(request):
        body=await original_read(request)
        await fleet.app.state.db.execute('UPDATE telemetry_tokens SET revoked_at=CURRENT_TIMESTAMP WHERE id=?',(token['id'],))
        return body
    monkeypatch.setattr(router,'read_body',body_then_revoke)
    r=await send(fleet,token['token'],batch())
    assert r.status_code==401
    assert await fleet.app.state.db.fetchall('SELECT * FROM otel_spans')==[]


async def test_classification_preserves_trace_identity_and_counts_continuation_errors(fleet):
    from datetime import datetime,timezone,timedelta
    instance,token=await issue(fleet)
    attrs=[{'key':'n8n.execution.id','value':{'stringValue':'execution'}},{'key':'n8n.node.id','value':{'stringValue':'node'}},{'key':'n8n.node.name','value':{'stringValue':'Node'}}]
    for trace in ('a'*32,'c'*32):
        await send(fleet,token['token'],batch(trace=trace,attributes=attrs))
    fleet.replies[('alpha.example','GET','/api/v1/executions/execution')]=httpx.Response(200,json={'id':'execution','status':'success','stoppedAt':(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat(),'data':{'resultData':{'runData':{'Node':[{'error':{'message':'private failure payload'}}]}}}})
    for _ in range(2):
        r=await fleet.client.post('/api/otel/classify',headers=fleet.headers,json={'instance_id':instance,'execution_id':'execution'})
        assert r.status_code==200,r.text
    rows=await fleet.app.state.db.fetchall('SELECT checked_at,health_status FROM otel_spans')
    assert len(rows)==2 and all(row['checked_at'] and row['health_status']=='error' for row in rows)
    assert len(fleet.calls)==1
    assert (await fleet.client.get('/api/otel/summary')).json()['error_spans']==2
    traces=(await fleet.client.get('/api/otel/traces')).json()['data']
    assert all(row['error_spans']==1 for row in traces)
    assert 'private failure payload' not in str(await fleet.app.state.db.fetchall('SELECT * FROM execution_cursors'))


async def test_a_failed_run_counts_its_failed_step_once(fleet):
    """n8n marks the failing node and the workflow's root span as errors; the run shows one failed step."""
    instance,token=await issue(fleet)
    node=[{'key':'n8n.node.name','value':{'stringValue':'HTTP Request'}}]
    for payload in (batch(trace='e'*32,span='1'*16,status=2),batch(trace='e'*32,span='2'*16,status=2,attributes=node),
                    batch(trace='f'*32,span='3'*16,status=2)):
        assert (await send(fleet,token['token'],payload)).status_code==200
    traces={row['trace_id']:row['error_spans'] for row in (await fleet.client.get('/api/otel/traces')).json()['data']}
    # The node's failure stands for its run; a run that failed outside any node still counts once.
    assert traces=={'e'*32:1,'f'*32:1}
    assert (await fleet.client.get('/api/otel/summary')).json()['error_spans']==2
    assert (await fleet.client.get('/api/otel/summary',params={'instance_id':instance})).json()['error_spans']==2


async def test_trace_cost_aggregate_preserves_all_stored_decimal_digits(fleet):
    instance,token=await issue(fleet)
    await send(fleet,token['token'],batch(attributes=[{'key':'gen_ai.request.model','value':{'stringValue':'priced-model'}}]))
    exact='0.123456789012345678901234567890123456789'
    await fleet.app.state.db.execute('UPDATE otel_spans SET cost_exact=?',(exact,))
    r=await fleet.client.get('/api/otel/traces/'+'a'*32,params={'instance_id':instance})
    assert r.json()['cost_usd']==exact


async def test_retention_uses_received_time_and_validates_configuration(fleet):
    instance,token=await issue(fleet)
    await send(fleet,token['token'],batch())
    await fleet.app.state.db.execute("UPDATE otel_spans SET received_at=datetime('now','-2 days'),start_ns=9000000000000000000,end_ns=9000000000000000001")
    r=await fleet.client.put('/api/otel/retention',headers=fleet.headers,json={'days':1})
    assert r.status_code==200,r.text
    assert await fleet.app.state.db.fetchall('SELECT * FROM otel_spans')==[]
    assert (await fleet.client.put('/api/otel/retention',headers=fleet.headers,json={'days':0})).status_code==422


async def test_machine_exemption_does_not_remove_admin_csrf_and_bounds_concurrency(fleet):
    instance,token=await issue(fleet)
    assert (await fleet.client.post('/api/otel/tokens',json={'instance_id':instance,'name':'Missing csrf'})).status_code==403
    for _ in range(4):
        await fleet.app.state.ingest_slots.acquire()
    try:
        assert (await send(fleet,token['token'],batch())).status_code==503
    finally:
        for _ in range(4):
            fleet.app.state.ingest_slots.release()
    assert await fleet.app.state.db.fetchall('SELECT * FROM otel_spans')==[]


async def test_zero_output_opt_in_uses_persisted_successful_history(fleet):
    from datetime import datetime,timezone,timedelta
    instance,token=await issue(fleet)
    for i in range(11):
        execution='run'+str(i)
        attrs=[{'key':key,'value':{'stringValue':value}} for key,value in [('n8n.execution.id',execution),('n8n.workflow.id','workflow'),('n8n.node.id','node'),('n8n.node.name','Node')]]
        attrs.extend([{'key':'n8n.node.input_items','value':{'intValue':'1'}},{'key':'n8n.node.output_items','value':{'intValue':'0' if i==10 else '1'}}])
        await send(fleet,token['token'],batch(trace=f'{i+1:032x}',attributes=attrs))
        fleet.replies[('alpha.example','GET',f'/api/v1/executions/{execution}')]=httpx.Response(200,json={'status':'success','stoppedAt':(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat(),'data':{'resultData':{'runData':{'Node':[{'executionStatus':'success'}]}}}})
        r=await fleet.client.post('/api/otel/classify',headers=fleet.headers,json={'instance_id':instance,'execution_id':execution,'zero_output_nodes':['node']})
        assert r.status_code==200,r.text
        assert r.json()['data'][0]['health_status']==('low' if i==10 else 'ok')


async def test_ingested_cost_uses_exact_model_override_and_preserves_provenance(fleet):
    instance,token=await issue(fleet)
    changed=await fleet.client.put('/api/otel/prices/vendor/model-v1',headers=fleet.headers,json={'input_per_million':'2','output_per_million':'8'})
    assert changed.status_code==200,changed.text
    attrs=[{'key':'gen_ai.response.model','value':{'stringValue':'vendor/model-v1'}},{'key':'gen_ai.usage.input_tokens','value':{'intValue':'1000'}},{'key':'gen_ai.usage.output_tokens','value':{'intValue':'250'}}]
    r=await send(fleet,token['token'],batch(attributes=attrs))
    assert r.status_code==200,r.text
    result=(await fleet.client.get('/api/otel/traces/'+'a'*32,params={'instance_id':instance})).json()
    assert result['cost_usd']=='0.004'
    assert result['data'][0]['pricing']['pricing_source']=='override'
    attrs[0]['value']['stringValue']='vendor/model-v2'
    await send(fleet,token['token'],batch(trace='f'*32,attributes=attrs))
    unknown=(await fleet.client.get('/api/otel/traces/'+'f'*32,params={'instance_id':instance})).json()
    assert unknown['cost_usd'] is None


async def test_one_error_per_failure_with_the_nodes_masked_message(fleet):
    instance,token=await issue(fleet)
    now=time.time_ns()
    root={'traceId':'f'*32,'spanId':'1'*16,'name':'workflow.execute','startTimeUnixNano':str(now-2000),'endTimeUnixNano':str(now),
          'status':{'code':2,'message':'Workflow failed'},'attributes':[{'key':'n8n.workflow.id','value':{'stringValue':'wf-9'}}]}
    node={'traceId':'f'*32,'spanId':'2'*16,'parentSpanId':'1'*16,'name':'node HTTP Request','startTimeUnixNano':str(now-1500),'endTimeUnixNano':str(now-500),
          'status':{'code':2,'message':'Request failed with status code 404 at https://api.example.com/v1/orders?token=abcdef1234567890abcdef1234'},
          'attributes':[{'key':'n8n.node.name','value':{'stringValue':'HTTP Request'}},{'key':'n8n.workflow.id','value':{'stringValue':'wf-9'}}]}
    payload=lambda *spans:{'resourceSpans':[{'resource':{'attributes':[]},'scopeSpans':[{'spans':list(spans)}]}]}
    # The root span arrives first, in its own batch: one workflow-level record with its own message.
    assert (await send(fleet,token['token'],payload(root))).status_code==200
    rows=(await fleet.client.get('/api/errors',params={'instance_id':instance})).json()['data']
    assert [(row['node_name'],row['error_message']) for row in rows]==[('workflow.execute','Workflow failed')]
    # The failing node's span replaces it: still one record, named for the node, secrets masked.
    assert (await send(fleet,token['token'],payload(node))).status_code==200
    rows=(await fleet.client.get('/api/errors',params={'instance_id':instance})).json()['data']
    assert len(rows)==1 and rows[0]['node_name']=='HTTP Request'
    assert rows[0]['error_message'].startswith('Request failed with status code 404')
    assert 'abcdef1234567890abcdef1234' not in rows[0]['error_message']
    # A node span that fails without a message still gets the generic line, and no second record appears.
    assert (await send(fleet,token['token'],payload({**root,'spanId':'3'*16,'status':{'code':2}}))).status_code==200
    assert len((await fleet.client.get('/api/errors',params={'instance_id':instance})).json()['data'])==1


def _otlp(*spans):
    return {'resourceSpans':[{'resource':{'attributes':[]},'scopeSpans':[{'spans':list(spans)}]}]}


def _span(trace,span_id,name,status,message=None,attrs=(),parent=None,ints=()):
    now=time.time_ns()
    result={'traceId':trace,'spanId':span_id,'name':name,'startTimeUnixNano':str(now-2000),'endTimeUnixNano':str(now),
            'status':{'code':status,**({'message':message} if message else {})},
            'attributes':[{'key':k,'value':{'stringValue':v}} for k,v in attrs]+[{'key':k,'value':{'intValue':str(v)}} for k,v in ints]}
    if parent:
        result['parentSpanId']=parent
    return result


# Real n8n 2.x: the execution ID rides on the root workflow span only.
_ROOT=[('n8n.workflow.id','wf-1'),('n8n.workflow.name','Orders'),('n8n.execution.id','ex1')]


async def _records(fleet,instance):
    rows=(await fleet.client.get('/api/errors',params={'instance_id':instance})).json()['data']
    return [(row['node_name'],row['execution_id'],row['workflow_id']) for row in rows]


async def test_a_failure_keeps_its_run_identity_whichever_span_arrives_first(fleet):
    instance,token=await issue(fleet)
    node=[('n8n.node.name','HTTP Request')]
    # Root first, node in a later batch: the node's record borrows the run's execution and workflow.
    await send(fleet,token['token'],_otlp(_span('c'*32,'4'*16,'workflow.execute',2,'Workflow failed',_ROOT)))
    await send(fleet,token['token'],_otlp(_span('c'*32,'5'*16,'node HTTP Request',2,'Request failed',node,parent='4'*16)))
    assert await _records(fleet,instance)==[('HTTP Request','ex1','wf-1')]
    # Node first: the root, arriving later, fills the node's record in.
    await send(fleet,token['token'],_otlp(_span('d'*32,'6'*16,'node HTTP Request',2,'Request failed',node,parent='7'*16)))
    await send(fleet,token['token'],_otlp(_span('d'*32,'7'*16,'workflow.execute',2,'Workflow failed',
                                                [('n8n.workflow.id','wf-1'),('n8n.workflow.name','Orders'),('n8n.execution.id','ex2')])))
    assert sorted(await _records(fleet,instance))==[('HTTP Request','ex1','wf-1'),('HTTP Request','ex2','wf-1')]
    filtered=(await fleet.client.get('/api/errors',params={'instance_id':instance,'workflow_id':'wf-1'})).json()['data']
    assert len(filtered)==2


async def test_classification_does_not_add_back_a_recorded_failure(fleet):
    instance,token=await issue(fleet)
    node=[('n8n.node.name','HTTP Request'),('n8n.node.id','n1'),('n8n.workflow.id','wf-1')]
    await send(fleet,token['token'],_otlp(_span('a'*32,'2'*16,'node HTTP Request',2,'Request failed',node,parent='1'*16),
                                          _span('a'*32,'1'*16,'workflow.execute',2,'Workflow failed',_ROOT)))
    await send(fleet,token['token'],_otlp(_span('b'*32,'3'*16,'workflow.execute',2,'Workflow failed',[('n8n.workflow.id','wf-1'),('n8n.execution.id','ex2')])))
    before=await _records(fleet,instance)
    assert len(before)==2
    for execution in ('ex1','ex2'):
        r=await fleet.client.post('/api/otel/classify',headers=fleet.headers,json={'instance_id':instance,'execution_id':execution})
        assert r.status_code==200,r.text
    assert sorted(await _records(fleet,instance))==sorted(before)


async def test_stored_error_text_masks_unquoted_secrets(fleet):
    instance,token=await issue(fleet)
    message='Auth failed: api_key=abcd1234efgh5678 (password=Hunter2024!) x-api-key: 9f8e7d6c5b4a3921 key sk-ab12cd34ef56'
    await send(fleet,token['token'],_otlp(_span('e'*32,'8'*16,'node HTTP Request',2,message,[('n8n.node.name','HTTP Request')])))
    stored=(await fleet.client.get('/api/errors',params={'instance_id':instance})).json()['data'][0]['error_message']
    for secret in ('abcd1234efgh5678','Hunter2024!','9f8e7d6c5b4a3921','sk-ab12cd34ef56'):
        assert secret not in stored
    assert stored.startswith('Auth failed: api_key=')


async def test_trace_cost_totals_count_ai_steps_of_the_whole_trace(fleet):
    instance,token=await issue(fleet)
    await fleet.client.put('/api/otel/prices/gpt-test',headers=fleet.headers,json={'input_per_million':'1','output_per_million':'2'})
    ai=[('gen_ai.request.model','gpt-test')]
    usage=(('gen_ai.usage.input_tokens',1000),('gen_ai.usage.output_tokens',500))
    await send(fleet,token['token'],_otlp(_span('f'*32,'9'*16,'workflow.execute',1),
                                          _span('f'*32,'a'*16,'node Chat',1,attrs=ai,ints=usage,parent='9'*16)))
    first=(await fleet.client.get('/api/otel/traces/'+'f'*32,params={'instance_id':instance,'limit':1})).json()
    # A step that is not an AI call is never "unpriced", and the total covers steps beyond the returned page.
    assert first['unknown_cost_spans']==0 and first['ai_spans']==1
    priced=Decimal(first['known_cost_usd'])
    assert first['cost_usd'] is None or Decimal(first['cost_usd'])==priced
