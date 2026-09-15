"""Repair and restore proposals: masked n8n reads, server-computed previews, verified writes.

Nothing here authorizes a write. Proposals are prepared under the instance guard and
stored by the approval service. Confirmation re-fetches the workflow, requires the exact
binding and an inactive workflow, snapshots the saved version, sends one PUT and
verifies the readback. Uncertain outcomes are recorded and never retried.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException, Request

from backend import audit
from backend.auth_gate import fail, require_current_role
from backend.config import VaultError
from backend.database import _settle
from backend.modules.n8n_proxy.client import N8nClient, instance_guard
from backend.modules.n8n_proxy.router import instance_row
from . import repair_contract as contract
from .repair_context import build_context, canonical_bytes, content_hash, mask_workflow, writable_document
from .repair_diff import PatchError, apply_operations, preview
from .service import canonical

SNAPSHOT_PREFIX = 'assistant:repair:'
SETTINGS_KEY = 'assistant:settings:repair'
MAX_SNAPSHOTS = 256
MAX_WORKFLOW_BYTES = 1024 * 1024
AUDIT_ACTIONS = {'propose_workflow_patch': 'assistant.workflow_repaired', 'propose_restore_workflow': 'assistant.workflow_restored'}
# Session, role and connection failures always propagate; other upstream failures only
# make execution detail unavailable.
PASSTHROUGH = frozenset({'authentication_required', 'session_revoked', 'session_expired', 'permission_denied',
                         'instance_not_found', 'instance_configuration_invalid'})
_IDENTIFIER = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')
Linker = Callable[[str, str], Awaitable[str]]


def _code(exc: HTTPException) -> str:
    return exc.detail.get('code', '') if isinstance(exc.detail, dict) else ''


def _identifier(value: Any) -> str | None:
    value = str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else None
    return value if value is not None and _IDENTIFIER.fullmatch(value) else None


async def _authorize(request: Request, role: str) -> None:
    async with request.app.state.db.transaction() as tx:
        await require_current_role(request, role, tx)


async def error_text_enabled(request: Request) -> bool:
    """Admin switch; defaults on, and a corrupt setting withholds error text."""
    row = await request.app.state.db.fetchone('SELECT value_json FROM documents WHERE key=?', (SETTINGS_KEY,))
    if row is None:
        return True
    try:
        value = json.loads(row['value_json'])
    except (TypeError, ValueError):
        return False
    return isinstance(value, dict) and value.get('error_text_enabled') is True


async def webhook_directory_enabled(request: Request) -> bool:
    """Admin switch for sharing other workflows' trigger routes with providers.

    Defaults on (including settings saved before the switch existed); a corrupt setting withholds the directory.
    """
    row = await request.app.state.db.fetchone('SELECT value_json FROM documents WHERE key=?', (SETTINGS_KEY,))
    if row is None:
        return True
    try:
        value = json.loads(row['value_json'])
    except (TypeError, ValueError):
        return False
    return isinstance(value, dict) and value.get('webhook_directory_enabled', True) is True


def context_bytes(context: dict[str, Any]) -> bytes:
    """The exact bytes returned by GET /repair/context; their length equals context["bytes"]."""
    return canonical_bytes(context)


def serialize_context(context: dict[str, Any]) -> str:
    """The exact text given to a provider: the same bytes as GET /repair/context."""
    return context_bytes(context).decode('utf-8')


def _bounded(document: dict[str, Any]) -> None:
    try:
        size = len(canonical_bytes(document))
    except (TypeError, ValueError, RecursionError):
        fail(502, 'upstream_invalid_response', 'n8n returned a workflow that is not valid JSON.')
    if size > MAX_WORKFLOW_BYTES:
        fail(422, 'workflow_too_large', 'The workflow exceeds the 1 MiB repair size limit.')


def _complete(document: dict[str, Any]) -> bool:
    return isinstance(document.get('nodes'), list) and isinstance(document.get('connections'), dict)


async def fetch_workflow(request: Request, instance_id: str, workflow_id: str) -> dict[str, Any]:
    document = await N8nClient(request).request(instance_id, 'GET', f'/workflows/{workflow_id}')
    if not _complete(document) or ('id' in document and str(document['id']) != workflow_id):
        fail(502, 'upstream_invalid_response', 'n8n returned an incomplete or different workflow.')
    return document


def binding(connection: dict[str, Any], workflow_id: str, name: str, digest: str) -> str:
    """Excludes active/versionId so a linked deactivation does not make the proposal stale."""
    return hashlib.sha256(canonical({'instance_id': connection['id'], 'url': connection['url'],
                                     'api_key_enc': connection['api_key_enc'], 'workflow_id': workflow_id,
                                     'tool': name, 'content_hash': digest}).encode('utf-8')).hexdigest()


def target_view(connection: dict[str, Any], workflow_id: str, document: dict[str, Any], digest: str) -> dict[str, Any]:
    def typed(value: Any, kind: type) -> Any:
        return value if isinstance(value, kind) else None
    return contract.Target.model_validate({
        'instance_id': connection['id'], 'instance_name': typed(connection.get('name'), str),
        'workflow_id': workflow_id, 'workflow_name': typed(document.get('name'), str),
        'active': typed(document.get('active'), bool), 'version_id': typed(document.get('versionId'), str),
        'content_hash': digest}).model_dump()


# ---------------------------------------------------------------------------
# Context reads (viewer)
# ---------------------------------------------------------------------------

async def _error_row(request: Request, instance_id: str, error_id: str | None, execution_id: str | None) -> dict[str, Any] | None:
    columns = 'id,instance_id,workflow_id,workflow_name,execution_id,node_name,error_type,error_message,count,occurred_at,status'
    async with request.app.state.db.transaction() as tx:
        await require_current_role(request, 'viewer', tx)
        await instance_row(request, instance_id, tx)
        if error_id is not None:
            row = await tx.fetchone(f'SELECT {columns} FROM errors WHERE id=? AND instance_id=?', (int(error_id), instance_id))
            if row is None:
                fail(404, 'error_not_found', 'The error was not found for this instance.')
        else:
            row = await tx.fetchone(f'SELECT {columns} FROM errors WHERE instance_id=? AND execution_id=? ORDER BY id DESC LIMIT 1',
                                    (instance_id, execution_id))
    if row is not None:
        row['id'] = str(row['id'])
    return row


async def _execution(request: Request, instance_id: str, execution_id: str | None) -> dict[str, Any] | None:
    if execution_id is None:
        return None
    try:
        return await N8nClient(request).request(instance_id, 'GET', f'/executions/{execution_id}', params={'includeData': 'true'})
    except HTTPException as exc:
        if _code(exc) in PASSTHROUGH:
            raise
        # Oversized (client response cap), pruned, missing or unreachable: detail "unavailable".
        return None


async def load_context(request: Request, target: dict[str, Any], *, include_workflow: bool, include_error: bool,
                       send_error_text: bool, focus_nodes: tuple[str, ...] = ()) -> dict[str, Any]:
    """Read and mask a workflow and/or scrub an error. Returns the context plus pinning facts."""
    instance_id = target['instance_id']
    workflow_id = target.get('workflow_id')
    error_row = execution = None
    admin_allows = await error_text_enabled(request)
    if include_error and (target.get('error_id') is not None or target.get('execution_id') is not None):
        error_row = await _error_row(request, instance_id, target.get('error_id'), target.get('execution_id'))
        row_workflow = _identifier(error_row.get('workflow_id')) if error_row else None
        if workflow_id and row_workflow and row_workflow != workflow_id:
            fail(409, 'target_mismatch', 'That error belongs to a different workflow than this conversation.')
        execution_id = target.get('execution_id') or _identifier((error_row or {}).get('execution_id'))
        execution = await _execution(request, instance_id, execution_id)
        run_workflow = _identifier(execution.get('workflowId')) if execution else None
        if run_workflow and run_workflow != (workflow_id or row_workflow or run_workflow):
            if workflow_id:
                fail(409, 'target_mismatch', 'That execution belongs to a different workflow than this conversation.')
            execution = None
        workflow_id = workflow_id or row_workflow or run_workflow
    document = await fetch_workflow(request, instance_id, workflow_id) if include_workflow and workflow_id else None
    effective = bool(send_error_text and admin_allows)
    context = build_context(instance_id, workflow=document, execution=execution, error_row=error_row,
                            send_error_text=effective, focus_nodes=tuple(focus_nodes))
    if document is not None and await webhook_directory_enabled(request):
        # Other workflows' trigger addresses let the model find the service a node should call instead of guessing
        # (live test: a model that saw only the target pointed it at its own webhook). Optional context only.
        from .repair_context import attach_webhook_directory, webhook_endpoints
        try:
            listing = await N8nClient(request).request(instance_id, 'GET', '/workflows', params={'limit': 100})
        except Exception:
            listing = None
        rows = listing.get('data') if isinstance(listing, dict) else None
        context = attach_webhook_directory(context, webhook_endpoints(rows if isinstance(rows, list) else [],
                                                                      exclude_workflow_id=workflow_id))
    await _authorize(request, 'viewer')  # Live check after awaited remote reads.
    error = context.get('error') if isinstance(context, dict) else None
    return {'context': context, 'workflow_id': workflow_id, 'document': document,
            # Stored error messages count too: only "withheld" guarantees no error text was included.
            'error_text_sent': bool(error) and error.get('detail') != 'withheld',
            'directory_sent': isinstance(context, dict) and 'webhook_directory' in context,
            'withheld_by_admin': bool(error) and bool(send_error_text) and not admin_allows}


async def workflow_context_tool(request: Request, arguments: dict[str, Any]) -> dict[str, Any]:
    args = contract.WorkflowContextArgs.model_validate(arguments)
    loaded = await load_context(request, {'instance_id': args.instance_id, 'workflow_id': args.workflow_id},
                                include_workflow=True, include_error=False, send_error_text=False,
                                focus_nodes=tuple(args.focus_nodes))
    return loaded['context']


async def error_context_tool(request: Request, arguments: dict[str, Any], *, send_error_text: bool,
                             expected_workflow_id: str | None = None) -> dict[str, Any]:
    args = contract.ErrorContextArgs.model_validate(arguments)
    loaded = await load_context(request, {'instance_id': args.instance_id, 'workflow_id': expected_workflow_id,
                                          'error_id': args.error_id, 'execution_id': args.execution_id},
                                include_workflow=False, include_error=True, send_error_text=send_error_text)
    return loaded['context']


# ---------------------------------------------------------------------------
# Proposals (operator; no remote writes)
# ---------------------------------------------------------------------------

async def load_snapshot(request: Request, repair_id: str) -> dict[str, Any] | None:
    row = await request.app.state.db.fetchone('SELECT value_json FROM documents WHERE key=?', (SNAPSHOT_PREFIX + repair_id,))
    if row is None:
        return None
    try:
        value = json.loads(request.app.state.vault.decrypt(json.loads(row['value_json'])['encrypted']))
    except (KeyError, TypeError, ValueError, VaultError):
        return None
    return value if isinstance(value, dict) else None


def _patch_message(exc: PatchError) -> str:
    text = str(getattr(exc, 'message', '') or exc).strip()
    return text[:300] if text else 'The proposed operations do not apply cleanly to the saved workflow.'


async def _restore_source(request: Request, args: contract.RestoreArguments, document: dict[str, Any],
                          before: dict[str, Any]) -> tuple[dict[str, Any], str, str | None]:
    label = document['name'] if isinstance(document.get('name'), str) else args.workflow_id
    unavailable = 'The requested version is unavailable for this workflow. History may be disabled, unlicensed or pruned.'
    if args.repair_id is not None:
        snapshot = await load_snapshot(request, args.repair_id)
        # One answer for missing and foreign snapshots, so IDs cannot be probed.
        if (snapshot is None or snapshot.get('instance_id') != args.instance_id
                or snapshot.get('workflow_id') != args.workflow_id or not isinstance(snapshot.get('before'), dict)
                or not _complete(snapshot['before'])):
            fail(404, 'history_unavailable', unavailable)
        return (writable_document(snapshot['before']),
                f'Restore workflow “{label}” to the version saved before repair {args.repair_id}.', None)
    try:
        version = await N8nClient(request).request(args.instance_id, 'GET', f'/workflows/{args.workflow_id}/{args.version_id}')
    except HTTPException as exc:
        if _code(exc) in {'upstream_not_found', 'upstream_forbidden'}:
            fail(404, 'history_unavailable', unavailable)
        raise
    if (not _complete(version) or ('workflowId' in version and str(version['workflowId']) != args.workflow_id)
            or ('versionId' in version and version['versionId'] != args.version_id)):
        fail(404, 'history_unavailable', unavailable)
    desired = writable_document({**before, 'nodes': version['nodes'], 'connections': version['connections']})
    return (desired, f'Restore workflow “{label}” to history version {args.version_id}.',
            'History versions restore nodes and connections; the workflow name and settings keep their current values.')


async def current_binding(request: Request, name: str, arguments: dict[str, Any]) -> str:
    args = contract.ARGUMENT_MODELS[name].model_validate(arguments)
    async with instance_guard(request, args.instance_id):
        document = await fetch_workflow(request, args.instance_id, args.workflow_id)
        connection = await instance_row(request, args.instance_id)
        return binding(connection, args.workflow_id, name, content_hash(document))


def blocked_paths(document: dict[str, Any], digest: str, seen: list[dict[str, Any]] | None = None) -> frozenset[str]:
    """Truncated paths the model never saw in full.

    With workflow views from the conversation, a path stays blocked only when every view of this
    exact content truncated it. Without a matching view, the default context view decides.
    """
    views = [view for view in (seen or []) if isinstance(view, dict) and view.get('content_hash') == digest
             and isinstance(view.get('truncated_paths'), list)]
    if not views:
        return frozenset(mask_workflow(document)['truncated_paths'])
    blocked = set(views[0]['truncated_paths'])
    for view in views[1:]:
        blocked &= set(view['truncated_paths'])
    return frozenset(blocked)


async def prepare(request: Request, name: str, arguments: dict[str, Any], *, link_deactivation: Linker,
                  seen: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Compute the approval state for a patch or restore. Sends no POST or PUT.

    `seen` lists the workflow views ({content_hash, truncated_paths, redacted_paths}) the model was given.
    """
    if name not in contract.PROPOSAL_TOOLS:
        fail(422, 'unknown_tool', 'The requested tool is not supported.')
    args = contract.ARGUMENT_MODELS[name].model_validate(arguments)
    instance_id, workflow_id = args.instance_id, args.workflow_id
    async with instance_guard(request, instance_id):
        document = await fetch_workflow(request, instance_id, workflow_id)
        connection = await instance_row(request, instance_id)
        before = writable_document(document)
        _bounded(before)
        digest = content_hash(document)
        if digest != args.base_content_hash:
            fail(409, 'context_stale', 'The workflow changed after its context was read. Read the context again before proposing.')
        truncated = blocked_paths(document, digest, seen)
        if name == 'propose_workflow_patch':
            try:
                desired = apply_operations(document, [operation.model_dump() for operation in args.operations],
                                           truncated_paths=truncated)
            except PatchError as exc:
                fail(422, 'invalid_patch', _patch_message(exc))
            # A workflow that calls its own trigger re-runs itself endlessly (seen live: 3,816 executions).
            from .repair_diff import introduced_self_calls
            loops = introduced_self_calls(before, desired)
            if loops:
                first = loops[0]
                fail(422, 'self_call_blocked', f"This change would make “{str(first['node'])[:64]}” call this workflow's own "
                     f"trigger {str(first['detail'])[:96]}, which starts the workflow again in an endless loop. Point it at "
                     "the service it should reach instead; other workflows' trigger addresses are listed in webhook_directory.")
            kind, summary_text, model_summary, warning = 'workflow_patch', None, args.summary, None
        else:
            desired, summary_text, warning = await _restore_source(request, args, document, before)
            kind, model_summary = 'workflow_restore', None
        _bounded(desired)
        desired_hash = content_hash(desired)
        if desired_hash == digest:
            fail(422, 'invalid_patch', 'The proposal would not change the saved workflow.')
        preconditions = []
        if document.get('active') is not False:
            linked = await link_deactivation(instance_id, workflow_id)
            preconditions.append({'code': 'workflow_active', 'satisfied': False, 'tool': 'deactivate_workflow', 'action_id': linked})
        name_text = document['name'] if isinstance(document.get('name'), str) else None
        view = contract.Preview.model_validate(preview(before, desired, kind=kind, summary_text=summary_text,
                                                       model_summary=model_summary, preconditions=preconditions,
                                                       workflow_name=name_text)).model_dump()
        redacted = sorted({path for item in (seen or []) if isinstance(item, dict) and item.get('content_hash') == digest
                           for path in item.get('redacted_paths') or [] if isinstance(path, str)})
        return {'fingerprint': binding(connection, workflow_id, name, digest), 'content_hash': digest,
                'before': before, 'desired': desired, 'desired_hash': desired_hash, 'preview': view,
                'target': target_view(connection, workflow_id, document, digest), 'warning': warning,
                'context_paths': {'truncated': sorted(truncated), 'redacted': redacted}}


