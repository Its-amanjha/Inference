"""Admin-only APIs for this installation's persisted Docker bundles."""
from typing import Any, Literal

from fastapi import APIRouter, Query, Request

from backend.auth_gate import require_current_role, require_role
from .client import DockerGateway
from . import deployer

router = APIRouter()


@router.get('/status')
async def status(request: Request) -> dict[str, Any]:
    require_role(request, 'admin')
    return await DockerGateway(request).status()


@router.get('/bundles')
async def bundles(request: Request) -> dict[str, Any]:
    async with request.app.state.db.transaction() as tx:
        await require_current_role(request, 'admin', tx)
        rows = await tx.fetchall("SELECT key FROM documents WHERE key LIKE 'docker:bundle:%' ORDER BY updated_at DESC,key LIMIT 100")
        data = [deployer.view(await deployer.load(request, row['key'].split(':')[-1], tx)) for row in rows]
    return {'data': data}


@router.get('/containers')
async def containers(request: Request) -> dict[str, Any]:
    require_role(request, 'admin')
    inventory = await DockerGateway(request).inventory()
    async with request.app.state.db.transaction() as tx:
        await require_current_role(request, 'admin', tx)
        rows = await tx.fetchall("SELECT key FROM documents WHERE key LIKE 'docker:bundle:%'")
        owned = {row['key'].split(':')[-1] for row in rows}
    return {'data': [item for item in inventory if item['bundle_id'] in owned]}


@router.post('/bundles')
async def provision(body: deployer.Provision, request: Request) -> dict[str, Any]:
    return await deployer.provision(request, body)


@router.get('/bundles/{bundle_id}')
async def bundle(bundle_id: str, request: Request) -> dict[str, Any]:
    async with request.app.state.db.transaction() as tx:
        await require_current_role(request, 'admin', tx)
        state = await deployer.load(request, bundle_id, tx)
    return deployer.view(state)


@router.post('/bundles/{bundle_id}/reconcile')
async def reconcile(bundle_id: str, request: Request) -> dict[str, Any]:
    return await deployer.reconcile(request, bundle_id)


@router.post('/bundles/{bundle_id}/{operation}')
async def lifecycle(bundle_id: str, operation: Literal['start', 'stop'], request: Request) -> dict[str, Any]:
    return await deployer.lifecycle(request, bundle_id, operation)


@router.delete('/bundles/{bundle_id}')
async def remove(bundle_id: str, request: Request, purge_data: bool = Query(default=False)) -> dict[str, Any]:
    return await deployer.lifecycle(request, bundle_id, 'delete', purge_data=purge_data)
