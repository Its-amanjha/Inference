"""Journaled n8n/runner deployment. Never retry an uncertain Docker write."""
import asyncio
import hashlib
import json
import secrets
import time
from copy import deepcopy
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.auth_gate import fail, require_current_role, require_role, token_hash
from backend.database import _settle
from backend.modules.n8n_proxy.client import instance_guard
from backend.modules.n8n_proxy.router import clean_url, check_literal_destination
from .bundle import BundleSpec, build
from .client import DockerGateway


class Provision(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    spec: BundleSpec
    instance_url: str = Field(min_length=1, max_length=2048)
    idempotency_key: str = Field(min_length=1, max_length=128)
    readiness_timeout: int = Field(default=90, ge=30, le=300)

    @field_validator('instance_url')
    @classmethod
    def valid_url(cls, value: str) -> str:
        return clean_url(value)


def encode(app: Any, state: dict[str, Any]) -> str:
    return json.dumps({'encrypted': app.state.vault.encrypt(json.dumps(state, sort_keys=True, allow_nan=False))})


def decode(app: Any, row: dict[str, Any]) -> dict[str, Any]:
    return json.loads(app.state.vault.decrypt(json.loads(row['value_json'])['encrypted']))


async def initialize(app: Any) -> None:
    app.state.docker_slots = asyncio.Semaphore(2)
    async with app.state.db.transaction() as tx:
        owner = await tx.fetchone("SELECT value_json FROM documents WHERE key='docker:owner'")
        if owner is None:
            if await tx.fetchone("SELECT key FROM documents WHERE key LIKE 'docker:bundle:%' LIMIT 1"):
                raise RuntimeError('Docker ownership record missing for existing bundles')
            value = uuid4().hex
            await tx.execute("INSERT INTO documents(key,value_json) VALUES('docker:owner',?)", (json.dumps({'id': value}),))
        else:
            value = json.loads(owner['value_json'])['id']
            if not isinstance(value, str) or len(value) != 32 or any(c not in '0123456789abcdef' for c in value):
                raise RuntimeError('Invalid Docker ownership record')
        app.state.docker_owner_id = value
        rows = await tx.fetchall("SELECT key,value_json FROM documents WHERE key LIKE 'docker:bundle:%'")
        for row in rows:
            state = decode(app, row)
            if state['status'] in {'deploying', 'starting', 'stopping', 'deleting'}:
                state['status'] = 'uncertain'
                state['diagnostic'] = 'The application restarted during an operation. Reconcile before further changes.'
                await tx.execute('UPDATE documents SET value_json=?,updated_at=CURRENT_TIMESTAMP WHERE key=?', (encode(app, state), row['key']))
                await tx.execute('INSERT INTO audit_events(actor,action,target,outcome) VALUES(?,?,?,?)',
                                 (state['actor'], 'docker.restarted', state['id'], 'uncertain'))


async def load(request: Request, bundle_id: str, tx: Any = None) -> dict[str, Any]:
    db = tx or request.app.state.db
    row = await db.fetchone('SELECT value_json FROM documents WHERE key=?', ('docker:bundle:' + bundle_id,))
    if not row:
        fail(404, 'bundle_not_found', 'The owned bundle was not found.')
    state = decode(request.app, row)
    if state.get('owner') != request.app.state.docker_owner_id or state.get('id') != bundle_id:
        fail(409, 'docker_ownership', 'The bundle ownership record does not match this installation.')
    return state


def view(state: dict[str, Any]) -> dict[str, Any]:
    return {key: state.get(key) for key in ('id', 'name', 'version', 'instance_id', 'instance_url', 'status', 'stage',
        'resources', 'warnings', 'diagnostic', 'observed', 'volume_retained', 'updated_at')}


async def save(request: Request, state: dict[str, Any], *, authorize: bool = False) -> None:
    state['updated_at'] = time.time()
    async with request.app.state.db.transaction() as tx:
        if authorize:
            await require_current_role(request, 'admin', tx)
        await tx.execute('UPDATE documents SET value_json=?,updated_at=CURRENT_TIMESTAMP WHERE key=?',
                         (encode(request.app, state), 'docker:bundle:' + state['id']))
        await tx.execute('INSERT INTO audit_events(actor,action,target,details_json,outcome) VALUES(?,?,?,?,?)',
                         (request.state.user['username'], 'docker.' + state['stage'], state['id'], '{}', state['status']))


async def stage(request: Request, state: dict[str, Any], name: str) -> None:
    state['stage'] = name
    await save(request, state, authorize=True)


async def wait_ready(gateway: DockerGateway, identifier: str, bundle_id: str, role: str, seconds: int) -> dict[str, Any]:
    async def poll() -> dict[str, Any]:
        while True:
            observed = await gateway.inspect_container(identifier, bundle_id, role)
            if observed.get('running') is True and observed.get('health') == 'healthy':
                return observed
            if observed.get('state') in {'dead', 'exited'} or observed.get('health') == 'unhealthy':
                fail(409, 'docker_not_ready', 'A bundle container failed its health check. Inspect the owned bundle before continuing.')
            await asyncio.sleep(1)
    try:
        return await asyncio.wait_for(poll(), timeout=seconds)
    except asyncio.TimeoutError:
        fail(409, 'docker_readiness_timeout', 'Containers started but did not become healthy before the readiness deadline.')


async def failure(request: Request, state: dict[str, Any], exc: BaseException) -> None:
    code = exc.detail.get('code', '') if isinstance(exc, HTTPException) and isinstance(exc.detail, dict) else ''
    state['status'] = 'partial' if isinstance(exc, HTTPException) and 'unknown' not in code else 'uncertain'
    state['diagnostic'] = 'Inspect and reconcile this bundle before further changes. Completed resources were retained.'
    await _settle(save(request, state))


async def provision(request: Request, body: Provision) -> dict[str, Any]:
    actor = require_role(request, 'admin')
    check_literal_destination(request, body.instance_url)
    fingerprint = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
    request_key = 'docker:request:' + hashlib.sha256((actor['username'] + ':' + body.idempotency_key).encode()).hexdigest()
    async with instance_guard(request, request_key), request.app.state.docker_slots:
        async with request.app.state.db.transaction() as tx:
            await require_current_role(request, 'admin', tx)
            previous = await tx.fetchone('SELECT value_json FROM documents WHERE key=?', (request_key,))
            if previous:
                record = json.loads(previous['value_json'])
                if record['fingerprint'] != fingerprint:
                    fail(409, 'idempotency_conflict', 'The idempotency key was used with different deployment settings.')
                return view(await load(request, record['id'], tx))
        gateway = DockerGateway(request)
        await gateway.status()  # Unavailable Docker must not create phantom instances.
        bundle_id, instance_id, token_id = uuid4().hex, uuid4().hex, uuid4().hex
        runner_token, telemetry_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        plan = build(body.spec, request.app.state.docker_owner_id, bundle_id, runner_token, telemetry_token)
        state = {'id': bundle_id, 'owner': request.app.state.docker_owner_id, 'actor': actor['username'],
            'name': body.spec.name, 'version': body.spec.version, 'instance_id': instance_id, 'instance_url': body.instance_url,
            'status': 'deploying', 'stage': 'staged', 'plan': plan, 'resources': {'containers': {}},
            'warnings': [*plan.get('warnings', []), 'Complete n8n owner setup and configure its public API key in Instances before fleet operations.'],
            'token_id': token_id, 'readiness_timeout': body.readiness_timeout, 'updated_at': time.time()}
        async with instance_guard(request, 'docker:bundle:' + bundle_id), instance_guard(request, instance_id):
            try:
                async with request.app.state.db.transaction() as tx:
                    await require_current_role(request, 'admin', tx)
                    await tx.execute('INSERT INTO instances(id,name,url,api_key_enc) VALUES(?,?,?,?)', (instance_id, body.spec.name, body.instance_url, ''))
                    await tx.execute('INSERT INTO telemetry_tokens(id,instance_id,name,token_hash) VALUES(?,?,?,?)',
                                     (token_id, instance_id, 'Docker bundle ' + body.spec.name, token_hash(telemetry_token)))
                    await tx.execute('INSERT INTO documents(key,value_json) VALUES(?,?)', ('docker:bundle:' + bundle_id, encode(request.app, state)))
                    await tx.execute('INSERT INTO documents(key,value_json) VALUES(?,?)', (request_key, json.dumps({'id': bundle_id, 'fingerprint': fingerprint})))
                    await tx.execute('INSERT INTO audit_events(actor,action,target,outcome) VALUES(?,?,?,?)', (actor['username'], 'docker.staged', bundle_id, 'intent'))
                for role in plan['order']:
                    await stage(request, state, 'image_' + role)
                    await gateway.ensure_image(plan['containers'][role]['config']['Image'])
                await stage(request, state, 'create_network')
                state['resources']['network'] = await gateway.create_network(plan['network'])
                await save(request, state)
                await stage(request, state, 'create_volume')
                state['resources']['volume'] = await gateway.create_volume(plan['volume'])
                await save(request, state)
                for role in plan['order']:
                    entry = plan['containers'][role]
                    network = await gateway.inspect_network(state['resources']['network'], bundle_id)
                    await gateway.inspect_volume(state['resources']['volume'], bundle_id)
                    config = deepcopy(entry['config'])
                    config['HostConfig']['NetworkMode'] = network['id']
                    config['NetworkingConfig']['EndpointsConfig'] = {network['id']: {'Aliases': [role]}}
                    await stage(request, state, 'create_' + role)
                    identifier = await gateway.create_container(entry['name'], config)
                    state['resources']['containers'][role] = identifier
                    await save(request, state)
                    await stage(request, state, 'start_' + role)
                    await gateway.start_container(identifier, bundle_id, role)
                    await stage(request, state, 'readiness_' + role)
                    await wait_ready(gateway, identifier, bundle_id, role, body.readiness_timeout)
                state['status'], state['stage'] = 'ready', 'complete'
                await save(request, state)
            except BaseException as exc:
                await failure(request, state, exc)
                raise
        await request.app.state.hub.broadcast({'event': 'docker.changed', 'bundle_id': bundle_id, 'status': state['status']})
        return view(state)


async def inspect_resources(request: Request, state: dict[str, Any]) -> dict[str, Any]:
    gateway = DockerGateway(request)
    observed: dict[str, Any] = {'containers': {}}
    async def inspect(method: Any, *args: Any) -> Any:
        try:
            return await method(*args)
        except HTTPException as exc:
            if exc.status_code == 404:
                return None
            raise
    for role, entry in state['plan']['containers'].items():
        identifier = state['resources']['containers'].get(role, entry['name'])
        observed['containers'][role] = await inspect(gateway.inspect_container, identifier, state['id'], role)
    for kind in ('network', 'volume'):
        identifier = state['resources'].get(kind, state['plan'][kind]['Name'])
        observed[kind] = await inspect(getattr(gateway, 'inspect_' + kind), identifier, state['id'])
    return observed


async def reconcile(request: Request, bundle_id: str) -> dict[str, Any]:
    require_role(request, 'admin')
    async with instance_guard(request, 'docker:bundle:' + bundle_id):
        state = await load(request, bundle_id)
        observed = await inspect_resources(request, state)
        state['observed'] = observed
        for role, info in observed['containers'].items():
            if info:
                state['resources']['containers'][role] = info['id']
            else:
                state['resources']['containers'].pop(role, None)
        for kind in ('network', 'volume'):
            if observed[kind]:
                state['resources'][kind] = observed[kind]['id']
            else:
                state['resources'].pop(kind, None)
        containers = list(observed['containers'].values())
        complete = bool(observed['network'] and observed['volume'])
        if complete and all(info and info['running'] and info['health'] == 'healthy' for info in containers):
            state['status'] = 'ready'
        elif complete and all(info and not info['running'] for info in containers):
            state['status'] = 'stopped'
        elif not any(containers) and not observed['network']:
            state['status'] = 'removed'
            state['volume_retained'] = bool(observed['volume'])
        else:
            state['status'] = 'partial'
        state['stage'] = 'reconciled'
        state['diagnostic'] = 'Observed current owned resources; no remote operation was retried.'
        await save(request, state, authorize=True)
        return view(state)


async def lifecycle(request: Request, bundle_id: str, operation: str, purge_data: bool = False) -> dict[str, Any]:
    require_role(request, 'admin')
    async with instance_guard(request, 'docker:bundle:' + bundle_id), request.app.state.docker_slots:
        state = await load(request, bundle_id)
        if state['status'] == 'uncertain':
            fail(409, 'docker_reconcile_required', 'Reconcile the uncertain bundle before another operation.')
        observed = await inspect_resources(request, state)  # Validate every label before effects.
        gateway = DockerGateway(request)
        if operation == 'start' and not (all(observed['containers'].values()) and observed['network'] and observed['volume']):
            fail(409, 'docker_incomplete_bundle', 'An incomplete bundle cannot be started. Reconcile and remove it explicitly.')
        state['status'] = {'start': 'starting', 'stop': 'stopping', 'delete': 'deleting'}[operation]
        try:
            order = state['plan']['order'] if operation == 'start' else list(reversed(state['plan']['order']))
            for role in order:
                info = observed['containers'][role]
                if not info:
                    continue
                await stage(request, state, operation + '_' + role)
                if operation == 'start':
                    await gateway.start_container(info['id'], bundle_id, role)
                    await wait_ready(gateway, info['id'], bundle_id, role, state['readiness_timeout'])
                else:
                    await gateway.stop_container(info['id'], bundle_id, role)
                    if operation == 'delete':
                        await stage(request, state, 'delete_' + role)
                        await gateway.delete_container(info['id'], bundle_id, role)
                        state['resources']['containers'].pop(role, None)
                        await save(request, state)
            if operation == 'delete':
                for kind in ('network', 'volume'):
                    if observed[kind] and (kind != 'volume' or purge_data):
                        await stage(request, state, 'delete_' + kind)
                        await getattr(gateway, 'delete_' + kind)(observed[kind]['id'], bundle_id)
                        state['resources'].pop(kind, None)
                        await save(request, state)
                state['volume_retained'] = bool(observed['volume']) and not purge_data
                async with request.app.state.db.transaction() as tx:
                    await require_current_role(request, 'admin', tx)
                    await tx.execute('UPDATE telemetry_tokens SET revoked_at=CURRENT_TIMESTAMP WHERE id=?', (state['token_id'],))
            state['status'] = {'start': 'ready', 'stop': 'stopped', 'delete': 'removed'}[operation]
            state['stage'], state['diagnostic'] = 'complete', None
            await save(request, state)
        except BaseException as exc:
            await failure(request, state, exc)
            raise
        await request.app.state.hub.broadcast({'event': 'docker.changed', 'bundle_id': bundle_id, 'status': state['status']})
        return view(state)
