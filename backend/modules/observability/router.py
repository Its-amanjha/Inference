"""Machine ingestion is token-scoped; browser administration retains session/CSRF."""
import asyncio
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import hashlib
import json
import secrets
import time
from uuid import uuid4

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse

from backend import audit
from backend.auth_gate import fail, require_current_role, require_role, token_hash
from backend.database import _settle
from backend.modules.n8n_proxy.client import N8nClient, instance_guard
from backend.modules.n8n_proxy.router import resource_id
from .ingest import IngestError, parse_traces
from .classifier import classify, _terminal_ns
from .pricing import calculate_cost, select_prices
from .pricebook import router as price_router
from .polling import router as polling_router

router=APIRouter()
router.include_router(price_router)
router.include_router(polling_router)
RAW_LIMIT=2*1024*1024


class TokenCreate(BaseModel):
    model_config=ConfigDict(extra='forbid')
    instance_id: str=Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')
    name: str=Field(min_length=1,max_length=120)


class Retention(BaseModel):
    model_config=ConfigDict(extra='forbid')
    days: int=Field(ge=1,le=365,strict=True)


class Classification(BaseModel):
    model_config=ConfigDict(extra='forbid')
    instance_id: str=Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')
    execution_id: str=Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')
    zero_output_nodes: set[str]=Field(default_factory=set,max_length=100)


@router.post('/tokens')
async def create_token(body: TokenCreate,request: Request):
    require_role(request,'admin')
    token=secrets.token_urlsafe(32)
    identifier=uuid4().hex
    async with request.app.state.db.transaction() as tx:
        await require_current_role(request,'admin',tx)
        if not await tx.fetchone('SELECT id FROM instances WHERE id=?',(body.instance_id,)):
            fail(404,'instance_not_found','Instance was not found.')
        await tx.execute('INSERT INTO telemetry_tokens(id,instance_id,name,token_hash) VALUES(?,?,?,?)',(identifier,body.instance_id,body.name,token_hash(token)))
    await audit.record(request,'telemetry.token_created',identifier,{'instance_id':body.instance_id})
    return {'id':identifier,'instance_id':body.instance_id,'name':body.name,'token':token}


@router.get('/tokens')
async def tokens(request: Request,instance_id: str=Query(min_length=1,max_length=128)):
    require_role(request,'admin')
    return {'data':await request.app.state.db.fetchall('SELECT id,instance_id,name,created_at,revoked_at FROM telemetry_tokens WHERE instance_id=? ORDER BY created_at,id',(instance_id,))}


@router.delete('/tokens/{identifier}')
async def revoke(identifier: str,request: Request):
    require_role(request,'admin')
    async with request.app.state.db.transaction() as tx:
        await require_current_role(request,'admin',tx)
        if not await tx.fetchone('SELECT id FROM telemetry_tokens WHERE id=?',(identifier,)):
            fail(404,'token_not_found','Telemetry token was not found.')
        await tx.execute('UPDATE telemetry_tokens SET revoked_at=CURRENT_TIMESTAMP WHERE id=?',(identifier,))
    await audit.record(request,'telemetry.token_revoked',identifier)
    return {'revoked':True}


async def machine_auth(request,tx=None):
    headers=request.headers.getlist('authorization')
    if len(headers)!=1 or not headers[0].startswith('Bearer ') or not 20<=len(headers[0][7:])<=128:
        fail(401,'telemetry_auth_required','A valid telemetry bearer token is required.')
    row=await (tx or request.app.state.db).fetchone('SELECT t.id,t.instance_id FROM telemetry_tokens t JOIN instances i ON i.id=t.instance_id WHERE t.token_hash=? AND t.revoked_at IS NULL',(token_hash(headers[0][7:]),))
    if not row:
        fail(401,'telemetry_auth_required','A valid telemetry bearer token is required.')
    return row


async def read_body(request):
    chunks=[]
    size=0
    async def read():
        nonlocal size
        async for chunk in request.stream():
            size+=len(chunk)
            if size>RAW_LIMIT:
                fail(413,'telemetry_too_large','Telemetry request exceeds 2 MiB.')
            chunks.append(chunk)
        return b''.join(chunks)
    try:
        return await asyncio.wait_for(read(),15)
    except asyncio.TimeoutError:
        fail(408,'telemetry_timeout','Telemetry request body timed out.')


