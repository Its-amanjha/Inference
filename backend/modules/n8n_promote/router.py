"""Stale-safe previews and journaled remote effects. No automatic write retries."""
import asyncio
from contextlib import asynccontextmanager, AsyncExitStack
import json
import time
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.auth_gate import fail, require_current_role, require_role
from backend.database import _settle
from backend.modules.n8n_credentials import service as credentials
from backend.modules.n8n_proxy.client import N8nClient, instance_guard
from backend.modules.n8n_proxy.router import instance_row, resource_id
from .transform import fingerprint, prepare

router=APIRouter()
IN_FLIGHT=('validating','provisioning','writing','activating','restoring')


class Preflight(BaseModel):
    model_config=ConfigDict(extra='forbid')
    source_instance_id: str=Field(min_length=1,max_length=128)
    target_instance_id: str=Field(min_length=1,max_length=128)
    workflow_id: str=Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')
    credential_map: dict[str,str]=Field(default_factory=dict,max_length=100)
    secret_map: dict[str,str]=Field(default_factory=dict,max_length=100)
    subworkflow_map: dict[str,str]=Field(default_factory=dict,max_length=100)

    @field_validator('credential_map','secret_map','subworkflow_map')
    @classmethod
    def bounded_maps(cls,value):
        if any(not k or len(k)>128 or not v or len(v)>256 for k,v in value.items()):
            raise ValueError('Mapping identifiers must be nonempty and bounded')
        return value


class Deploy(BaseModel):
    model_config=ConfigDict(extra='forbid')
    preview_id: str=Field(pattern=r'^[a-f0-9]{32}$')
    idempotency_key: str=Field(min_length=1,max_length=128)


class Reconcile(BaseModel):
    model_config=ConfigDict(extra='forbid')
    target_workflow_id: str | None=Field(default=None,pattern=r'^[A-Za-z0-9_-]{1,128}$')
    credential_map: dict[str,str]=Field(default_factory=dict,max_length=100)


def encode(request,value):
    return request.app.state.vault.encrypt(json.dumps(value,sort_keys=True,allow_nan=False))


def decode(request,value):
    return json.loads(request.app.state.vault.decrypt(value))


def writable(workflow):
    return {key:workflow.get(key,{} if key in ('settings','connections') else None) for key in ('name','nodes','connections','settings')}


def workflow_diff(before,after):
    old={node['name']:node for node in (before or {}).get('nodes',[])}
    new={node['name']:node for node in after['nodes']}
    return {'operation':'update' if before else 'create','changed':fingerprint(writable(before))!=fingerprint(after) if before else True,
            'nodes_added':sorted(new.keys()-old.keys()),'nodes_removed':sorted(old.keys()-new.keys()),
            'nodes_changed':[{'name':name,'fields':sorted(key for key in old[name].keys()|new[name].keys() if old[name].get(key)!=new[name].get(key))} for name in sorted(new.keys()&old.keys()) if old[name]!=new[name]],
            'connections_changed':(before or {}).get('connections',{})!=after['connections'],
            'settings_changed':(before or {}).get('settings',{})!=after['settings']}


@asynccontextmanager
async def connections(request,source,target):
    async with AsyncExitStack() as stack:
        for identifier in sorted({source,target}):
            await stack.enter_async_context(instance_guard(request,identifier))
        yield


async def maps(request,source,target):
    result={}
    for table in ('credential_mappings','workflow_mappings'):
        result[table]=await request.app.state.db.fetchall(f'SELECT * FROM {table} WHERE source_instance_id=? AND target_instance_id=? ORDER BY key',(source,target))
    return result


async def config_hash(request,source,target):
    rows=[await instance_row(request,identifier) for identifier in (source,target)]
    return fingerprint([{'id':row['id'],'url':row['url'],'key_hash':fingerprint(request.app.state.vault.decrypt(row['api_key_enc']))} for row in rows])


