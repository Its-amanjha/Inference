"""Admin-only credential metadata and explicit identity mappings."""
from contextlib import AsyncExitStack
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from backend import audit
from backend.auth_gate import fail, require_current_role, require_role
from backend.modules.n8n_proxy.client import instance_guard
from . import service

router = APIRouter()


class Mapping(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_instance_id: str = Field(pattern=service.IDENTIFIER)
    target_instance_id: str = Field(pattern=service.IDENTIFIER)
    source_credential_id: str = Field(pattern=service.IDENTIFIER)
    target_credential_id: str = Field(pattern=service.IDENTIFIER)
    credential_type: str = Field(pattern=service.IDENTIFIER)
    target_name: str = Field(min_length=1, max_length=256)


@router.get('')
@router.get('/')
async def credentials(request: Request, instance_id: str = Query(pattern=service.IDENTIFIER)) -> dict[str, Any]:
    require_role(request, 'admin')
    return await service.discover(request, instance_id)


@router.get('/schema/{credential_type}')
async def credential_schema(credential_type: str, request: Request, instance_id: str = Query(pattern=service.IDENTIFIER)) -> dict[str, Any]:
    require_role(request, 'admin')
    return await service.schema(request, instance_id, credential_type)


@router.get('/mappings')
async def mappings(request: Request, source_instance_id: str = Query(pattern=service.IDENTIFIER), target_instance_id: str = Query(pattern=service.IDENTIFIER)) -> dict[str, Any]:
    require_role(request, 'admin')
    return {'data': await request.app.state.db.fetchall('SELECT * FROM credential_mappings WHERE source_instance_id=? AND target_instance_id=? ORDER BY source_credential_id', (source_instance_id, target_instance_id))}


@router.put('/mappings')
async def save_mapping(body: Mapping, request: Request) -> dict[str, Any]:
    require_role(request, 'admin')
    if body.source_instance_id == body.target_instance_id:
        fail(422, 'same_instance', 'Source and target instances must differ.')
    diagnostics = []
    async with AsyncExitStack() as stack:
        for identifier in sorted((body.source_instance_id, body.target_instance_id)):
            await stack.enter_async_context(instance_guard(request, identifier))
        for instance_id, credential_id in ((body.source_instance_id, body.source_credential_id), (body.target_instance_id, body.target_credential_id)):
            metadata = await service.discover(request, instance_id)
            diagnostics.extend(metadata['diagnostics'])
            if metadata['supported'] and not any(item['id'] == credential_id and item['type'] == body.credential_type for item in metadata['data']):
                fail(422, 'credential_mapping_invalid', 'Credential ID and type were not found on the specified instance.')
        key = service.mapping_key(body.source_instance_id, body.target_instance_id, body.source_credential_id)
        async with request.app.state.db.transaction() as tx:
            await require_current_role(request, 'admin', tx)
            for identifier in (body.source_instance_id, body.target_instance_id):
                if await tx.fetchone('SELECT id FROM instances WHERE id=?', (identifier,)) is None:
                    fail(404, 'instance_not_found', 'Instance was not found.')
            await tx.execute('INSERT INTO credential_mappings(key,source_instance_id,target_instance_id,source_credential_id,target_credential_id,credential_type,target_name) VALUES(?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET target_credential_id=excluded.target_credential_id,credential_type=excluded.credential_type,target_name=excluded.target_name', (key, body.source_instance_id, body.target_instance_id, body.source_credential_id, body.target_credential_id, body.credential_type, body.target_name))
    await audit.record(request, 'credentials.mapping_saved', key)
    return {'key': key, **body.model_dump(), 'diagnostics': diagnostics}


@router.delete('/mappings/{key}')
async def delete_mapping(key: str, request: Request) -> dict[str, bool]:
    require_role(request, 'admin')
    row = await request.app.state.db.fetchone('SELECT * FROM credential_mappings WHERE key=?', (key,))
    if row is None:
        fail(404, 'mapping_not_found', 'Credential mapping was not found.')
    async with AsyncExitStack() as stack:
        for identifier in sorted({row['source_instance_id'], row['target_instance_id']}):
            await stack.enter_async_context(instance_guard(request, identifier))
        async with request.app.state.db.transaction() as tx:
            await require_current_role(request, 'admin', tx)
            current = await tx.fetchone('SELECT * FROM credential_mappings WHERE key=?', (key,))
            if current is None:
                fail(404, 'mapping_not_found', 'Credential mapping was not found.')
            if current['source_instance_id'] != row['source_instance_id'] or current['target_instance_id'] != row['target_instance_id']:
                fail(409, 'stale_mapping', 'Credential mapping changed. Refresh before retrying.')
            await tx.execute('DELETE FROM credential_mappings WHERE key=?', (key,))
    await audit.record(request, 'credentials.mapping_deleted', key)
    return {'deleted': True}
