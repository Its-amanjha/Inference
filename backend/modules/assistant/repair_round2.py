"""Create and retry: prepare without writes, confirm one POST, verify by readback.

Endpoint contract: n8n@2.19.0/packages/cli/src/public-api/v1/handlers/executions/
spec/paths/executions.id.retry.yml (loadWorkflow=true selects the saved workflow).
"""
import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException, Request

from backend.auth_gate import fail
from backend.database import _settle
from backend.modules.n8n_proxy.client import N8nClient, instance_guard
from backend.modules.n8n_proxy.router import instance_row
from . import repair_contract as contract
from .repair_actions import _authorize, _bounded, _code, _complete, _identifier, _outcome, binding, fetch_workflow, target_view
from .repair_context import canonical_bytes, content_hash, mask_text
from .repair_diff import PatchError, apply_operations, introduced_self_calls, preview

CREATE = 'propose_create_workflow'
EMPTY = {'name': '', 'nodes': [], 'connections': {}, 'settings': {}}


async def retry_source(request: Request, args: contract.RetryArguments) -> tuple[dict, str]:
    execution = await N8nClient(request).request(args.instance_id, 'GET', f'/executions/{args.execution_id}')
    if _identifier(execution.get('id')) != args.execution_id or _identifier(execution.get('workflowId')) != args.workflow_id:
        fail(409, 'target_mismatch', 'The execution does not belong to the pinned workflow.')
    if execution.get('status') not in {'error', 'crashed'} or execution.get('retrySuccessId'):
        fail(409, 'execution_not_retryable', 'Choose a failed execution without an existing successful retry.')
    document = await fetch_workflow(request, args.instance_id, args.workflow_id)
    # Include all returned metadata, but never request execution payloads.
    digest = hashlib.sha256(canonical_bytes({'execution': execution, 'workflow': content_hash(document)})).hexdigest()
    return document, digest


async def current_binding(request: Request, name: str, arguments: dict) -> str:
    args = contract.ARGUMENT_MODELS[name].model_validate(arguments)
    connection = await instance_row(request, args.instance_id)
    if name == CREATE:
        return binding(connection, '', name, '')
    _, digest = await retry_source(request, args)
    return binding(connection, args.workflow_id, name, digest)


async def prepare(request: Request, name: str, arguments: dict) -> dict:
    args = contract.ARGUMENT_MODELS[name].model_validate(arguments)
    async with instance_guard(request, args.instance_id):
        connection = await instance_row(request, args.instance_id)
        if name == CREATE:
            definition = args.workflow
            operations = ([{'op': 'add_node', 'node': node.model_dump()} for node in definition.nodes]
                          + [{'op': 'connect', **edge.model_dump()} for edge in definition.connections]
                          + [{'op': 'set_setting', 'key': key, 'value': value} for key, value in definition.settings.items()])
            # Same limits and validation as patching; refuse ambiguous graphs and placeholders.
            try:
                desired = apply_operations({**EMPTY, 'name': definition.name}, operations)
            except PatchError as exc:
                fail(422, 'invalid_patch', exc.message)
            _bounded(desired)
            if introduced_self_calls(EMPTY, desired):
                fail(422, 'self_call_blocked', 'This workflow would call its own trigger in an endless loop.')
            view = preview(EMPTY, desired, kind='workflow_patch',
                           summary_text=f'Create inactive workflow “{mask_text(definition.name)[0]}” on '
                                        f'instance {args.instance_id}. Activation requires a separate approval.')
            view.update(kind='workflow_create')
            view['diff']['operation'] = 'create'
            view['apply']['label'] = 'Create inactive workflow'
            state = {'fingerprint': binding(connection, '', name, ''), 'desired': desired,
                     'desired_hash': content_hash(desired), 'target': {
                         'instance_id': args.instance_id, 'instance_name': connection.get('name'),
                         'workflow_id': None, 'workflow_name': definition.name, 'active': False}}
        else:
            document, digest = await retry_source(request, args)
            if content_hash(document) != args.base_content_hash:
                fail(409, 'context_stale', 'The saved workflow changed. Read its context before proposing a retry.')
            view = preview(document, document, kind='workflow_patch', summary_text=(
                f'Retry execution {args.execution_id} of workflow {args.workflow_id} '
                f'“{mask_text(str(document.get("name", "")))[0]}” on instance {args.instance_id} '
                'using the currently saved workflow. This repeats side effects, including writes, emails and payments.'))
            view.update(kind='execution_retry')
            view['diff']['operation'] = 'retry'
            view['apply'] = {'label': 'Retry execution', 'danger': True, 'typed_confirmation': 'RETRY'}
            state = {'fingerprint': binding(connection, args.workflow_id, name, digest),
                     'target': {**target_view(connection, args.workflow_id, document, content_hash(document)),
                                'execution_id': args.execution_id}}
        await _authorize(request, 'operator')
        return {**state, 'preview': contract.Preview.model_validate(view).model_dump(), 'warning': None}


