"""Durable, exact-action proposals. The cache is bounded; SQLite owns state."""
import asyncio
from copy import deepcopy
import json
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import HTTPException, Request

from backend.auth_gate import ROLES, fail, require_current_role, require_role
from backend.database import _settle
from backend.modules.n8n_proxy.client import instance_guard

TTL = 15 * 60
MAX_PENDING = 256
MAX_OWN_PENDING = 32
MCP_WARNING = 'MCP approval binds the server configuration and tool schema; remote resource state cannot be verified.'


async def initialize(app: Any) -> None:
    app.state.assistant_pending = {}
    app.state.assistant_slots = asyncio.Semaphore(4)
    async with app.state.db.transaction() as tx:
        await tx.execute("INSERT INTO audit_events(actor,action,target,outcome) SELECT actor,'assistant.approval',id,'expired' FROM approval_actions WHERE status='pending'")
        await tx.execute("INSERT INTO audit_events(actor,action,target,outcome) SELECT actor,'assistant.approval',id,'uncertain' FROM approval_actions WHERE status='executing'")
        await tx.execute("UPDATE approval_actions SET status='expired',updated_at=CURRENT_TIMESTAMP WHERE status='pending'")
        await tx.execute("UPDATE approval_actions SET status='uncertain',updated_at=CURRENT_TIMESTAMP WHERE status='executing'")


async def event(tx: Any, actor: str, action_id: str, status: str) -> None:
    await tx.execute('INSERT INTO audit_events(actor,action,target,outcome) VALUES(?,?,?,?)',
                     (actor, 'assistant.approval', action_id, status))


async def current(request: Request, role: str) -> None:
    async with request.app.state.db.transaction() as tx:
        await require_current_role(request, role, tx)


def required_role(name: str) -> str:
    from . import tools
    return 'admin' if name == 'mcp_call' else tools.role(name)


def is_repair(name: str) -> bool:
    from . import tools
    return name in tools.REPAIR_PROPOSALS


@asynccontextmanager
async def guard(request: Request, name: str, arguments: dict[str, Any]):
    if name == 'mcp_call':
        # Serialize MCP configuration writes with confirmations in this process.
        async with instance_guard(request, 'assistant:mcp:' + arguments['server_id']):
            yield
    else:
        async with instance_guard(request, arguments['instance_id']):
            yield


async def fingerprint(request: Request, name: str, arguments: dict[str, Any]) -> str:
    if name == 'mcp_call':
        from .service import mcp_fingerprint
        return await mcp_fingerprint(request, arguments)
    from . import tools
    return await tools.fingerprint(request, name, arguments)


def present(name: str, action_id: str, status: str, expires_at: float, state: dict[str, Any],
            linked_status: dict[str, str] | None = None) -> dict[str, Any]:
    """The `pending_action` object: contract PendingRepairAction for repairs, the original shape otherwise."""
    if is_repair(name):
        preview = deepcopy(state['preview'])
        for condition in preview.get('preconditions', []):
            if condition.get('action_id') and linked_status is not None:
                condition['satisfied'] = linked_status.get(condition['action_id']) == 'succeeded'
        return {'id': action_id, 'name': name, 'status': status, 'expires_at': expires_at,
                'chat_id': state.get('chat_id'), 'warning': state.get('warning'), 'arguments': state['arguments'],
                'target': state['target'], 'preview': preview}
    return {'id': action_id, 'name': name, 'arguments': state['arguments'], 'expires_at': expires_at, 'status': status,
            'warning': MCP_WARNING if name == 'mcp_call' else None, 'chat_id': state.get('chat_id'),
            'linked_to': state.get('linked_to'), 'preview': None}