# ---------------------------------------------------------------------------
# Confirmation (operator; one PUT, verified, never retried)
# ---------------------------------------------------------------------------

async def _outcome(request: Request, action: str, label: str, details: dict[str, Any], outcome: str) -> None:
    await _settle(audit.record(request, action, label, details, outcome))


async def execute(request: Request, action_id: str, name: str, state: dict[str, Any], *,
                  before_write: Callable[[], Awaitable[None]], progress: dict[str, bool]) -> dict[str, Any]:
    target = state['target']
    instance_id, workflow_id = target['instance_id'], target['workflow_id']
    path = f'/workflows/{workflow_id}'
    label = f'{instance_id}:{workflow_id}:{action_id}'
    action = AUDIT_ACTIONS[name]
    client = N8nClient(request)
    async with instance_guard(request, instance_id):
        document = await fetch_workflow(request, instance_id, workflow_id)
        connection = await instance_row(request, instance_id)
        digest = content_hash(document)
        if binding(connection, workflow_id, name, digest) != state['fingerprint']:
            fail(409, 'approval_stale', 'The workflow or its connection changed after this proposal. Create a new proposal.')
        if document.get('active') is not False:
            fail(409, 'workflow_active', 'Deactivate the workflow before applying this change. Nothing was written.')
        await before_write()
        snapshot = {'contract': contract.CONTRACT_VERSION, 'repair_id': action_id, 'tool': name,
                    'instance_id': instance_id, 'workflow_id': workflow_id, 'before': writable_document(document),
                    'content_hash': digest, 'version_id': document.get('versionId') if isinstance(document.get('versionId'), str) else None,
                    'desired_hash': state['desired_hash'], 'actor': request.state.user['username']}
        encrypted = request.app.state.vault.encrypt(canonical(snapshot))
        async with request.app.state.db.transaction() as tx:
            await require_current_role(request, 'operator', tx)
            await tx.execute("DELETE FROM documents WHERE key IN (SELECT key FROM documents WHERE key LIKE 'assistant:repair:%' "
                             'ORDER BY rowid DESC LIMIT -1 OFFSET ?)', (MAX_SNAPSHOTS - 1,))
            await tx.execute('INSERT INTO documents(key,value_json) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=CURRENT_TIMESTAMP',
                             (SNAPSHOT_PREFIX + action_id, json.dumps({'encrypted': encrypted})))
            await tx.execute('INSERT INTO audit_events(actor,action,target,details_json,outcome) VALUES(?,?,?,?,?)',
                             (request.state.user['username'], action, label,
                              json.dumps({'content_hash': digest, 'desired_hash': state['desired_hash']}), 'intent'))
        progress['writing'] = True
        sent = False
        try:
            await client.request(instance_id, 'PUT', path, json=state['desired'])
            sent = True
            actual = await client.request(instance_id, 'GET', path)
            actual_hash = content_hash(actual) if _complete(actual) else None
            if actual.get('active') is not False or actual_hash != state['desired_hash']:
                fail(502, 'upstream_outcome_unknown', 'The workflow write could not be verified. Reconcile before proposing another change.')
        except HTTPException as exc:
            code = _code(exc) or 'request_failed'
            unknown = sent or code == 'upstream_outcome_unknown'
            if not unknown:
                progress['writing'] = False  # A definite rejection before any effect.
            await _outcome(request, action, label, {'failure': code}, 'unknown' if unknown else 'failed')
            if sent:
                fail(502, 'upstream_outcome_unknown', 'The workflow write could not be verified. Reconcile before proposing another change.')
            raise
        except asyncio.CancelledError:
            await _outcome(request, action, label, {'failure': 'request_cancelled'}, 'unknown')
            raise
        await _outcome(request, action, label, {'content_hash': actual_hash}, 'success')
    await request.app.state.hub.broadcast({'event': 'fleet.changed', 'instance_id': instance_id, 'workflow_id': workflow_id})
    return contract.RepairResult.model_validate({
        'ok': True, 'tool': name, 'instance_id': instance_id, 'workflow_id': workflow_id, 'repair_id': action_id,
        'verified': True, 'active': False,
        'version_id': actual.get('versionId') if isinstance(actual.get('versionId'), str) else None,
        'content_hash': actual_hash}).model_dump()


async def reconcile(request: Request, action_id: str, status: str, state: dict[str, Any]) -> dict[str, Any]:
    """Read-only comparison of the saved workflow with the proposal's before and desired versions."""
    target = state['target']
    try:
        actual = await N8nClient(request).request(target['instance_id'], 'GET', f"/workflows/{target['workflow_id']}")
    except HTTPException as exc:
        if _code(exc) != 'upstream_not_found':
            raise
        await _authorize(request, 'viewer')
        return {'id': action_id, 'status': status, 'state': 'not_found', 'active': None, 'version_id': None, 'content_hash': None}
    digest = content_hash(actual) if _complete(actual) else None
    await _authorize(request, 'viewer')
    result = ('matches_desired' if digest == state['desired_hash'] else 'matches_before' if digest == state['content_hash']
              else 'different')
    return {'id': action_id, 'status': status, 'state': result,
            'active': actual.get('active') if isinstance(actual.get('active'), bool) else None,
            'version_id': actual.get('versionId') if isinstance(actual.get('versionId'), str) else None,
            'content_hash': digest}