def safe_transform(source,credential_map,subworkflow_map):
    try:
        return prepare(source,credential_map,subworkflow_map)
    except (ValueError,TypeError,RecursionError):
        fail(422,'unsupported_workflow','The workflow contains malformed or unsupported references.')


async def check_bindings(request,target,required,mapping,source=None):
    # Operator can use admin-established mappings without credential enumeration rights.
    if require_role(request,'operator')['role']!='admin':
        for item in required:
            row=await request.app.state.db.fetchone('SELECT * FROM credential_mappings WHERE source_instance_id=? AND target_instance_id=? AND source_credential_id=?',(source,target,item['id']))
            if not row or row['credential_type']!=item['type'] or row['target_credential_id']!=mapping.get(item['id']):
                fail(409,'credential_binding_invalid','An administrator must establish a mapping for this credential type.')
        return [{'code':'credential_verification_unavailable','message':'An administrator must verify target credential access.'}]
    found=await credentials.discover(request,target)
    if not found['supported']:
        return found['diagnostics']
    available={str(item['id']):item for item in found['data']}
    for item in required:
        identifier=mapping.get(item['id'])
        if identifier and (identifier not in available or available[identifier]['type']!=item['type']):
            fail(409,'credential_binding_invalid','A mapped target credential is missing or has the wrong type.')
    return found['diagnostics']


@router.post('/preflight')
async def preflight(body: Preflight,request: Request):
    actor=require_role(request,'operator')
    if body.source_instance_id==body.target_instance_id:
        fail(422,'same_instance','Choose distinct source and target instances.')
    if body.secret_map:
        require_role(request,'admin')
    if not request.app.state.limiter.hit('preflight:'+actor['username'],limit=20,window=60):
        fail(429,'rate_limited','Wait before creating another preview.')
    source_id,target_id=body.source_instance_id,body.target_instance_id
    async with connections(request,source_id,target_id):
        config=await config_hash(request,source_id,target_id)
        stored=await maps(request,source_id,target_id)
        cmap={row['source_credential_id']:row['target_credential_id'] for row in stored['credential_mappings']}
        if actor['role']!='admin' and any(cmap.get(k)!=v for k,v in body.credential_map.items()):
            fail(403,'credential_mapping_requires_admin','An administrator must establish credential mappings first.')
        cmap.update(body.credential_map)
        smap={row['source_workflow_id']:row['target_workflow_id'] for row in stored['workflow_mappings']}
        smap.update(body.subworkflow_map)
        client=N8nClient(request)
        source=await client.request(source_id,'GET',f'/workflows/{body.workflow_id}')
        target_workflow_id=next((row['target_workflow_id'] for row in stored['workflow_mappings'] if row['source_workflow_id']==body.workflow_id),None)
        previous=await client.request(target_id,'GET',f'/workflows/{target_workflow_id}') if target_workflow_id else None
        base=safe_transform(source,cmap,smap)
        required=base['required_credentials']
        required_ids={item['id'] for item in required}
        if set(body.secret_map)-required_ids or set(body.secret_map)&set(cmap):
            fail(422,'conflicting_mapping','Secret provisioning must reference an unmapped source credential.')
        for item in required:
            saved=next((row for row in stored['credential_mappings'] if row['source_credential_id']==item['id']),None)
            if saved and item['id'] not in body.credential_map and saved['credential_type']!=item['type']:
                fail(409,'credential_binding_invalid','A persisted credential mapping has a different source type.')
        warnings=await check_bindings(request,target_id,required,cmap,source_id) if required else []
        secret_hashes={}
        for item in required:
            if item['id'] in body.secret_map:
                data=await credentials.validate_secret(request,target_id,item['type'],body.secret_map[item['id']])
                secret_hashes[item['id']]=fingerprint(data)
                cmap[item['id']]='pending-'+fingerprint(item['id'])[:16]
        transformed=safe_transform(source,cmap,smap)
        subworkflow_hashes={}
        for entry in transformed['mapped']:
            if entry['kind']=='workflow':
                resource_id(entry['target_id'])
                remote=await client.request(target_id,'GET',f"/workflows/{entry['target_id']}")
                subworkflow_hashes[entry['target_id']]=fingerprint(remote)
        missing=transformed['missing']
        if previous and previous.get('active') is not False:
            missing=[*missing,{'code':'target_active','message':'Explicitly deactivate the target before promotion.'}]
        identifier=uuid4().hex
        state={'id':identifier,'actor':actor['username'],'expires_at':time.time()+900,'body':body.model_dump(),
               'config_hash':config,'mapping_hash':fingerprint(stored),'original_mappings':stored,'source_hash':fingerprint(source),
               'target_hash':fingerprint(previous),'target_workflow_id':target_workflow_id,'previous':previous,
               'source':source,'credential_map':{k:v for k,v in cmap.items() if k not in body.secret_map},
               'subworkflow_map':smap,'subworkflow_hashes':subworkflow_hashes,'secret_hashes':secret_hashes,'required_credentials':required,
               'can_deploy':not missing and not transformed['collisions']}
        async with request.app.state.db.transaction() as tx:
            await require_current_role(request,'admin' if body.secret_map else 'operator',tx)
            await tx.execute('INSERT INTO documents(key,value_json) VALUES(?,?)',('promotion_preview:'+identifier,json.dumps({'encrypted':encode(request,state)})))
    return {'preview_id':identifier,'expires_at':state['expires_at'],'source_hash':state['source_hash'],'target_hash':state['target_hash'],
            'diff':workflow_diff(previous,transformed['workflow']),
            'mapped':transformed['mapped'],'missing':missing,'collisions':transformed['collisions'],
            'warnings':warnings+transformed['warnings'],'can_deploy':state['can_deploy']}