def list_item(row: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """One `/approvals` row; repair rows carry the Target object and preview summary."""
    item = {key: row[key] for key in ('id', 'name', 'target', 'status', 'expires_at', 'created_at')}
    item.update(arguments=state['arguments'], chat_id=state.get('chat_id'), linked_to=state.get('linked_to'),
                preview_summary=None)
    if is_repair(row['name']):
        item.update(target=state['target'], preview_summary=state['preview'].get('summary_text'))
    return item


def decode(request: Request, row: dict[str, Any]) -> dict[str, Any]:
    return json.loads(request.app.state.vault.decrypt(row['state_enc']))


def _record(action_id: str, name: str, arguments: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    return {'id': action_id, 'name': name, 'state': state,
            'target': arguments.get('instance_id', arguments.get('server_id', ''))}


async def prepare(request: Request, name: str, arguments: dict[str, Any], *, chat_id: str | None = None,
                  seen: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Validate and bind one proposal (and any linked deactivation) without storing anything.

    `seen` carries the workflow context views the model was given, so code it saw truncated stays blocked.
    """
    from . import tools
    from .service import validate_mcp_arguments
    arguments = validate_mcp_arguments(arguments) if name == 'mcp_call' else tools.validate(name, arguments)
    if name != 'mcp_call' and name not in tools.MUTATIONS:
        fail(422, 'not_a_mutation', 'This tool does not require an approval.')
    require_role(request, required_role(name))
    action_id = secrets.token_urlsafe(24)
    records: list[dict[str, Any]] = []
    async with guard(request, name, arguments):
        if name in tools.REPAIR_PROPOSALS:
            from . import repair_actions

            async def link_deactivation(instance_id: str, workflow_id: str) -> str:
                linked = tools.validate('deactivate_workflow', {'instance_id': instance_id, 'workflow_id': workflow_id})
                linked_id = secrets.token_urlsafe(24)
                digest = await fingerprint(request, 'deactivate_workflow', linked)
                records.append(_record(linked_id, 'deactivate_workflow', linked, {
                    'arguments': linked, 'fingerprint': digest, 'chat_id': chat_id, 'linked_to': action_id}))
                return linked_id

            prepared = await repair_actions.prepare(request, name, arguments, link_deactivation=link_deactivation, seen=seen)
            state = {'arguments': arguments, 'chat_id': chat_id, **prepared}
        else:
            state = {'arguments': arguments, 'fingerprint': await fingerprint(request, name, arguments)}
            if chat_id is not None:
                state['chat_id'] = chat_id
            if name == 'mcp_call':
                from .service import mcp_binding
                state['binding'] = await mcp_binding(request, arguments['server_id'])
        records.append(_record(action_id, name, arguments, state))
    return records


async def store(request: Request, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Store prepared proposals atomically: every record becomes pending, or none does."""
    if not records:
        return []
    actor = require_role(request, 'viewer')
    role = max((required_role(record['name']) for record in records), key=lambda item: ROLES[item])
    expires = time.time() + TTL
    encrypted = [request.app.state.vault.encrypt(json.dumps(record['state'], sort_keys=True, allow_nan=False)) for record in records]
    async with request.app.state.db.transaction() as tx:
        await require_current_role(request, role, tx)
        await tx.execute("UPDATE approval_actions SET status='expired',updated_at=CURRENT_TIMESTAMP WHERE status='pending' AND expires_at<=?", (time.time(),))
        count = await tx.fetchone("SELECT count(*) AS n FROM approval_actions WHERE status='pending'")
        own = await tx.fetchone("SELECT count(*) AS n FROM approval_actions WHERE status='pending' AND actor=?", (actor['username'],))
        if count['n'] + len(records) > MAX_PENDING or own['n'] + len(records) > MAX_OWN_PENDING:
            fail(429, 'approval_limit', 'Resolve pending proposals before creating more.')
        for record, state_enc in zip(records, encrypted):
            await tx.execute('INSERT INTO approval_actions(id,actor,session_id,name,target,status,state_enc,expires_at) VALUES(?,?,?,?,?,?,?,?)',
                (record['id'], actor['username'], actor['session_id'], record['name'], record['target'], 'pending', state_enc, expires))
            await event(tx, actor['username'], record['id'], 'pending')
    cache = request.app.state.assistant_pending
    for key, expiry in list(cache.items()):
        if expiry <= time.time():
            cache.pop(key, None)
    for record in records:
        if len(cache) >= MAX_PENDING:
            cache.pop(next(iter(cache)))
        cache[record['id']] = expires
    return [present(record['name'], record['id'], 'pending', expires, record['state']) for record in records]


async def propose(request: Request, name: str, arguments: dict[str, Any], *, chat_id: str | None = None) -> dict[str, Any]:
    actions = await store(request, await prepare(request, name, arguments, chat_id=chat_id))
    result: dict[str, Any] = {'pending_action': actions[-1]}
    if len(actions) > 1:
        result['linked_actions'] = actions[:-1]
    return result


async def owned(request: Request, tx: Any, action_id: str) -> dict[str, Any]:
    actor = require_role(request, 'viewer')
    row = await tx.fetchone('SELECT * FROM approval_actions WHERE id=? AND actor=? AND session_id=?',
                            (action_id, actor['username'], actor['session_id']))
    if not row:
        fail(404, 'approval_not_found', 'The proposal was not found for this session.')
    return row


async def detail(request: Request, action_id: str) -> dict[str, Any]:
    actor = require_role(request, 'viewer')
    async with request.app.state.db.transaction() as tx:
        await require_current_role(request, 'viewer', tx)
        await tx.execute("UPDATE approval_actions SET status='expired',updated_at=CURRENT_TIMESTAMP WHERE status='pending' AND expires_at<=?", (time.time(),))
        row = await owned(request, tx, action_id)
        state = decode(request, row)
        linked: dict[str, str] = {}
        if is_repair(row['name']):
            for condition in state['preview'].get('preconditions', []):
                if condition.get('action_id'):
                    found = await tx.fetchone('SELECT status FROM approval_actions WHERE id=? AND actor=? AND session_id=?',
                                              (condition['action_id'], actor['username'], actor['session_id']))
                    if found:
                        linked[condition['action_id']] = found['status']
    return present(row['name'], row['id'], row['status'], row['expires_at'], state, linked)


async def finish(request: Request, action_id: str, status: str) -> None:
    async with request.app.state.db.transaction() as tx:
        row = await tx.fetchone('SELECT status,expires_at FROM approval_actions WHERE id=?', (action_id,))
        if not row or row['status'] != 'executing':
            return
        if status == 'pending' and row['expires_at'] <= time.time():
            status = 'expired'
        await tx.execute("UPDATE approval_actions SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='executing'", (status, action_id))
        await event(tx, request.state.user['username'], action_id, status)
    if status != 'pending':
        request.app.state.assistant_pending.pop(action_id, None)


async def confirm(request: Request, action_id: str) -> dict[str, Any]:
    # Claim is committed before any awaited remote work, including the recheck.
    status = 'failed'
    writing = False
    claimed = False
    name = ''
    progress = {'writing': False}
    try:
        async with request.app.state.db.transaction() as tx:
            row = await owned(request, tx, action_id)
            await require_current_role(request, required_role(row['name']), tx)
            if row['status'] != 'pending' or row['expires_at'] <= time.time():
                fail(409, 'approval_unavailable', 'The proposal has expired or has already been consumed.')
            await tx.execute("UPDATE approval_actions SET status='executing',updated_at=CURRENT_TIMESTAMP WHERE id=?", (action_id,))
            await event(tx, row['actor'], action_id, 'executing')
            claimed = True
        name = row['name']
        state = decode(request, row)
        arguments = state['arguments']
        async with guard(request, name, arguments):
            async def before_write() -> None:
                if row['expires_at'] <= time.time():
                    fail(409, 'approval_expired', 'The proposal expired before execution.')
                await current(request, required_role(name))
            if is_repair(name):
                from . import repair_actions
                await current(request, required_role(name))
                result = await repair_actions.execute(request, action_id, name, state, before_write=before_write, progress=progress)
                status = 'succeeded'
                return {'id': action_id, 'status': status, 'result': result}
            if await fingerprint(request, name, arguments) != state['fingerprint']:
                status = 'stale'
                fail(409, 'approval_stale', 'The target changed. Create a new proposal.')
            await current(request, required_role(name))
            await before_write()
            writing = True
            if name == 'mcp_call':
                from .service import execute_mcp
                result = await execute_mcp(request, arguments, expected_binding=state['binding'], before_call=before_write)
            else:
                from . import tools
                result = await tools.execute(request, name, arguments, expected_fingerprint=state['fingerprint'])
            status = 'failed' if name == 'mcp_call' and result.get('isError') is True else 'succeeded'
            return {'id': action_id, 'status': status, 'result': result}
    except HTTPException as exc:
        code = exc.detail.get('code', '') if isinstance(exc.detail, dict) else ''
        if 'unknown' in code:
            status = 'uncertain'
        elif code == 'approval_stale':
            status = 'stale'
        elif code == 'approval_expired':
            status = 'expired'
        elif code == 'workflow_active' and is_repair(name) and not progress['writing']:
            # Nothing was sent: release the claim so the linked deactivation can run first.
            status = 'pending'
        raise
    except BaseException:
        if writing or progress['writing']:
            status = 'uncertain'
        raise
    finally:
        if claimed:
            await _settle(finish(request, action_id, status))


async def cancel(request: Request, action_id: str) -> dict[str, Any]:
    async with request.app.state.db.transaction() as tx:
        row = await owned(request, tx, action_id)
        await require_current_role(request, 'viewer', tx)
        if row['status'] != 'pending':
            fail(409, 'approval_unavailable', 'The proposal has already been consumed.')
        await tx.execute("UPDATE approval_actions SET status='cancelled',updated_at=CURRENT_TIMESTAMP WHERE id=?", (action_id,))
        await event(tx, row['actor'], action_id, 'cancelled')
    request.app.state.assistant_pending.pop(action_id, None)
    return {'id': action_id, 'status': 'cancelled'}