async def record_result(request: Request, action_id: str, state: dict, result: dict) -> None:
    """Keep returned identifiers durably, including when subsequent verification fails."""
    state['result'] = result
    encrypted = request.app.state.vault.encrypt(json.dumps(state, sort_keys=True, allow_nan=False))
    async with request.app.state.db.transaction() as tx:
        await tx.execute('UPDATE approval_actions SET state_enc=? WHERE id=?', (encrypted, action_id))


async def execute(request: Request, action_id: str, name: str, state: dict, *,
                  before_write: Callable[[], Awaitable[None]], progress: dict) -> dict[str, Any]:
    target = state['target']
    instance_id = target['instance_id']
    action = 'assistant.workflow_created' if name == CREATE else 'assistant.execution_retried'
    label = f'{instance_id}:{action_id}'
    async with instance_guard(request, instance_id):
        if await current_binding(request, name, state['arguments']) != state['fingerprint']:
            fail(409, 'approval_stale', 'The instance, workflow or execution changed. Create a new proposal.')
        await before_write()
        await _outcome(request, action, label, {'tool': name}, 'intent')
        # Check live privileges again after the audit write and immediately before the remote operation.
        await before_write()
        client = N8nClient(request)
        # n8n's create schema treats active as read-only; POST creates inactive, verified below.
        sent = False
        progress['writing'] = True
        try:
            response = (await client.request(instance_id, 'POST', '/workflows', json=state['desired']) if name == CREATE
                        else await client.retry_execution(instance_id, target['execution_id']))
            sent = True
            identifier = _identifier(response.get('id'))
            if identifier is None or (name != CREATE and identifier == target['execution_id']):
                fail(502, 'upstream_outcome_unknown', 'The instance did not return a verifiable new resource ID.')
            result = {'ok': False, 'tool': name, 'instance_id': instance_id, 'repair_id': action_id, 'verified': False,
                      'workflow_id': identifier if name == CREATE else target['workflow_id']}
            if name != CREATE:
                result.update(execution_id=identifier, retry_of=target['execution_id'])
            await _settle(record_result(request, action_id, state, result))
            actual = await client.request(instance_id, 'GET', f'/{"workflows" if name == CREATE else "executions"}/{identifier}')
            if name == CREATE:
                if (not _complete(actual) or _identifier(actual.get('id')) != identifier or actual.get('active') is not False
                        or content_hash(actual) != state['desired_hash']):
                    fail(502, 'upstream_outcome_unknown', 'The new inactive workflow could not be verified. Reconcile manually.')
                result.update(active=False, content_hash=state['desired_hash'])
            else:
                if (_identifier(actual.get('id')) != identifier or _identifier(actual.get('workflowId')) != target['workflow_id']
                        or _identifier(actual.get('retryOf')) != target['execution_id']
                        or actual.get('status') not in {'new', 'running', 'waiting', 'success', 'error', 'crashed', 'canceled'}):
                    fail(502, 'upstream_outcome_unknown', 'The retry execution could not be verified. Reconcile manually.')
                result['execution_status'] = actual['status']
            result.update(ok=True, verified=True)
            result = (contract.RepairResult if name == CREATE else contract.RetryResult).model_validate(result).model_dump()
            await _settle(record_result(request, action_id, state, result))
            await _outcome(request, action, label, result, 'success')
            return result
        except HTTPException as exc:
            unknown = sent or _code(exc) == 'upstream_outcome_unknown'
            if not unknown:
                progress['writing'] = False
            await _outcome(request, action, label, {'failure': _code(exc)}, 'unknown' if unknown else 'failed')
            if unknown:
                fail(502, 'upstream_outcome_unknown', 'The operation could not be verified. Do not repeat it; reconcile on the instance.')
            raise