async def span_error(tx,span):
    """One error per failure. n8n marks both the failing node's span and the workflow's root span as
    errors; the node's record wins, and a workflow-level record stands only while no node span failed."""
    summary=span.get('status_message') or GENERIC_ERROR
    workflow_key=hashlib.sha256(f"{span['instance_id']}:{span['trace_id']}:workflow".encode()).hexdigest()
    if span.get('node_name') or span.get('node_id'):
        if not (span.get('workflow_id') and span.get('execution_id')):
            known=await tx.fetchone("SELECT workflow_id,workflow_name,execution_id FROM otel_spans WHERE instance_id=? AND trace_id=? "
                                    "AND (workflow_id<>'' OR execution_id<>'') ORDER BY (COALESCE(node_name,'')='' AND COALESCE(node_id,'')='') DESC,id LIMIT 1",
                                    (span['instance_id'],span['trace_id']))
            if known:
                span={**span,**{key:span.get(key) or known[key] for key in ('workflow_id','workflow_name','execution_id')}}
        await error_record(tx,span,summary=summary)
        # The root span may have arrived first, in an earlier batch.
        await tx.execute('DELETE FROM errors WHERE error_key=?',(workflow_key,))
        return
    failed_nodes=await tx.fetchall("SELECT span_id FROM otel_spans WHERE instance_id=? AND trace_id=? AND status='error' AND (node_name<>'' OR node_id<>'')",
                                   (span['instance_id'],span['trace_id']))
    if not failed_nodes:
        await error_record(tx,span,summary=summary,key=workflow_key)
        return
    # The failure is already on record under its node; give that record the identity this root carries.
    for row in failed_nodes:
        key=hashlib.sha256(f"{span['instance_id']}:{span['trace_id']}:{row['span_id']}".encode()).hexdigest()
        await tx.execute("UPDATE errors SET workflow_id=CASE WHEN COALESCE(workflow_id,'')='' THEN ? ELSE workflow_id END,"
                         "workflow_name=CASE WHEN COALESCE(workflow_name,'') IN ('','Unknown Workflow') AND ?<>'' THEN ? ELSE workflow_name END,"
                         "execution_id=CASE WHEN COALESCE(execution_id,'')='' THEN ? ELSE execution_id END WHERE error_key=?",
                         (span.get('workflow_id',''),span.get('workflow_name',''),span.get('workflow_name',''),span.get('execution_id',''),key))


async def price_snapshot(tx):
    rows=await tx.fetchall('SELECT model,value_json FROM price_book')
    result={}
    for row in rows:
        layers=json.loads(row['value_json'])
        picked=select_prices({row['model']:layers['override']} if layers.get('override') else {},
                             {row['model']:layers['live']} if layers.get('live') else {},
                             {row['model']:layers['bundled']} if layers.get('bundled') else {})
        result.update(picked)
    return result


GENERIC_ERROR='A node reported an execution error.'


async def error_record(tx,span,error_type='ExecutionError',summary=GENERIC_ERROR,key=None):
    key=key or hashlib.sha256(f"{span['instance_id']}:{span['trace_id']}:{span['span_id']}".encode()).hexdigest()
    await tx.execute('INSERT INTO errors(instance_id,workflow_id,workflow_name,execution_id,node_name,error_type,error_message,error_key) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(error_key) DO NOTHING',
                     (span['instance_id'],span.get('workflow_id',''),span.get('workflow_name',''),span.get('execution_id',''),span.get('node_name') or span['name'],error_type,summary,key))


