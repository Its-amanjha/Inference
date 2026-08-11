"""Persistent, revocable grants for bounded read-only execution discovery."""
import asyncio
import hashlib
import json
import re
import time
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.requests import Request as BackgroundRequest

from backend import audit
from backend.auth_gate import fail, require_current_role, require_role
from backend.modules.n8n_proxy.client import N8nClient, instance_guard
from backend.modules.n8n_proxy.router import list_view

router=APIRouter()
PREFIX='telemetry_polling:'
STATUS_PREFIX='telemetry_polling_status:'
IDENTIFIER=re.compile(r'[A-Za-z0-9_-]{1,128}\Z')


class Settings(BaseModel):
    model_config=ConfigDict(extra='forbid')
    enabled: bool=False
    interval_seconds: int=Field(default=60,ge=30,le=3600,strict=True)
    zero_output_nodes: dict[str,list[str]]=Field(default_factory=dict,max_length=100)

    @field_validator('zero_output_nodes')
    @classmethod
    def validate_nodes(cls,value):
        if any(not IDENTIFIER.fullmatch(workflow) or len(nodes)>100 or any(not IDENTIFIER.fullmatch(node) for node in nodes) for workflow,nodes in value.items()):
            raise ValueError('Use bounded workflow/node identifiers')
        return {key:sorted(set(nodes)) for key,nodes in value.items()}


def connection_hash(row):
    return hashlib.sha256(json.dumps({'url':row['url'],'api_key_enc':row['api_key_enc']},sort_keys=True).encode()).hexdigest()


async def load(db,key,default=None):
    row=await db.fetchone('SELECT value_json FROM documents WHERE key=?',(key,))
    return json.loads(row['value_json']) if row else default


async def save(db,key,value):
    await db.execute("INSERT INTO documents(key,value_json) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=datetime('now')",(key,json.dumps(value)))


async def grant(tx,instance_id,version):
    config=await load(tx,PREFIX+instance_id)
    if not config or not config['enabled'] or config['version']!=version:
        fail(403,'polling_disabled','The polling grant was disabled or replaced.')
    owner=await tx.fetchone('SELECT role,disabled FROM users WHERE username=?',(config['owner'],))
    if not owner or owner['disabled'] or owner['role']!='admin':
        fail(403,'polling_owner_revoked','Polling requires its granting administrator to remain enabled.')
    row=await tx.fetchone('SELECT url,api_key_enc FROM instances WHERE id=?',(instance_id,))
    if not row or connection_hash(row)!=config['connection_hash']:
        fail(403,'polling_connection_changed','Re-enable polling after changing connection settings.')
    return config


class PollingClient(N8nClient):
    def __init__(self,request,instance_id,version):
        super().__init__(request)
        self.instance_id=instance_id
        self.version=version

    async def _authorize(self,tx,required_role,instance_id,method,path):
        if instance_id!=self.instance_id or method!='GET' or not re.fullmatch(r'/executions(?:/[A-Za-z0-9_-]{1,128})?',path):
            fail(403,'polling_operation_denied','Polling can only read execution data from its granted instance.')
        await grant(tx,self.instance_id,self.version)


@router.get('/polling/{instance_id}')
async def get_settings(instance_id: str,request: Request):
    require_role(request,'admin')
    config=await load(request.app.state.db,PREFIX+instance_id,Settings().model_dump())
    return {'instance_id':instance_id,**{key:config[key] for key in ('enabled','interval_seconds','zero_output_nodes')},
            'owner':config.get('owner'),'status':await load(request.app.state.db,STATUS_PREFIX+instance_id,{})}