def public_run(row,state):
    status=row['status']
    # Source instance and workflow let a workflow's page list its own promotions.
    return {'id':row['id'],'preview_id':row['preview_id'],'status':status,'created_at':row.get('created_at'),
            'source_instance_id':state['body'].get('source_instance_id'),'workflow_id':state['body'].get('workflow_id'),
            'target_instance_id':state['body']['target_instance_id'],
            'target_workflow_id':state.get('target_workflow_id'),'created_credentials':state.get('created_credentials',[]),
            'error_code':state.get('error_code'),'pending_credential':state.get('pending_credential'),
            'recovery_actions':(['reconcile','new_preview_with_explicit_mappings'] if status in (*IN_FLIGHT,'uncertain','partial') else ['activate','restore'] if status=='deployed' and state.get('previous') else ['activate'] if status=='deployed' else [])}


async def load_run(request,identifier):
    actor=require_role(request,'operator')
    row=await request.app.state.db.fetchone('SELECT * FROM promotion_runs WHERE id=?',(identifier,))
    if not row:
        fail(404,'run_not_found','Promotion run was not found.')
    if row['actor']!=actor['username']:
        fail(403,'wrong_actor','This promotion belongs to another user.')
    return row,decode(request,row['state_enc'])


async def save_run(request,identifier,state,status):
    async with request.app.state.db.transaction() as tx:
        await tx.execute("UPDATE promotion_runs SET status=?,state_enc=?,updated_at=datetime('now') WHERE id=?",(status,encode(request,state),identifier))
        await tx.execute('INSERT INTO audit_events(actor,action,target,details_json,outcome) VALUES(?,?,?,?,?)',(state['actor'],'promotion.'+status,identifier,'{}','intent' if status in IN_FLIGHT else status))


async def stage(request,identifier,state,status):
    await save_run(request,identifier,state,status)