@router.post('/v1/traces')
async def ingest(request: Request):
    identity=await machine_auth(request)
    if not request.app.state.limiter.hit('ingest:'+identity['id'],limit=120,window=60):
        fail(429,'telemetry_rate_limited','Telemetry rate limit exceeded.')
    slots=request.app.state.ingest_slots
    if slots.locked():
        fail(503,'telemetry_busy','Telemetry parser is busy; retry later.')
    async with slots:
        body=await read_body(request)
        content_type=request.headers.get('content-type','').split(';')[0].strip().lower()
        try:
            parsed=await _settle(asyncio.to_thread(parse_traces,body,content_type,request.headers.get('content-encoding','identity')))
        except IngestError as exc:
            fail(exc.status,exc.code,str(exc))
        async with request.app.state.db.transaction() as tx:
            identity=await machine_auth(request,tx)
            prices=await price_snapshot(tx)
            for span in parsed['spans']:
                attrs=json.loads(span['attributes_json'])
                pricing=calculate_cost(span.get('model'),span.get('tokens_in'),span.get('tokens_out'),prices,
                    provider=attrs.get('gen_ai.provider.name',attrs.get('gen_ai.system')),
                    cached_tokens=attrs.get('gen_ai.usage.cache_read.input_tokens'),reasoning_tokens=attrs.get('gen_ai.usage.reasoning.output_tokens'))
                span={**span,'instance_id':identity['instance_id'],'cost_exact':pricing['cost_usd'],
                      'cost_usd':float(pricing['cost_usd']) if pricing['cost_usd'] is not None else None,
                      'pricing_json':json.dumps(pricing),'health_status':'error' if span['status']=='error' else 'unknown'}
                columns=('instance_id','trace_id','span_id','parent_id','name','kind','start_ns','end_ns','status','attributes_json','workflow_id','workflow_name','execution_id','node_id','node_name','model','tokens_in','tokens_out','output_items','input_items','cost_exact','cost_usd','pricing_json','health_status')
                inserted=await tx.fetchone(f"INSERT INTO otel_spans({','.join(columns)}) VALUES({','.join('?' for _ in columns)}) ON CONFLICT(instance_id,trace_id,span_id) DO NOTHING RETURNING id",tuple(span.get(key) for key in columns))
                if inserted and span['status']=='error':
                    await span_error(tx,span)
    rejected=parsed['rejected_spans']
    if content_type=='application/x-protobuf':
        response=ExportTraceServiceResponse()
        if rejected:
            response.partial_success.rejected_spans=rejected
            response.partial_success.error_message='Invalid spans were rejected.'
        return Response(response.SerializeToString(),media_type='application/x-protobuf')
    return {'partialSuccess':{'rejectedSpans':str(rejected),'errorMessage':'Invalid spans were rejected.'}} if rejected else {}


@router.get('/traces')
async def traces(request: Request,instance_id: str | None=Query(default=None,max_length=128),workflow_id: str | None=Query(default=None,max_length=128),
                 limit: int=Query(default=50,ge=1,le=100),before: int | None=Query(default=None,ge=1)):
    require_role(request,'viewer')
    conditions,params=[],[]
    if instance_id:
        conditions.append('instance_id=?')
        params.append(instance_id)
    # A workflow ID is only unique within its instance; callers pass both to name one workflow. The filter
    # selects whole traces, so steps that do not carry the workflow ID still count toward them.
    if workflow_id:
        conditions.append('trace_id IN (SELECT trace_id FROM otel_spans WHERE workflow_id=?' + (' AND instance_id=?' if instance_id else '') + ')')
        params.extend([workflow_id, instance_id] if instance_id else [workflow_id])
    where='WHERE '+' AND '.join(conditions) if conditions else ''
    having='HAVING MAX(id)<?' if before else ''
    if before:
        params.append(before)
    params.append(limit+1)
    rows=await request.app.state.db.fetchall(f'SELECT instance_id,trace_id,MIN(start_ns) start_ns,MAX(end_ns) end_ns,COUNT(*) span_count,{FAILED_STEPS} error_spans,MAX(id) cursor '
        f'FROM otel_spans {where} GROUP BY instance_id,trace_id {having} ORDER BY cursor DESC LIMIT ?',params)
    more=len(rows)>limit
    rows=rows[:limit]
    for row in rows:
        row['start_ns']=str(row['start_ns'])
        row['end_ns']=str(row['end_ns'])
        # Workflow and execution identity come together from one span that reported them, so a trace is
        # labelled by its workflow instead of its hex ID and never mixes two spans' fields.
        named=await request.app.state.db.fetchone("SELECT workflow_id,workflow_name,execution_id FROM otel_spans WHERE instance_id=? AND trace_id=? AND workflow_id<>'' ORDER BY start_ns,id LIMIT 1",
                                                   (row['instance_id'],row['trace_id']))
        row.update({key:(named or {}).get(key) or None for key in ('workflow_id','workflow_name','execution_id')})
    return {'data':rows,'next_cursor':str(rows[-1]['cursor']) if more else None}


