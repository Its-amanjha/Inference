"""Fleet endpoints. Instances are explicit; removing one never changes n8n."""
import asyncio
from datetime import datetime, timezone
import ipaddress
import json
import re
from time import monotonic
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend import audit
from backend.auth_gate import fail, require_current_role, require_role
from backend.net import allowed_ip, validate_url
from .client import N8nClient, instance_guard

router=APIRouter()
RESOURCE_ID=r'^[A-Za-z0-9_-]{1,128}$'


def clean_url(value: str) -> str:
    value=value.strip()
    raw=urlsplit(value)
    if '?' in value or '#' in value or '%' in raw.path or '\\' in value or any(segment in ('.','..') for segment in raw.path.split('/')):
        raise ValueError('Use an instance base URL without query, fragment or encoded path segments')
    url=validate_url(value)
    if url.path.rstrip('/').endswith('/api/v1'):
        raise ValueError('Supply the instance base URL, without /api/v1')
    return str(url).rstrip('/')


def clean_key(value: str) -> str:
    if not value or value!=value.strip() or any(ord(char)<33 or ord(char)>126 for char in value):
        raise ValueError('API keys must contain printable ASCII without whitespace')
    return value


class InstanceCreate(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name: str=Field(min_length=1,max_length=120)
    url: str=Field(min_length=1,max_length=2048)
    api_key: str=Field(min_length=1,max_length=4096)

    @field_validator('name')
    @classmethod
    def name_value(cls,value: str) -> str:
        if not value.strip():
            raise ValueError('A name is required')
        return value.strip()

    @field_validator('url')
    @classmethod
    def url_value(cls,value: str) -> str:
        return clean_url(value)

    @field_validator('api_key')
    @classmethod
    def key_value(cls,value: str) -> str:
        return clean_key(value)


class InstanceUpdate(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name: str | None=Field(default=None,min_length=1,max_length=120)
    url: str | None=Field(default=None,min_length=1,max_length=2048)
    api_key: str | None=Field(default=None,min_length=1,max_length=4096)

    @field_validator('url','name','api_key')
    @classmethod
    def value_valid(cls,value: str | None,info: Any) -> str | None:
        if value is None:
            return None
        if info.field_name=='url':
            return clean_url(value)
        if info.field_name=='api_key':
            return clean_key(value)
        if not value.strip():
            raise ValueError('A name is required')
        return value.strip()


def check_literal_destination(request: Request,url: str) -> None:
    try:
        address=ipaddress.ip_address(validate_url(url).host)
    except ValueError:
        return  # DNS destinations are validated and pinned at connection time.
    if not allowed_ip(str(address),request.app.state.settings.allow_cidrs):
        fail(422,'destination_blocked','Destination is blocked. Configure an explicit private-network allowlist if needed.')


def instance_view(row: dict[str,Any],last_probe: dict[str,Any] | None=None) -> dict[str,Any]:
    return {**{key:row[key] for key in ('id','name','url','is_active','created_at')},'api_key_configured':bool(row['api_key_enc']),
            'last_probe':last_probe}


PROBE_FIELDS=('reachable','checked_at','latency_ms','capabilities','diagnostics')


async def saved_probes(db: Any,instance_ids: list[str]) -> dict[str,dict[str,Any]]:
    """The last connection check of each instance, as stored by a probe or a fleet health scan, so every
    page shows the same reachability. A check is removed whenever the instance's URL or key changes."""
    if not instance_ids:
        return {}
    keys=[f'instance_probe:{identifier}' for identifier in instance_ids]
    rows=await db.fetchall(f"SELECT key,value_json FROM documents WHERE key IN ({','.join('?'*len(keys))})",tuple(keys))
    result={}
    for row in rows:
        try:
            value=json.loads(row['value_json'])
        except ValueError:
            continue
        if isinstance(value,dict) and isinstance(value.get('reachable'),bool) and isinstance(value.get('checked_at'),str):
            result[row['key'].split(':',1)[1]]={key:value.get(key) for key in PROBE_FIELDS}
    return result


async def save_probe(tx: Any,instance_id: str,result: dict[str,Any]) -> None:
    await tx.execute("INSERT INTO documents(key,value_json) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=datetime('now')",
                     (f'instance_probe:{instance_id}',json.dumps({'instance_id':instance_id,**{key:result.get(key) for key in PROBE_FIELDS}})))


async def instance_row(request: Request,instance_id: str,tx: Any=None) -> dict[str,Any]:
    row=await (tx or request.app.state.db).fetchone('SELECT * FROM instances WHERE id=?',(instance_id,))
    if row is None:
        fail(404,'instance_not_found','Instance was not found.')
    return row


@router.get('/instances')
async def instances(request: Request) -> dict[str,Any]:
    require_role(request,'viewer')
    rows=await request.app.state.db.fetchall('SELECT * FROM instances ORDER BY name,id')
    probes=await saved_probes(request.app.state.db,[row['id'] for row in rows])
    return {'data':[instance_view(row,probes.get(row['id'])) for row in rows]}


@router.post('/instances')
async def create_instance(body: InstanceCreate,request: Request) -> dict[str,Any]:
    require_role(request,'admin')
    check_literal_destination(request,body.url)
    identifier=uuid4().hex
    encrypted=request.app.state.vault.encrypt(body.api_key)
    async with request.app.state.db.transaction() as tx:
        await require_current_role(request,'admin',tx)
        await tx.execute('INSERT INTO instances(id,name,url,api_key_enc) VALUES(?,?,?,?)',(identifier,body.name,body.url,encrypted))
        row=await instance_row(request,identifier,tx)
    await audit.record(request,'fleet.instance_created',identifier)
    return instance_view(row)


@router.get('/instances/{instance_id}')
async def get_instance(instance_id: str,request: Request) -> dict[str,Any]:
    require_role(request,'viewer')
    probes=await saved_probes(request.app.state.db,[instance_id])
    return instance_view(await instance_row(request,instance_id),probes.get(instance_id))


@router.patch('/instances/{instance_id}')
async def update_instance(instance_id: str,body: InstanceUpdate,request: Request) -> dict[str,Any]:
    require_role(request,'admin')
    changes=body.model_dump(exclude_none=True)
    if not changes:
        fail(400,'empty_update','Supply a name, URL or new API key.')
    if 'url' in changes:
        check_literal_destination(request,changes['url'])
    if 'api_key' in changes:
        changes['api_key_enc']=request.app.state.vault.encrypt(changes.pop('api_key'))
    async with instance_guard(request,instance_id):
        async with request.app.state.db.transaction() as tx:
            await require_current_role(request,'admin',tx)
            await instance_row(request,instance_id,tx)
            assignment=','.join(f'{key}=?' for key in changes)  # Closed schema supplies column names.
            await tx.execute(f'UPDATE instances SET {assignment} WHERE id=?',(*changes.values(),instance_id))
            await tx.execute('DELETE FROM documents WHERE key=?',(f'instance_probe:{instance_id}',))
            row=await instance_row(request,instance_id,tx)
    await audit.record(request,'fleet.instance_updated',instance_id,{'fields':list(changes)})
    return instance_view(row)


@router.delete('/instances/{instance_id}')
async def delete_instance(instance_id: str,request: Request) -> dict[str,Any]:
    require_role(request,'admin')
    async with instance_guard(request,instance_id):
        async with request.app.state.db.transaction() as tx:
            await require_current_role(request,'admin',tx)
            await instance_row(request,instance_id,tx)
            await tx.execute('DELETE FROM instances WHERE id=?',(instance_id,))
            await tx.execute('DELETE FROM documents WHERE key=?',(f'instance_probe:{instance_id}',))
    await audit.record(request,'fleet.instance_removed',instance_id)
    return {'ok':True,'instance_id':instance_id,'remote_resources_changed':False}


def list_view(payload: dict[str,Any],instance_id: str) -> dict[str,Any]:
    data=payload.get('data')
    cursor=payload.get('nextCursor')
    if not isinstance(data,list) or any(not isinstance(row,dict) for row in data) or (cursor is not None and not isinstance(cursor,str)):
        fail(502,'upstream_invalid_response','n8n returned an unsupported list response.')
    return {'data':data,'next_cursor':cursor,'instance_id':instance_id}


def masked_document(payload: Any) -> Any:
    """Workflow documents reach browsers with secret parameter values replaced.

    Operators browse, inspect and promote workflows from these views; none of that needs the keys typed into
    node parameters. The masking primitives are the ones the repair context already uses, imported here so the
    fleet module keeps no import-time dependency on the assistant. Code editing reads its own endpoint in
    codelab.py, which still returns the real node code it has to write back.
    """
    from backend.modules.assistant.repair_context import mask_value
    return mask_value(payload,'',[],limit=None)


async def probe_result(request: Request,instance_id: str) -> dict[str,Any]:
    snapshot=await instance_row(request,instance_id)
    started=monotonic()
    client=N8nClient(request)
    capabilities={}
    diagnostics=[]
    reached=False
    for resource,capability in (('workflows','workflow_read'),('executions','execution_read')):
        params={'limit':1}
        if resource=='executions':
            params['includeData']='false'
        try:
            payload=await client.request(instance_id,'GET',f'/{resource}',params=params)
            list_view(payload,instance_id)
            capabilities[capability]='supported'
            reached=True
        except HTTPException as exc:
            detail=exc.detail if isinstance(exc.detail,dict) else {}
            code=detail.get('code','upstream_error')
            if code in ('authentication_required','session_revoked','session_expired','permission_denied','instance_not_found'):
                raise
            denied=code in ('upstream_auth_failed','upstream_forbidden')
            capabilities[capability]='forbidden' if denied else 'unavailable'
            reached=reached or denied or code in ('upstream_not_found','upstream_rate_limited','upstream_invalid_response')
            diagnostics.append({'capability':capability,'code':code})
    ensure_same_connection(snapshot,await instance_row(request,instance_id))
    return {'instance_id':instance_id,'reachable':reached,'capabilities':capabilities,'diagnostics':diagnostics,
            'checked_at':datetime.now(timezone.utc).isoformat(),'latency_ms':round((monotonic()-started)*1000)}


def ensure_same_connection(before: dict[str,Any],after: dict[str,Any]) -> None:
    if any(before[key]!=after[key] for key in ('url','api_key_enc')):
        fail(409,'instance_changed','Connection settings changed during the probe. Check the connection again.')


@router.post('/instances/{instance_id}/probe')
async def probe(instance_id: str,request: Request) -> dict[str,Any]:
    actor=require_role(request,'admin')
    if not request.app.state.limiter.hit(f"probe:{actor['username']}",limit=10,window=60):
        fail(429,'rate_limited','Wait before checking connections again.')
    snapshot=await instance_row(request,instance_id)
    result=await probe_result(request,instance_id)
    async with instance_guard(request,instance_id):
        async with request.app.state.db.transaction() as tx:
            await require_current_role(request,'admin',tx)
            ensure_same_connection(snapshot,await instance_row(request,instance_id,tx))
            await save_probe(tx,instance_id,result)
    await audit.record(request,'fleet.instance_probed',instance_id,{'reachable':result['reachable']})
    return result


@router.get('/health')
async def health(request: Request) -> dict[str,Any]:
    actor=require_role(request,'viewer')
    if not request.app.state.limiter.hit(f"fleet_health:{actor['username']}",limit=4,window=60):
        fail(429,'rate_limited','Wait before refreshing fleet health.')
    rows=await request.app.state.db.fetchall('SELECT id,name,url,api_key_enc FROM instances ORDER BY name,id')
    # Five fixed workers bound task count and outbound fan-out even for large fleets.
    pending=iter(rows)
    results=[]

    async def worker() -> None:
        for row in pending:
            try:
                result=await probe_result(request,row['id'])
            except HTTPException as exc:
                code=exc.detail.get('code','request_failed') if isinstance(exc.detail,dict) else 'request_failed'
                if code in ('authentication_required','session_revoked','session_expired','permission_denied'):
                    raise
                result={'instance_id':row['id'],'reachable':False,'capabilities':{},'diagnostics':[{'code':code}]}
            results.append({**result,'name':row['name']})

    workers=[asyncio.create_task(worker()) for _ in range(min(5,len(rows)))]
    try:
        await asyncio.gather(*workers)
    finally:
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers,return_exceptions=True)
    # Completed checks are saved like a probe's, unless the connection changed while they ran.
    before={row['id']:row for row in rows}
    async with request.app.state.db.transaction() as tx:
        for result in results:
            if 'checked_at' not in result:
                continue
            current=await tx.fetchone('SELECT url,api_key_enc FROM instances WHERE id=?',(result['instance_id'],))
            snapshot=before[result['instance_id']]
            if current and current['url']==snapshot['url'] and current['api_key_enc']==snapshot['api_key_enc']:
                await save_probe(tx,result['instance_id'],result)
    return {'data':sorted(results,key=lambda row:(row['name'],row['instance_id']))}


@router.get('/workflows')
async def workflows(request: Request,instance_id: str=Query(min_length=1,max_length=128),limit: int=Query(default=25,ge=1,le=250),
                    cursor: str | None=Query(default=None,max_length=2048),active: bool | None=None,name: str | None=Query(default=None,max_length=120)) -> dict[str,Any]:
    params: dict[str,Any]={'limit':limit,'excludePinnedData':'true'}
    params.update({key:value for key,value in {'cursor':cursor,'name':name,'active':str(active).lower() if active is not None else None}.items() if value is not None})
    view=list_view(await N8nClient(request).request(instance_id,'GET','/workflows',params=params),instance_id)
    return {**view,'data':[masked_document(row) for row in view['data']]}


@router.get('/dashboard')
async def dashboard(request: Request, instance_id: str | None=Query(default=None,min_length=1,max_length=128)) -> dict[str,Any]:
    from .dashboard import aggregate
    return await aggregate(request, instance_id)


@router.get('/workflows/{workflow_id}')
async def workflow(workflow_id: str,request: Request,instance_id: str=Query(min_length=1,max_length=128)) -> dict[str,Any]:
    resource_id(workflow_id)
    return {**masked_document(await N8nClient(request).request(instance_id,'GET',f'/workflows/{workflow_id}')),'instance_id':instance_id}


def resource_id(value: str) -> None:
    if not re.fullmatch(RESOURCE_ID,value):
        fail(422,'invalid_resource_id','Resource ID contains unsupported characters.')


async def finish_audit(request: Request,action: str,target: str,details: Any=None,outcome: str='success') -> None:
    task=asyncio.create_task(audit.record(request,action,target,details,outcome))
    cancelled=False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled=True
    task.result()
    if cancelled:
        raise asyncio.CancelledError()


async def mutate(request: Request,instance_id: str,method: str,path: str,action: str,target: str) -> dict[str,Any]:
    require_role(request,'operator')
    await audit.record(request,action,f'{instance_id}:{target}',outcome='intent')
    try:
        result=await N8nClient(request).request(instance_id,method,path)
    except HTTPException as exc:
        code=exc.detail.get('code','request_failed') if isinstance(exc.detail,dict) else 'request_failed'
        await finish_audit(request,action,f'{instance_id}:{target}',{'code':code},outcome='unknown' if code=='upstream_outcome_unknown' else 'failed')
        raise
    except asyncio.CancelledError:
        await finish_audit(request,action,f'{instance_id}:{target}',{'reason':'request_cancelled'},outcome='unknown')
        raise
    await finish_audit(request,action,f'{instance_id}:{target}')
    await request.app.state.hub.broadcast({'event':'fleet.changed','instance_id':instance_id})
    return {**result,'instance_id':instance_id}


@router.post('/workflows/{workflow_id}/activate')
async def activate(workflow_id: str,request: Request,instance_id: str=Query(min_length=1,max_length=128)) -> dict[str,Any]:
    resource_id(workflow_id)
    return await mutate(request,instance_id,'POST',f'/workflows/{workflow_id}/activate','fleet.workflow_activated',workflow_id)


@router.post('/workflows/{workflow_id}/deactivate')
async def deactivate(workflow_id: str,request: Request,instance_id: str=Query(min_length=1,max_length=128)) -> dict[str,Any]:
    resource_id(workflow_id)
    return await mutate(request,instance_id,'POST',f'/workflows/{workflow_id}/deactivate','fleet.workflow_deactivated',workflow_id)


@router.get('/executions')
async def executions(request: Request,instance_id: str=Query(min_length=1,max_length=128),limit: int=Query(default=25,ge=1,le=250),
                     cursor: str | None=Query(default=None,max_length=2048),workflow_id: str | None=Query(default=None,max_length=128),
                     status: Literal['canceled','crashed','error','new','running','success','unknown','waiting'] | None=None) -> dict[str,Any]:
    if workflow_id is not None:
        resource_id(workflow_id)
    params: dict[str,Any]={'limit':limit,'includeData':'false'}
    params.update({key:value for key,value in {'cursor':cursor,'workflowId':workflow_id,'status':status}.items() if value is not None})
    return list_view(await N8nClient(request).request(instance_id,'GET','/executions',params=params),instance_id)


@router.get('/executions/{execution_id}')
async def execution(execution_id: str,request: Request,instance_id: str=Query(min_length=1,max_length=128)) -> dict[str,Any]:
    resource_id(execution_id)
    return {**await N8nClient(request).request(instance_id,'GET',f'/executions/{execution_id}',params={'includeData':'true'}),'instance_id':instance_id}


@router.delete('/executions/{execution_id}')
async def delete_execution(execution_id: str,request: Request,instance_id: str=Query(min_length=1,max_length=128)) -> dict[str,Any]:
    resource_id(execution_id)
    return await mutate(request,instance_id,'DELETE',f'/executions/{execution_id}','fleet.execution_deleted',execution_id)


from .codelab import router as codelab_router
router.include_router(codelab_router)