async def complete_mapping(request,identifier,state,status='deployed'):
    body=state['body']
    async with request.app.state.db.transaction() as tx:
        await require_current_role(request,'operator',tx)
        for item in state['required_credentials']:
            target_id=state['credential_map'].get(item['id'])
            if target_id:
                existing=await tx.fetchone('SELECT * FROM credential_mappings WHERE key=?',(credentials.mapping_key(body['source_instance_id'],body['target_instance_id'],item['id']),))
                original=next((entry for entry in state['original_mappings']['credential_mappings'] if entry['source_credential_id']==item['id']),None)
                if existing and existing!=original and (existing['target_credential_id']!=target_id or existing['credential_type']!=item['type']):
                    fail(409,'mapping_changed','A credential mapping changed; reconcile it explicitly.')
                await tx.execute('INSERT INTO credential_mappings(key,source_instance_id,target_instance_id,source_credential_id,target_credential_id,credential_type,target_name) VALUES(?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET target_credential_id=excluded.target_credential_id,credential_type=excluded.credential_type,target_name=excluded.target_name',
                    (credentials.mapping_key(body['source_instance_id'],body['target_instance_id'],item['id']),body['source_instance_id'],body['target_instance_id'],item['id'],target_id,item['type'],item['name']))
        key=credentials.mapping_key(body['source_instance_id'],body['target_instance_id'],body['workflow_id'])
        existing=await tx.fetchone('SELECT * FROM workflow_mappings WHERE key=?',(key,))
        original=next((entry for entry in state['original_mappings']['workflow_mappings'] if entry['source_workflow_id']==body['workflow_id']),None)
        if existing and existing!=original and existing['target_workflow_id']!=state['target_workflow_id']:
            fail(409,'mapping_changed','Another run changed the workflow mapping. No mapping was overwritten.')
        await tx.execute('INSERT INTO workflow_mappings(key,source_instance_id,target_instance_id,source_workflow_id,target_workflow_id) VALUES(?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET target_workflow_id=excluded.target_workflow_id',
                         (key,body['source_instance_id'],body['target_instance_id'],body['workflow_id'],state['target_workflow_id']))
        await tx.execute("UPDATE promotion_runs SET status=?,state_enc=?,updated_at=datetime('now') WHERE id=?",(status,encode(request,state),identifier))
        await tx.execute('INSERT INTO audit_events(actor,action,target,details_json,outcome) VALUES(?,?,?,?,?)',(state['actor'],'promotion.'+status,identifier,'{}',status))


@router.post('/deploy')
async def deploy(body: Deploy,request: Request):
    actor=require_role(request,'operator')
    key=fingerprint([actor['username'],body.idempotency_key])
    async with instance_guard(request,'promotion:'+body.preview_id):
        existing=await request.app.state.db.fetchone('SELECT * FROM promotion_runs WHERE idempotency_key=?',(key,))
        if existing:
            if existing['preview_id']!=body.preview_id:
                fail(409,'idempotency_conflict','This key belongs to another preview.')
            return public_run(existing,decode(request,existing['state_enc']))
        preview=await request.app.state.db.fetchone('SELECT value_json FROM documents WHERE key=?',('promotion_preview:'+body.preview_id,))
        if not preview:
            fail(404,'preview_not_found','Preview was not found.')
        state=decode(request,json.loads(preview['value_json'])['encrypted'])
        if state['actor']!=actor['username']:
            fail(403,'wrong_actor','This preview belongs to another user.')
        if state.get('run_id'):
            fail(409,'preview_consumed','This preview already has a deployment run.')
        if state['expires_at']<time.time() or not state['can_deploy']:
            fail(409,'preview_not_deployable','Create a fresh preview and resolve its blockers.')
        identifier=uuid4().hex
        state.update(run_id=identifier,created_credentials=[])
        async with request.app.state.db.transaction() as tx:
            await require_current_role(request,'admin' if state['body']['secret_map'] else 'operator',tx)
            if await tx.fetchone('SELECT id FROM promotion_runs WHERE idempotency_key=?',(key,)):
                fail(409,'idempotency_conflict','This key belongs to another preview.')
            await tx.execute('INSERT INTO promotion_runs(id,actor,preview_id,idempotency_key,status,state_enc) VALUES(?,?,?,?,?,?)',(identifier,actor['username'],body.preview_id,key,'validating',encode(request,state)))
            await tx.execute('UPDATE documents SET value_json=? WHERE key=?',(json.dumps({'encrypted':encode(request,state)}),'promotion_preview:'+body.preview_id))
        async with instance_guard(request,'run:'+identifier):
            await execute(request,identifier,state)
        row,state=await load_run(request,identifier)
        return public_run(row,state)