# When telemetry last arrived from one instance: one indexed read, not a grouping of its spans.
@router.get('/last-span')
async def last_span(request: Request,instance_id: str=Query(min_length=1,max_length=128)):
    require_role(request,'viewer')
    row=await request.app.state.db.fetchone('SELECT MAX(start_ns) start_ns FROM otel_spans WHERE instance_id=?',(instance_id,))
    value=(row or {}).get('start_ns')
    return {'instance_id':instance_id,'start_ns':str(value) if value is not None else None}


@router.get('/traces/{trace_id}')
async def trace(trace_id: str,request: Request,instance_id: str=Query(min_length=1,max_length=128),limit: int=Query(default=2000,ge=1,le=2000),after: int=Query(default=0,ge=0)):
    require_role(request,'viewer')
    rows=await request.app.state.db.fetchall('SELECT * FROM otel_spans WHERE instance_id=? AND trace_id=? AND id>? ORDER BY id LIMIT ?',(instance_id,trace_id,after,limit+1))
    if not rows and after==0:
        fail(404,'trace_not_found','Trace was not found.')
    more=len(rows)>limit
    rows=rows[:limit]
    # Totals cover the whole trace; only AI steps (a model or token counts) carry a price, so only they can lack one.
    costs=await request.app.state.db.fetchall('SELECT cost_exact,model,tokens_in,tokens_out FROM otel_spans WHERE instance_id=? AND trace_id=?',(instance_id,trace_id))
    ai_rows=[row for row in costs if row['model'] is not None or row['tokens_in'] is not None or row['tokens_out'] is not None]
    values=[Decimal(row['cost_exact']) for row in ai_rows if row['cost_exact'] is not None]
    with localcontext() as context:
        context.prec=max(28,max((v.adjusted() for v in values),default=0)-min((v.as_tuple().exponent for v in values),default=0)+len(str(len(values)))+3)
        known=sum(values,Decimal(0))
    unknown=sum(row['cost_exact'] is None for row in ai_rows)
    plain=lambda value:(lambda text:text.rstrip('0').rstrip('.') if '.' in text else text)(format(value,'f'))
    for row in rows:
        row['id']=str(row['id'])
        row['start_ns']=str(row['start_ns'])
        row['end_ns']=str(row['end_ns'])
        row['attributes']=json.loads(row.pop('attributes_json'))
        row['pricing']=json.loads(row.pop('pricing_json') or '{}')
    return {'data':rows,'next_cursor':rows[-1]['id'] if more else None,'cost_usd':None if unknown else plain(known),'ai_spans':len(ai_rows),
            'known_cost_usd':plain(known),'unknown_cost_spans':unknown,'cost_scope':'trace'}


# Failed steps of one trace (used inside GROUP BY instance_id,trace_id). n8n marks both the failing node's
# span and the workflow's root span as errors, so a workflow-level error counts only when no node failed,
# the same rule that keeps one error record per failure.
_FAILED="(status='error' OR health_status='error')"
_NODE="(COALESCE(node_name,'')<>'' OR COALESCE(node_id,'')<>'')"
FAILED_STEPS=(f'(SUM({_FAILED} AND {_NODE})+CASE WHEN SUM({_FAILED} AND {_NODE})=0 '
              f'THEN MIN(1,SUM({_FAILED} AND NOT {_NODE})) ELSE 0 END)')


