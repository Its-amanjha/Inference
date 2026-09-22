"""Closed built-in tool catalog; mutation dispatch is internal to approvals.

Provider tool calls must go through the approval service, never directly to
``execute``. Read results deliberately exclude workflow and execution payloads.
"""
from typing import Annotated, Any, Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from backend.auth_gate import ROLES, fail, require_current_role
from backend.modules.n8n_proxy.client import N8nClient, instance_guard
from backend.modules.n8n_proxy.codelab import CodeUpdate, MAX_CODE_BYTES, code_node, update_code, workflow_hash
from backend.modules.n8n_proxy.router import instance_row, list_view, mutate
from .providers import flatten_schema
from .repair_contract import ARGUMENT_MODELS, PROPOSAL_TOOLS, READ_TOOLS, TOOL_DESCRIPTIONS

Identifier = Annotated[str, Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')]
# One skill runs to ~23 KB and MAX_CALLS allows eight tool calls per turn, so an uncapped result
# could push ~180 KB into a single provider request. Truncation is reported in the result.
MAX_SKILL_RESULT = 24 * 1024


class InstanceArguments(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    instance_id: Identifier


class ListArguments(InstanceArguments):
    limit: int = Field(default=25, ge=1, le=25)


class WorkflowArguments(InstanceArguments):
    workflow_id: Identifier


class ExecutionArguments(InstanceArguments):
    execution_id: Identifier


class SkillArguments(BaseModel):
    """Names one installed skill. No instance: skills are local application content.

    Deliberately looser than a skill id. Models write what they mean rather than the exact id -
    a real one asked for "Code" - and a schema rejection raises before the tool can answer, which
    standard_chat does not catch, so a near-miss would end the conversation. The name is only ever
    a parameter in a lookup, never a path, and the leading character still bars traversal.
    """
    model_config = ConfigDict(extra='forbid', strict=True)
    skill_name: Annotated[str, Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$')]


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
# Repair conversations see only repair tools plus the linked activation switches, and the skill
# library: repair is exactly where n8n code guidance is needed.
REPAIR_SCOPE = REPAIR_TOOLS | {'activate_workflow', 'deactivate_workflow', 'get_skill'}
MUTATIONS = frozenset({'activate_workflow', 'deactivate_workflow', 'delete_execution', 'update_node'}) | REPAIR_PROPOSALS
_CATALOG = {
    'list_workflows': (ListArguments, 'List workflow identifiers, names and activation state for one instance.'),
    'get_execution': (ExecutionArguments, 'Read execution status metadata, without execution inputs or outputs.'),
    'list_errors': (ListArguments, 'List structural error metadata without messages or execution payloads.'),
    'get_skill': (SkillArguments, 'Read one installed n8n skill guide, for writing or debugging n8n JavaScript, '
                                  'Python, expressions, error handling or node configuration. Use the exact id, '
                                  'such as "n8n-code-javascript". An unrecognised name returns the installed ids '
                                  'instead of the guide, so call it again with one of those.'),
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


def declarations(user_role: str, *, scope: Literal['all', 'standard', 'repair'] = 'all',
                 allow_changes: bool = True) -> list[dict[str, Any]]:
    """Role-filtered, provider-portable (flattened) declarations.

    `standard` conversations do not see repair tools; `repair` conversations see only
    repair tools and the activation switches used by linked proposals. Without
    `allow_changes` no tool that could create an approval is offered at all, so a model
    that ignores its instructions still cannot turn a question into a proposal.
    """
    def visible(name: str) -> bool:
        if not allow_changes and name in MUTATIONS:
            return False
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
    if name == 'get_skill':
        # Handled before instance_id is read: skills are local content and name no instance.
        from backend.modules.skills.router import read_skill
        try:
            # Models write the name in their own case and spacing; match the id they meant.
            row = await read_skill(request, args['skill_name'].strip().casefold().replace(' ', '-').replace('_', '-'))
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            # The model invents this name with nothing to source it from, and standard_chat does not
            # catch tool errors, so raising here would end the conversation over a guess. Report the
            # miss and name what may be asked for instead, so the next round can correct itself.
            async with request.app.state.db.transaction() as tx:
                await require_current_role(request, 'viewer', tx)
                rows = await tx.fetchall('SELECT id FROM skills ORDER BY id LIMIT 50')
            # Same error shape as any other failed tool call, plus the ids the model can use instead.
            return {'skill': args['skill_name'],
                    'error': {'code': 'skill_not_installed',
                              'detail': 'No installed skill has that name. Call get_skill again with one of the available ids.'},
                    'available': [row['id'] for row in rows]}
        content = row['content']
        truncated = len(content) > MAX_SKILL_RESULT
        return {'skill': row['id'], 'name': row['name'], 'description': row['description'],
                # Truncation is reported, never silent: a model must not treat a cut guide as complete.
                'content': content[:MAX_SKILL_RESULT], 'truncated': truncated}
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