@router.put('/polling/{instance_id}')
async def configure(instance_id: str,body: Settings,request: Request):
    actor=require_role(request,'admin')
    async with request.app.state.db.transaction() as tx:
        await require_current_role(request,'admin',tx)
        row=await tx.fetchone('SELECT url,api_key_enc FROM instances WHERE id=?',(instance_id,))
        if not row:
            fail(404,'instance_not_found','Instance was not found.')
        config={**body.model_dump(),'owner':actor['username'],'version':uuid4().hex,'connection_hash':connection_hash(row)}
        await save(tx,PREFIX+instance_id,config)
        await save(tx,STATUS_PREFIX+instance_id,{'next_attempt':time.time()+body.interval_seconds,'failures':0})
    await audit.record(request,'telemetry.polling_configured',instance_id,{'enabled':body.enabled})
    return await get_settings(instance_id,request)


@router.post('/polling/{instance_id}/run')
async def run_now(instance_id: str,request: Request):
    actor=require_role(request,'admin')
    if not request.app.state.limiter.hit('polling_run:'+actor['username'],limit=10,window=60):
        fail(429,'rate_limited','Wait before requesting another polling cycle.')
    async with request.app.state.db.transaction() as tx:
        await require_current_role(request,'admin',tx)
    return await tick(request.app,instance_id,force=True)


async def tick(app,instance_id,force=False):
    started=time.time()
    original=await load(app.state.db,PREFIX+instance_id,{})
    try:
        return await asyncio.wait_for(_tick(app,instance_id,force),timeout=120)
    except asyncio.TimeoutError:
        state={'last_error':'polling_deadline','next_attempt':time.time()+60}
        async with app.state.db.transaction() as tx:
            current=await load(tx,PREFIX+instance_id,{})
            saved=await load(tx,STATUS_PREFIX+instance_id,{})
            if current.get('version')==original.get('version') and saved.get('last_success',0)<started:
                await save(tx,STATUS_PREFIX+instance_id,{**saved,**state})
        return state