@router.get('/summary')
async def summary(request: Request,instance_id: str | None=Query(default=None,max_length=128)):
    require_role(request,'viewer')
    where='WHERE instance_id=?' if instance_id else ''
    params=(instance_id,) if instance_id else ()
    counts=await request.app.state.db.fetchone(f'SELECT COALESCE(SUM(n),0) spans,COUNT(*) traces,COALESCE(SUM(steps),0) error_spans '
        f'FROM (SELECT COUNT(*) n,{FAILED_STEPS} steps FROM otel_spans {where} GROUP BY instance_id,trace_id)',params)
    counts['incomplete_is_failure']=False
    return counts


async def retention_days(db):
    row=await db.fetchone("SELECT value_json FROM documents WHERE key='telemetry_retention'")
    return json.loads(row['value_json'])['days'] if row else 7


async def cleanup(db):
    async with db.transaction() as tx:
        days=await retention_days(tx)
        await tx.execute("DELETE FROM otel_spans WHERE received_at<datetime('now',?)",(f'-{days} days',))
        await tx.execute("DELETE FROM errors WHERE occurred_at<datetime('now',?)",(f'-{days} days',))
        await tx.execute("DELETE FROM execution_cursors WHERE datetime(json_extract(value_json,'$.received_at'))<datetime('now',?)",(f'-{days} days',))


async def maintenance(app):
    while True:
        await cleanup(app.state.db)
        await asyncio.sleep(3600)


def execution_summary(execution):
    data=execution.get('data') or {}
    result=data.get('resultData',{}) if isinstance(data,dict) else {}
    runs=result.get('runData',{}) if isinstance(result,dict) else {}
    safe={}
    if isinstance(runs,dict):
        if len(runs)>2000:
            fail(413,'execution_too_large','Execution contains too many nodes to classify.')
        for name,items in runs.items():
            if not isinstance(items,list) or len(items)>2000:
                fail(502,'execution_invalid','Execution run data has an unsupported shape.')
            safe[name]=[{'error':bool(item.get('error')),'executionStatus':item.get('executionStatus') if item.get('executionStatus') in ('success','error') else None,
                         'taskData':{'continuation':{'error':bool((item.get('taskData') or {}).get('continuation',{}).get('error'))}}}
                        for item in items if isinstance(item,dict) and isinstance(item.get('taskData') or {},dict) and isinstance((item.get('taskData') or {}).get('continuation',{}),dict)]
    return {'status':execution.get('status'),'stoppedAt':execution.get('stoppedAt'),'data':{'resultData':{'runData':safe}}}


@router.get('/retention')
async def get_retention(request: Request):
    require_role(request,'admin')
    return {'days':await retention_days(request.app.state.db)}


@router.put('/retention')
async def set_retention(body: Retention,request: Request):
    require_role(request,'admin')
    async with request.app.state.db.transaction() as tx:
        await require_current_role(request,'admin',tx)
        await tx.execute("INSERT INTO documents(key,value_json) VALUES('telemetry_retention',?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",(body.model_dump_json(),))
    await cleanup(request.app.state.db)
    await audit.record(request,'telemetry.retention_changed','telemetry',body.model_dump())
    return body.model_dump()


@router.post('/classify')
async def classify_execution(body: Classification,request: Request):
    require_role(request,'operator')
    async def authorize(tx):
        await require_current_role(request,'operator',tx)
    return await classify_instance(body,request,N8nClient(request),authorize)


