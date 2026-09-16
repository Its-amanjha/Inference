"""Closed built-in tool catalog; mutation dispatch is internal to approvals.

Provider tool calls must go through the approval service, never directly to
``execute``. Read results deliberately exclude workflow and execution payloads.
"""
from typing import Annotated, Any, Literal

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from backend.auth_gate import ROLES, fail, require_current_role
from backend.modules.n8n_proxy.client import N8nClient, instance_guard
from backend.modules.n8n_proxy.codelab import CodeUpdate, MAX_CODE_BYTES, code_node, update_code, workflow_hash
from backend.modules.n8n_proxy.router import instance_row, list_view, mutate
from .providers import flatten_schema
from .repair_contract import ARGUMENT_MODELS, PROPOSAL_TOOLS, READ_TOOLS, TOOL_DESCRIPTIONS

Identifier = Annotated[str, Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')]


class InstanceArguments(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    instance_id: Identifier


class ListArguments(InstanceArguments):
    limit: int = Field(default=25, ge=1, le=25)


class WorkflowArguments(InstanceArguments):
    workflow_id: Identifier


class ExecutionArguments(InstanceArguments):
    execution_id: Identifier


class NodeArguments(WorkflowArguments):
    node_id: Identifier
    code: str = Field(max_length=MAX_CODE_BYTES)

    @field_validator('code')
    @classmethod
    def bounded_code(cls, value: str) -> str:
        if len(value.encode('utf-8')) > MAX_CODE_BYTES:
            raise ValueError('Code exceeds the byte limit.')
        return value


REPAIR_READS = frozenset(READ_TOOLS)
REPAIR_PROPOSALS = frozenset(PROPOSAL_TOOLS)
REPAIR_TOOLS = REPAIR_READS | REPAIR_PROPOSALS
# Repair conversations see only repair tools plus the linked activation switches.
REPAIR_SCOPE = REPAIR_TOOLS | {'activate_workflow', 'deactivate_workflow'}
MUTATIONS = frozenset({'activate_workflow', 'deactivate_workflow', 'delete_execution', 'update_node'}) | REPAIR_PROPOSALS
_CATALOG = {
    'list_workflows': (ListArguments, 'List workflow identifiers, names and activation state for one instance.'),
    'get_execution': (ExecutionArguments, 'Read execution status metadata, without execution inputs or outputs.'),
    'list_errors': (ListArguments, 'List structural error metadata without messages or execution payloads.'),
    'activate_workflow': (WorkflowArguments, 'Propose activating a workflow. Requires human confirmation.'),
    'deactivate_workflow': (WorkflowArguments, 'Propose deactivating a workflow. Requires human confirmation.'),
    'delete_execution': (ExecutionArguments, 'Propose deleting an execution. Requires human confirmation.'),
    'update_node': (NodeArguments, 'Propose replacing code in an inactive workflow Code node. Requires human confirmation.'),
    **{name: (ARGUMENT_MODELS[name], TOOL_DESCRIPTIONS[name]) for name in (*READ_TOOLS, *PROPOSAL_TOOLS)},
}


def _failing_fields(error: ValidationError, limit: int = 5, budget: int = 300) -> str:
    """Field paths and reasons, never the values: arguments may carry a pasted key or workflow content."""
    seen: set[str] = set()
    parts: list[str] = []
    for item in error.errors(include_url=False, include_input=False):
        location = '.'.join(str(part) for part in item['loc']) or 'arguments'
        if location in seen:
            continue
        seen.add(location)
        entry = f'{location}: {item["msg"]}'
        if sum(len(part) + 2 for part in parts) + len(entry) > budget or len(parts) == limit:
            parts.append('…')
            break
        parts.append(entry)
    return '; '.join(parts)


def validate(name: str, arguments: dict) -> dict:
    if not isinstance(name, str) or name not in _CATALOG:
        fail(422, 'unknown_tool', 'The requested tool is not supported.')
    try:
        parsed = _CATALOG[name][0].model_validate(arguments)
        # Repair arguments keep only what was supplied; re-validation restores defaults.
        return parsed.model_dump(exclude_unset=True) if name in REPAIR_TOOLS else parsed.model_dump()
    except ValidationError as error:
        # Name the tool and the fields: "does not match the schema" alone leaves nobody able to act on it.
        fail(422, 'invalid_tool_arguments',
             f'Tool arguments for {name} do not match the permitted schema. ' + (_failing_fields(error) or 'No field detail available.'))
    except UnicodeError:
        fail(422, 'invalid_tool_arguments', f'Tool arguments for {name} contain text that could not be decoded.')


def parse(name: str, arguments: dict) -> BaseModel:
    """Validated contract model for a repair tool."""
    validate(name, arguments)
    return _CATALOG[name][0].model_validate(arguments)


def role(name: str) -> str:
    if name not in _CATALOG:
        fail(422, 'unknown_tool', 'The requested tool is not supported.')
    return 'operator' if name in MUTATIONS else 'viewer'


def declarations(user_role: str, *, scope: Literal['all', 'standard', 'repair'] = 'all') -> list[dict[str, Any]]:
    """Role-filtered, provider-portable (flattened) declarations.

    `standard` conversations do not see repair tools; `repair` conversations see only
    repair tools and the activation switches used by linked proposals.
    """
    def visible(name: str) -> bool:
        if scope == 'standard':
            return name not in REPAIR_TOOLS
        if scope == 'repair':
            return name in REPAIR_SCOPE
        return True
    return [{'type': 'function', 'function': {'name': name, 'description': description,
             'parameters': flatten_schema(model.model_json_schema())}}
            for name, (model, description) in _CATALOG.items()
            if visible(name) and ROLES.get(user_role, -1) >= ROLES[role(name)]]


def target(arguments: dict) -> str:
    return InstanceArguments.model_validate({'instance_id': arguments['instance_id']}).instance_id


async def _authorize(request: Request, name: str) -> None:
    async with request.app.state.db.transaction() as tx:
        await require_current_role(request, role(name), tx)


async def fingerprint(request: Request, name: str, args: dict) -> str:
    args = validate(name, args)
    await _authorize(request, name)
    if name not in MUTATIONS:
        fail(422, 'invalid_tool', 'Only mutations have approval fingerprints.')
    if name in REPAIR_PROPOSALS:
        # Content binding: excludes active/versionId so a linked deactivation keeps it valid.
        from . import repair_actions
        return await repair_actions.current_binding(request, name, args)
    instance = args['instance_id']
    async with instance_guard(request, instance):
        connection = await instance_row(request, instance)
        execution = name == 'delete_execution'
        resource = args['execution_id'] if execution else args['workflow_id']
        path = f"/{'executions' if execution else 'workflows'}/{resource}"
        kwargs = {'params': {'includeData': 'true'}} if execution else {}
        document = await N8nClient(request).request(instance, 'GET', path, **kwargs)
        if name == 'update_node':
            if document.get('active') is not False:
                fail(409, 'workflow_active', 'Explicitly deactivate the workflow before editing code.')
            code_node(document, args['node_id'])
        return workflow_hash({'instance_id': instance, 'url': connection['url'],
                              'api_key_enc': connection['api_key_enc'], 'resource_id': resource,
                              'name': name, 'resource': document})


def _metadata(document: dict, keys: tuple[str, ...]) -> dict:
    # Reject nested values in allowlisted fields rather than leaking injected payloads.
    result = {key: value for key in keys if (value := document.get(key)) is None
              or isinstance(value, (str, bool, int, float))}
    for key in ('id', 'workflowId', 'instance_id', 'workflow_id', 'execution_id'):
        if key in result and result[key] is not None:
            result[key] = str(result[key])
    for key, value in result.items():
        if isinstance(value, str):
            result[key] = value[:512]
    return result


async def execute(request: Request, name: str, args: dict, *, expected_fingerprint: str | None = None,
                  send_error_text: bool = True, expected_workflow_id: str | None = None) -> dict:
    """Execute reads, or a mutation already claimed by the approval service.

    Repair reads return masked contract context. `send_error_text` is further limited by
    the administrator switch; `expected_workflow_id` pins error reads to one workflow.
    """
    args = validate(name, args)
    await _authorize(request, name)
    if name in REPAIR_TOOLS:
        from . import repair_actions
        if name == 'get_workflow_context':
            return await repair_actions.workflow_context_tool(request, args)
        if name == 'get_error_context':
            return await repair_actions.error_context_tool(request, args, send_error_text=send_error_text,
                                                           expected_workflow_id=expected_workflow_id)
        fail(409, 'approval_required', 'Repair proposals are applied only through confirmation.')
    instance = args['instance_id']
    client = N8nClient(request)
    if name == 'list_workflows':
        payload = await client.request(instance, 'GET', '/workflows', params={'limit': args['limit'], 'excludePinnedData': 'true'})
        await _authorize(request, name)
        rows = list_view(payload, instance)['data'][:args['limit']]
        return {'instance_id': instance, 'data': [_metadata(row, ('id', 'name', 'active')) for row in rows]}
    if name == 'get_execution':
        document = await client.request(instance, 'GET', f"/executions/{args['execution_id']}", params={'includeData': 'false'})
        await _authorize(request, name)
        return {'instance_id': instance, **_metadata(document, ('id', 'workflowId', 'status', 'startedAt', 'stoppedAt', 'finished'))}
    if name == 'list_errors':
        async with request.app.state.db.transaction() as tx:
            await require_current_role(request, 'viewer', tx)
            await instance_row(request, instance, tx)
            rows = await tx.fetchall('SELECT id,instance_id,workflow_id,execution_id,count,occurred_at,status FROM errors WHERE instance_id=? ORDER BY id DESC LIMIT ?', (instance, args['limit']))
        return {'data': [_metadata(row, tuple(row)) for row in rows], 'instance_id': instance}
    if name == 'update_node':
        async with instance_guard(request, instance):
            document = await client.request(instance, 'GET', f"/workflows/{args['workflow_id']}")
            if expected_fingerprint is not None:
                connection = await instance_row(request, instance)
                actual = workflow_hash({'instance_id': instance, 'url': connection['url'],
                    'api_key_enc': connection['api_key_enc'], 'resource_id': args['workflow_id'],
                    'name': name, 'resource': document})
                if actual != expected_fingerprint:
                    fail(409, 'approval_stale', 'The approved workflow changed. Create a new proposal.')
            body = CodeUpdate(expected_hash=workflow_hash(document), code=args['code'])
            await update_code(args['workflow_id'], args['node_id'], body, request, instance)
    elif name == 'delete_execution':
        await mutate(request, instance, 'DELETE', f"/executions/{args['execution_id']}", 'fleet.execution_deleted', args['execution_id'])
    else:
        action = 'activate' if name == 'activate_workflow' else 'deactivate'
        await mutate(request, instance, 'POST', f"/workflows/{args['workflow_id']}/{action}", f'fleet.workflow_{action}d', args['workflow_id'])
    return {'ok': True, 'instance_id': instance, 'tool': name}