async def execute(request,identifier,state):
    body=state['body']
    source_id,target_id=body['source_instance_id'],body['target_instance_id']
    effect=False
    status='validating'
    try:
        async with connections(request,source_id,target_id):
            client=N8nClient(request)
            source=await client.request(source_id,'GET',f"/workflows/{body['workflow_id']}")
            previous=await client.request(target_id,'GET',f"/workflows/{state['target_workflow_id']}") if state['target_workflow_id'] else None
            if (fingerprint(source)!=state['source_hash'] or fingerprint(previous)!=state['target_hash'] or
                await config_hash(request,source_id,target_id)!=state['config_hash'] or
                fingerprint(await maps(request,source_id,target_id))!=state['mapping_hash']):
                fail(409,'stale_preview','Source, target or mappings changed. Create a fresh preview.')
            if previous and previous.get('active') is not False:
                fail(409,'target_active','Deactivate the target before promotion.')
            cmap=state['credential_map'].copy()
            if state['required_credentials']:
                await check_bindings(request,target_id,state['required_credentials'],cmap,source_id)
            for remote_id,expected in state['subworkflow_hashes'].items():
                if fingerprint(await client.request(target_id,'GET',f'/workflows/{remote_id}'))!=expected:
                    fail(409,'stale_preview','A target subworkflow changed after preview.')
            # Resolve and validate every secret before the first remote effect.
            for item in state['required_credentials']:
                if item['id'] in body['secret_map']:
                    data=await credentials.validate_secret(request,target_id,item['type'],body['secret_map'][item['id']])
                    if fingerprint(data)!=state['secret_hashes'][item['id']]:
                        fail(409,'stale_preview','A secret changed after preview.')
            for item in state['required_credentials']:
                if item['id'] not in body['secret_map']:
                    continue
                state['pending_credential']=item
                status='provisioning'
                await stage(request,identifier,state,status)
                effect=True
                created=await credentials.provision(request,target_id,item['type'],item['name'] or item['type'],body['secret_map'][item['id']],expected_hash=state['secret_hashes'][item['id']])
                cmap[item['id']]=created['id']
                state['created_credentials'].append({'source_id':item['id'],**created})
                state['credential_map']=cmap.copy()
                state.pop('pending_credential',None)
                await save_run(request,identifier,state,status)
            transformed=safe_transform(source,cmap,state['subworkflow_map'])
            if transformed['missing'] or transformed['collisions']:
                fail(409,'unresolved_references','Workflow references are unresolved.')
            state['desired']=transformed['workflow']
            state['pending_action']='deploy'
            # Provisioning can take time; check remote snapshots again before writing.
            latest_source=await client.request(source_id,'GET',f"/workflows/{body['workflow_id']}")
            latest_target=await client.request(target_id,'GET',f"/workflows/{state['target_workflow_id']}") if previous else None
            if fingerprint(latest_source)!=state['source_hash'] or fingerprint(latest_target)!=state['target_hash']:
                fail(409,'stale_preview','A workflow changed while preparing the deployment.')
            status='writing'
            await stage(request,identifier,state,status)
            effect=True
            result=await client.request(target_id,'PUT' if previous else 'POST',f"/workflows/{state['target_workflow_id']}" if previous else '/workflows',json=state['desired'])
            remote_id=result.get('id')
            if not isinstance(remote_id,str):
                fail(502,'upstream_outcome_unknown','The workflow write returned no target ID.')
            resource_id(remote_id)
            if previous and remote_id!=state['target_workflow_id']:
                fail(502,'upstream_outcome_unknown','The response did not identify the expected workflow.')
            state['target_workflow_id']=remote_id
            await save_run(request,identifier,state,status)
            actual=await client.request(target_id,'GET',f'/workflows/{remote_id}')
            if actual.get('active') is not False or fingerprint(writable(actual))!=fingerprint(state['desired']):
                fail(502,'upstream_outcome_unknown','The written workflow could not be verified inactive.')
            state['deployed_hash']=fingerprint(actual)
            await complete_mapping(request,identifier,state)
            status='deployed'
    except HTTPException as exc:
        code=exc.detail.get('code','promotion_failed') if isinstance(exc.detail,dict) else 'promotion_failed'
        state['error_code']=code
        final='uncertain' if code=='upstream_outcome_unknown' or status=='writing' else 'partial' if effect else 'rejected'
        await _settle(save_run(request,identifier,state,final))
    except BaseException:
        state['error_code']='interrupted'
        await _settle(save_run(request,identifier,state,'uncertain' if effect else 'rejected'))
        raise