async def _tick(app,instance_id,force=False):
    # A server-owned request carries audit attribution only, never a browser session.
    request=BackgroundRequest({'type':'http','method':'GET','path':'/internal/telemetry','headers':[],'app':app})
    async with instance_guard(request,'polling:'+instance_id):
        config=await load(app.state.db,PREFIX+instance_id)
        state=await load(app.state.db,STATUS_PREFIX+instance_id,{})
        if not config or not config.get('enabled'):
            return {'enabled':False}
        if not force and time.time()<state.get('next_attempt',0):
            return state
        version=config['version']
        request.state.user={'username':'poller:'+config['owner']}
        client=PollingClient(request,instance_id,version)
        async def authorize(tx):
            await grant(tx,instance_id,version)
        try:
            async with app.state.db.transaction() as tx:
                await authorize(tx)
            params={'limit':100,'includeData':'false'}
            if state.get('cursor'):
                params['cursor']=state['cursor']
            page=list_view(await client.request(instance_id,'GET','/executions',params=params),instance_id)
            if len(page['data'])>100 or any(not isinstance(row.get('id'),str) or not IDENTIFIER.fullmatch(row['id']) for row in page['data']):
                fail(502,'upstream_invalid_response','Execution discovery returned unsupported identifiers.')
            cursor=page['next_cursor']
            if cursor and (len(cursor)>4096 or cursor in state.get('seen_cursors',[])):
                fail(502,'upstream_invalid_response','Execution discovery returned invalid pagination.')
            # Work is driven by actual stored telemetry, including late arrivals.
            rows=await app.state.db.fetchall("WITH candidates AS (SELECT s.execution_id,MAX(s.id) last_id,c.value_json,e.value_json error_json FROM otel_spans s LEFT JOIN execution_cursors c ON c.key=s.instance_id || ':' || s.execution_id LEFT JOIN documents e ON e.key='telemetry_poll_error:' || s.instance_id || ':' || s.execution_id WHERE s.instance_id=? AND s.execution_id<>'' GROUP BY s.execution_id) SELECT * FROM candidates WHERE (COALESCE(json_extract(value_json,'$.grace_complete'),0)=0 OR json_extract(value_json,'$.generation') IS NOT ? OR json_extract(value_json,'$.last_span_id') IS NOT last_id) AND COALESCE(json_extract(error_json,'$.next_attempt'),0)<=? ORDER BY COALESCE(json_extract(error_json,'$.failures'),0),COALESCE(json_extract(value_json,'$.checked_at_epoch'),0),last_id LIMIT 20",(instance_id,version,time.time()))
            from .router import Classification, classify_instance
            count=0
            execution_errors=[]
            for row in rows:
                if not IDENTIFIER.fullmatch(row['execution_id']):
                    continue
                cached=json.loads(row['value_json']) if row['value_json'] else {}
                complete=cached.get('grace_complete',False)
                if complete and cached.get('generation')==version and cached.get('last_span_id')==row['last_id']:
                    continue
                error_key='telemetry_poll_error:'+instance_id+':'+row['execution_id']
                try:
                    await classify_instance(Classification(instance_id=instance_id,execution_id=row['execution_id']),request,client,authorize,generation=version)
                    async with app.state.db.transaction() as tx:
                        await authorize(tx)
                        await tx.execute('DELETE FROM documents WHERE key=?',(error_key,))
                    count+=1
                except HTTPException as exc:
                    code=exc.detail.get('code','classification_failed') if isinstance(exc.detail,dict) else 'classification_failed'
                    if code.startswith('polling_'):
                        raise
                    failures=(json.loads(row['error_json']).get('failures',0) if row['error_json'] else 0)+1
                    async with app.state.db.transaction() as tx:
                        await authorize(tx)
                        await save(tx,error_key,{'code':code,'failures':min(failures,10),'next_attempt':time.time()+min(60*2**min(failures,6),3600)})
                    execution_errors.append({'execution_id':row['execution_id'],'code':code})
            seen=state.get('seen_cursors',[])+([cursor] if cursor else [])
            # Bounded historical sweep; return to newest executions after ten pages.
            reset=not cursor or len(seen)>=10
            state={'next_attempt':time.time()+config['interval_seconds'],'failures':0,'last_error':None,
                   'last_success':time.time(),'classified_executions':count,'discovered_executions':len(page['data']),
                   'execution_errors':execution_errors,
                   'cursor':None if reset else cursor,'seen_cursors':[] if reset else seen}
            async with app.state.db.transaction() as tx:
                await authorize(tx)
                await save(tx,STATUS_PREFIX+instance_id,state)
        except Exception as exc:
            code=exc.detail.get('code','polling_failed') if isinstance(exc,HTTPException) and isinstance(exc.detail,dict) else 'polling_failed'
            failures=min(state.get('failures',0)+1,10)
            state={**state,'failures':failures,'last_error':code,'next_attempt':time.time()+min(config['interval_seconds']*2**(failures-1),3600)}
            # Keep failure diagnostics, but never overwrite a newer grant's state.
            async with app.state.db.transaction() as tx:
                current=await load(tx,PREFIX+instance_id)
                if current and current.get('version')==version:
                    await save(tx,STATUS_PREFIX+instance_id,state)
        return state


async def maintenance(app):
    while True:
        rows=await app.state.db.fetchall("SELECT key FROM documents WHERE key LIKE 'telemetry_polling:%' AND json_extract(value_json,'$.enabled')=1 ORDER BY COALESCE((SELECT json_extract(s.value_json,'$.next_attempt') FROM documents s WHERE s.key='telemetry_polling_status:' || substr(documents.key,19)),0) LIMIT 100")
        pending=iter(rows)
        async def worker():
            for row in pending:
                await tick(app,row['key'][len(PREFIX):])
        workers=[asyncio.create_task(worker()) for _ in range(min(4,len(rows)))]
        try:
            await asyncio.gather(*workers)
        finally:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers,return_exceptions=True)
        await asyncio.sleep(15)
