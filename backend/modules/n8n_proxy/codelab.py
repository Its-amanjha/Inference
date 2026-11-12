"""Stale-checked edits of inactive Code nodes through the n8n public API."""

import asyncio
from copy import deepcopy
import hashlib
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend import audit
from backend.auth_gate import fail, require_role
from .client import N8nClient, instance_guard

router = APIRouter()
MAX_CODE_BYTES = 256 * 1024


class CodeUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expected_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    code: str = Field(max_length=MAX_CODE_BYTES)

    @field_validator('code')
    @classmethod
    def bounded_code(cls, value: str) -> str:
        if len(value.encode('utf-8')) > MAX_CODE_BYTES:
            raise ValueError('Code must not exceed 256 KiB encoded as UTF-8.')
        return value


def workflow_hash(workflow: dict[str, Any]) -> str:
    """Fingerprint the complete fetched workflow, including server metadata."""
    try:
        canonical = json.dumps(workflow, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)
        return hashlib.sha256(canonical.encode('utf-8')).hexdigest()
    except (ValueError, TypeError, RecursionError):
        fail(502, 'upstream_invalid_response', 'n8n returned an invalid workflow.')


def code_node(workflow: dict[str, Any], node_id: str) -> tuple[dict[str, Any], str, str]:
    nodes = workflow.get('nodes')
    if not isinstance(nodes, list) or any(not isinstance(node, dict) for node in nodes):
        fail(502, 'upstream_invalid_response', 'n8n returned invalid workflow nodes.')
    matches = [node for node in nodes if node.get('id') == node_id]
    if not matches:
        fail(404, 'node_not_found', 'The node was not found.')
    if len(matches) != 1:
        fail(409, 'ambiguous_node', 'The workflow contains duplicate node identifiers.')
    node = matches[0]
    if node.get('type') != 'n8n-nodes-base.code':
        fail(422, 'unsupported_node', 'CodeLab supports n8n Code nodes only.')
    parameters = node.get('parameters')
    if not isinstance(parameters, dict):
        fail(502, 'upstream_invalid_response', 'n8n returned invalid Code node parameters.')
    language = 'javaScript' if node.get('typeVersion') == 1 else parameters.get('language', 'javaScript')
    field = {'javaScript': 'jsCode', 'python': 'pythonCode', 'pythonNative': 'pythonCode'}.get(language) if isinstance(language, str) else None
    if field is None:
        fail(422, 'unsupported_language', 'The Code node language is unsupported.')
    if not isinstance(parameters.get(field, ''), str):
        fail(502, 'upstream_invalid_response', 'n8n returned invalid node code.')
    return node, language, field


def node_view(workflow: dict[str, Any], node_id: str, instance_id: str) -> dict[str, Any]:
    node, language, field = code_node(workflow, node_id)
    return {'instance_id': instance_id, 'workflow_hash': workflow_hash(workflow),
            'node_id': node_id, 'language': language, 'code': node['parameters'].get(field, '')}


@router.get('/workflows/{workflow_id}/nodes/{node_id}')
async def read_code(workflow_id: str, node_id: str, request: Request,
                    instance_id: str = Query(min_length=1, max_length=128)) -> dict[str, Any]:
    """The node's real code, unmasked, because the editor writes it back. Code often holds keys typed into
    it, so reading it needs the role that may edit it; viewers read masked code on the Workflows endpoints."""
    from .router import resource_id
    require_role(request, 'operator')
    resource_id(workflow_id)
    workflow = await N8nClient(request).request(instance_id, 'GET', f'/workflows/{workflow_id}')
    return node_view(workflow, node_id, instance_id)


@router.put('/workflows/{workflow_id}/nodes/{node_id}')
async def update_code(workflow_id: str, node_id: str, body: CodeUpdate, request: Request,
                      instance_id: str = Query(min_length=1, max_length=128)) -> dict[str, Any]:
    from .router import finish_audit, resource_id
    require_role(request, 'operator')
    resource_id(workflow_id)
    client = N8nClient(request)
    path = f'/workflows/{workflow_id}'
    target = f'{instance_id}:{workflow_id}:{node_id}'
    async with instance_guard(request, instance_id):
        workflow = await client.request(instance_id, 'GET', path)
        if workflow_hash(workflow) != body.expected_hash:
            fail(409, 'workflow_stale', 'The workflow changed. Reload before saving.')
        if workflow.get('active') is not False:
            fail(409, 'workflow_active', 'Explicitly deactivate the workflow before editing code.')
        edited = deepcopy(workflow)
        node, language, field = code_node(edited, node_id)
        node['parameters'][field] = body.code
        if not isinstance(edited.get('name'), str) or not isinstance(edited.get('connections'), dict) or not isinstance(edited.get('settings'), dict):
            fail(502, 'upstream_invalid_response', 'n8n returned an incomplete workflow.')
        payload = {key: edited[key] for key in ('name', 'nodes', 'connections', 'settings')}
        await audit.record(request, 'fleet.node_updated', target,
                           {'workflow_hash': body.expected_hash}, outcome='intent')
        written = False
        try:
            await client.request(instance_id, 'PUT', path, json=payload)
            written = True
            actual = await client.request(instance_id, 'GET', path)
            result = node_view(actual, node_id, instance_id)
            if actual.get('active') is not False or result['code'] != body.code or result['language'] != language:
                fail(502, 'upstream_outcome_unknown', 'The saved code could not be verified. Reload and reconcile before retrying.')
        except HTTPException as exc:
            code = exc.detail.get('code', 'request_failed') if isinstance(exc.detail, dict) else 'request_failed'
            unknown = written or code == 'upstream_outcome_unknown'
            await finish_audit(request, 'fleet.node_updated', target, {'code': code}, outcome='unknown' if unknown else 'failed')
            if written:
                fail(502, 'upstream_outcome_unknown', 'The saved code could not be verified. Reload and reconcile before retrying.')
            raise
        except asyncio.CancelledError:
            await finish_audit(request, 'fleet.node_updated', target, {'reason': 'request_cancelled'}, outcome='unknown')
            raise
        await finish_audit(request, 'fleet.node_updated', target, {'workflow_hash': result['workflow_hash']})
    await request.app.state.hub.broadcast({'event': 'fleet.changed', 'instance_id': instance_id})
    return result