@router.get('/runs')
async def runs(request: Request,limit: int=Query(default=50,ge=1,le=100)):
    actor=require_role(request,'operator')
    rows=await request.app.state.db.fetchall('SELECT * FROM promotion_runs WHERE actor=? ORDER BY created_at DESC,id DESC LIMIT ?',(actor['username'],limit))
    return {'data':[public_run(row,decode(request,row['state_enc'])) for row in rows]}


@router.get('/runs/{identifier}')
async def run(identifier: str,request: Request):
    row,state=await load_run(request,identifier)
    return public_run(row,state)


@router.get('/runs/{identifier}/previous')
async def previous(identifier: str,request: Request):
    _,state=await load_run(request,identifier)
    if not state.get('previous'):
        fail(404,'no_previous_version','This deployment has no previous target snapshot.')
    return {'workflow':state['previous'],'target_instance_id':state['body']['target_instance_id']}


@router.post('/runs/{identifier}/reconcile')
async def reconcile(identifier: str,body: Reconcile,request: Request):
    async with instance_guard(request,'run:'+identifier):
        row,state=await load_run(request,identifier)
        if row['status'] not in (*IN_FLIGHT,'uncertain','partial'):
            return public_run(row,state)
        source,target=state['body']['source_instance_id'],state['body']['target_instance_id']
        async with connections(request,source,target):
            if await config_hash(request,source,target)!=state['config_hash']:
                fail(409,'instance_changed','Restore the original connection settings before reconciliation.')
            pending=state.get('pending_credential')
            if pending and body.credential_map:
                require_role(request,'admin')
                if set(body.credential_map)!={pending['id']}:
                    fail(422,'invalid_reconciliation','Supply only the pending source credential mapping.')
                mapped=body.credential_map[pending['id']]
                resource_id(mapped)
                found=await credentials.discover(request,target)
                if not found['supported'] or not any(item['id']==mapped and item['type']==pending['type'] for item in found['data']):
                    fail(409,'credential_binding_invalid','The supplied credential ID and type could not be verified.')
                state['credential_map'][pending['id']]=mapped
                state['created_credentials'].append({'source_id':pending['id'],'id':mapped,'type':pending['type'],'name':pending['name']})
                state.pop('pending_credential',None)
                state['error_code']='new_preview_required'
                async with request.app.state.db.transaction() as tx:
                    await require_current_role(request,'admin',tx)
                await save_run(request,identifier,state,'partial')
                return public_run({**row,'status':'partial'},state)
            target_id=state.get('target_workflow_id') or body.target_workflow_id
            if not target_id or not state.get('desired'):
                fail(409,'manual_recovery_required','Inspect residual credentials and create a new preview with explicit mappings. No remote write will be retried.')
            actual=await N8nClient(request).request(target,'GET',f'/workflows/{target_id}')
            action=state.get('pending_action','deploy')
            expected=writable(state['previous']) if action=='restore' else state['desired']
            if actual.get('active') is not (action=='activate') or fingerprint(writable(actual))!=fingerprint(expected):
                fail(409,'reconciliation_mismatch','The selected workflow does not match the expected inactive deployment.')
            state.update(target_workflow_id=target_id,deployed_hash=fingerprint(actual))
            state.pop('error_code',None)
            async with request.app.state.db.transaction() as tx:
                await require_current_role(request,'operator',tx)
            if action=='deploy':
                await complete_mapping(request,identifier,state)
            else:
                await save_run(request,identifier,state,'activated' if action=='activate' else 'restored')
        row,state=await load_run(request,identifier)
        return public_run(row,state)