async def classify_instance(body,request,client,authorize,generation=None):
    async with instance_guard(request,body.instance_id):
        spans=await request.app.state.db.fetchall('SELECT * FROM otel_spans WHERE instance_id=? AND execution_id=? ORDER BY id LIMIT 2001',(body.instance_id,body.execution_id))
        if len(spans)>2000:
            fail(413,'execution_too_large','Execution exceeds the classification span limit.')
        if not spans:
            return {'data':[],'diagnostics':[{'code':'incomplete_telemetry'}]}
        connection=await request.app.state.db.fetchone('SELECT url,api_key_enc FROM instances WHERE id=?',(body.instance_id,))
        if not connection:
            fail(404,'instance_not_found','Instance was not found.')
        connection_hash=hashlib.sha256(json.dumps(connection,sort_keys=True).encode()).hexdigest()
        cache_key=body.instance_id+':'+body.execution_id
        cached=await request.app.state.db.fetchone('SELECT value_json FROM execution_cursors WHERE key=?',(cache_key,))
        cached=json.loads(cached['value_json']) if cached else {}
        terminal=cached.get('execution',{}).get('status') in ('success','error','canceled','crashed')
        if terminal and cached.get('connection_hash')==connection_hash:
            execution=cached['execution']
        else:
            remote=await client.request(body.instance_id,'GET',f'/executions/{body.execution_id}',params={'includeData':'true'})
            execution=execution_summary(remote)
        node_keys=list({json.dumps([span.get('workflow_id'),span.get('node_id')]) for span in spans})
        histories=await request.app.state.db.fetchall(f"WITH recent AS (SELECT value_json FROM execution_cursors WHERE key LIKE ? AND key<>? ORDER BY json_extract(value_json,'$.checked_at_epoch') DESC LIMIT 1000) SELECT j.key node_key,COUNT(*) successful_runs,SUM(json_extract(j.value,'$.output_items')=0) zero_output_runs FROM recent,json_each(recent.value_json,'$.nodes') j WHERE json_extract(recent.value_json,'$.execution.status')='success' AND json_extract(recent.value_json,'$.connection_hash')=? AND j.key IN ({','.join('?' for _ in node_keys)}) AND json_type(j.value,'$.output_items')='integer' GROUP BY j.key",(body.instance_id+':%',cache_key,connection_hash,*node_keys))
        histories={row['node_key']:row for row in histories}
        for span in spans:
            span['history']=histories.get(json.dumps([span.get('workflow_id'),span.get('node_id')]),{'successful_runs':0,'zero_output_runs':0})
        opted=set(body.zero_output_nodes)
        if not opted:
            preferences=await request.app.state.db.fetchone('SELECT value_json FROM documents WHERE key=?',('telemetry_polling:'+body.instance_id,))
            if preferences:
                choices=json.loads(preferences['value_json']).get('zero_output_nodes',{})
                sets=[set(choices.get(workflow,[])) for workflow in {span['workflow_id'] for span in spans}]
                opted=set.intersection(*sets) if sets else set()
        results=classify(spans,execution,time.time_ns(),zero_output_nodes=opted)
        results=[{**result,'trace_id':span['trace_id']} for span,result in zip(spans,results,strict=True)]
        async with request.app.state.db.transaction() as tx:
            await authorize(tx)
            nodes={}
            invalid=set()
            for span,result in zip(spans,results,strict=True):
                node_key=json.dumps([span.get('workflow_id'),span.get('node_id')])
                if not span.get('node_id') or span['status']!='ok' or result['health_status']=='error' or type(span.get('output_items')) is not int:
                    invalid.add(node_key)
                else:
                    nodes.setdefault(node_key,{'output_items':0})['output_items']+=span['output_items']
            terminal_ns=_terminal_ns(execution)
            snapshot={'execution':execution,'connection_hash':connection_hash,'received_at':datetime.now(timezone.utc).isoformat(),
                      'nodes':{key:value for key,value in nodes.items() if key not in invalid},
                      'last_span_id':max(span['id'] for span in spans),'generation':generation,'checked_at_epoch':time.time(),
                      'grace_complete':terminal_ns is not None and time.time_ns()>=terminal_ns+30*10**9}
            await tx.execute('INSERT INTO execution_cursors(key,value_json) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json',(cache_key,json.dumps(snapshot)))
            for span,result in zip(spans,results,strict=True):
                await tx.execute("UPDATE otel_spans SET health_status=?,error_type=?,error_summary=?,silent=?,checked_at=datetime('now') WHERE id=?",(result['health_status'],result.get('error_type'),result.get('error_summary'),int(result.get('silent',False)),span['id']))
                if result['health_status']=='error' and span['status']!='error':
                    await error_record(tx,span,result.get('error_type') or 'ExecutionError',result.get('error_summary') or 'Execution failure confirmed.')
    await audit.record(request,'telemetry.classified',body.instance_id,{'execution_id':body.execution_id})
    return {'data':results}