@router.post('/runs/{identifier}/activate')
async def activate(identifier: str,request: Request):
    return await final_action(request,identifier,'activate')


@router.post('/runs/{identifier}/restore')
async def restore(identifier: str,request: Request):
    return await final_action(request,identifier,'restore')


async def final_action(request,identifier,action):
    async with instance_guard(request,'run:'+identifier):
        row,state=await load_run(request,identifier)
        if row['status']!='deployed':
            fail(409,'run_not_deployed','Reconcile the deployment before changing its state.')
        source,target=state['body']['source_instance_id'],state['body']['target_instance_id']
        async with connections(request,source,target):
            if await config_hash(request,source,target)!=state['config_hash']:
                fail(409,'instance_changed','Connection settings changed after deployment.')
            client=N8nClient(request)
            remote_id=state['target_workflow_id']
            actual=await client.request(target,'GET',f'/workflows/{remote_id}')
            if actual.get('active') is not False or fingerprint(actual)!=state['deployed_hash']:
                fail(409,'stale_target','The target changed after deployment. No action was sent.')
            if action=='restore' and not state.get('previous'):
                fail(409,'no_previous_version','This run created a workflow; there is no previous version to restore.')
            if action=='activate' and state['required_credentials']:
                diagnostics=await check_bindings(request,target,state['required_credentials'],state['credential_map'],source)
                if diagnostics:
                    fail(409,'activation_verification_unavailable','An administrator with credential discovery access must verify bindings before activation.')
                if fingerprint(await client.request(target,'GET',f'/workflows/{remote_id}'))!=state['deployed_hash']:
                    fail(409,'stale_target','The target changed while checking credential bindings.')
            state['pending_action']=action
            await stage(request,identifier,state,'activating' if action=='activate' else 'restoring')
            accepted=False
            try:
                await client.request(target,'POST' if action=='activate' else 'PUT',f'/workflows/{remote_id}/activate' if action=='activate' else f'/workflows/{remote_id}',**({} if action=='activate' else {'json':writable(state['previous'])}))
                accepted=True
                confirmed=await client.request(target,'GET',f'/workflows/{remote_id}')
                valid=confirmed.get('active') is True if action=='activate' else confirmed.get('active') is False and fingerprint(writable(confirmed))==fingerprint(writable(state['previous']))
                if not valid:
                    fail(502,'upstream_outcome_unknown','The remote action could not be verified.')
                state.pop('error_code',None)
                await _settle(save_run(request,identifier,state,'activated' if action=='activate' else 'restored'))
            except HTTPException as exc:
                code=exc.detail.get('code','upstream_outcome_unknown') if isinstance(exc.detail,dict) else 'upstream_outcome_unknown'
                definite=not accepted and code in ('upstream_rejected','upstream_auth_failed','upstream_forbidden','upstream_not_found','upstream_rate_limited','permission_denied','session_revoked','authentication_required','session_expired')
                state['error_code']=code
                await _settle(save_run(request,identifier,state,'deployed' if definite else 'uncertain'))
                raise
            except BaseException:
                state['error_code']='upstream_outcome_unknown'
                await _settle(save_run(request,identifier,state,'uncertain'))
                raise
        row,state=await load_run(request,identifier)
        return public_run(row,state)
