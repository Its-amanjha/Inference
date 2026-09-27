"""AI cost of n8n runs, read straight from their executions.

The execution list and each finished run's data come from n8n's public API (the same reads as the
Workflows and Errors views), so no telemetry exporter is needed. A finished run is read once and kept for
the telemetry retention period; only its AI calls' counts, model, provider and node names are stored.
A run that is still going is read on a later check.

A run that failed is also recorded in Errors (the failing step and its message, secrets masked), unless
telemetry already recorded that run, so Errors works without telemetry too.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from backend.auth_gate import fail, require_current_role, require_role
from backend.modules.assistant.repair_context import cap_text
from backend.modules.errors.grouping import group_key, run_error_key
from backend.modules.n8n_proxy.client import N8nClient
from .ingest import mask_error_text
from .pricebook import price_snapshot
from .usage import TERMINAL, call_totals, execution_calls, plain, price_calls

router = APIRouter()
SCAN_SECONDS = 45
SUMMARY_ROWS = 20000
TOP_WORKFLOWS = 50
# Kept per call; the rest of an execution (prompts, answers, items) is never stored.
CALL_FIELDS = ('node', 'provider', 'model', 'tokens_in', 'tokens_out', 'cached_tokens', 'reasoning_tokens',
               'cache_write_tokens', 'estimated', 'source')


class Scan(BaseModel):
    model_config = ConfigDict(extra='forbid')
    instance_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')
    cursor: str | None = Field(default=None, min_length=1, max_length=512)
    limit: int = Field(default=20, ge=1, le=50)


def _instant(value: Any) -> str:
    """An ISO time as UTC text that sorts in time order (2026-09-24T03:15:41.764Z), or ''."""
    if not isinstance(value, str) or not value.strip():
        return ''
    try:
        moment = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except ValueError:
        return ''
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def _text(value: Any, limit: int = 256) -> str:
    return value[:limit] if isinstance(value, str) else str(value)[:limit] if isinstance(value, int) and not isinstance(value, bool) else ''


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


FAILED = ('error', 'crashed')


def run_error(execution: Any, status: Any) -> dict | None:
    """The failure n8n recorded for a run that did not succeed: its step and message, secrets masked.
    Descriptions, stacks and run data are never kept. None for a run that did not fail."""
    if status not in FAILED:
        return None
    result = _dict(_dict(_dict(execution).get('data')).get('resultData'))
    error = _dict(result.get('error'))
    message = error.get('message') if isinstance(error.get('message'), str) and error['message'].strip() else None
    if not message:
        message = ('n8n stopped while this run was going.' if status == 'crashed' else
                   'The run failed; Inference could not read its error from n8n.' if not execution else 'The run failed without an error message.')
    return {'node_name': _text(_dict(error.get('node')).get('name')) or _text(result.get('lastNodeExecuted')),
            'error_message': cap_text(mask_error_text(message), 300)[0]}


def _sqlite_time(*values: Any) -> str:
    """The first readable ISO time as the errors table stores times (UTC, YYYY-MM-DD HH:MM:SS), or now."""
    for value in values:
        instant = _instant(value)
        if instant:
            return instant[:19].replace('T', ' ')
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


async def _store(request: Request, instance_id: str, item: dict, state: str, calls: list[dict], workflow: dict,
                 failure: dict | None = None) -> bool:
    """Keep a run's AI calls, and its failure in Errors; returns whether a new failure was recorded."""
    execution_id = _text(item.get('id'), 128)
    totals = call_totals(calls)
    recorded = False
    async with request.app.state.db.transaction() as tx:
        # The read took time; the reader must still hold the role before the result is kept.
        await require_current_role(request, 'viewer', tx)
        if failure and not await tx.fetchone('SELECT 1 FROM errors WHERE instance_id=? AND execution_id=?', (instance_id, execution_id)):
            workflow_id, name = _text(workflow.get('id') or item.get('workflowId'), 128), _text(workflow.get('name'))
            inserted = await tx.fetchone(
                'INSERT INTO errors(instance_id,workflow_id,workflow_name,execution_id,node_name,error_type,error_message,occurred_at,error_key,group_key) '
                'VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(error_key) DO NOTHING RETURNING id',
                (instance_id, workflow_id, name, execution_id, failure['node_name'], 'ExecutionError', failure['error_message'],
                 _sqlite_time(item.get('stoppedAt'), item.get('startedAt')), run_error_key(instance_id, execution_id),
                 group_key(instance_id, workflow_id, failure['node_name'], 'ExecutionError', failure['error_message'])))
            recorded = inserted is not None
        await tx.execute(
            'INSERT INTO run_costs(key,instance_id,execution_id,workflow_id,workflow_name,status,started_at,read_state,calls_json,'
            'ai_calls,unpriced_calls,known_cost,cost_exact,checked_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO NOTHING',
            (f'{instance_id}:{execution_id}', instance_id, execution_id,
             _text(workflow.get('id') or item.get('workflowId'), 128), _text(workflow.get('name')),
             _text(item.get('status'), 32), _instant(item.get('startedAt')) or _instant(datetime.now(timezone.utc).isoformat()),
             state, json.dumps(calls), totals['ai_calls'], totals['unpriced_calls'], totals['known_cost'], totals['cost_exact'],
             datetime.now(timezone.utc).isoformat()))
    return recorded


@router.post('/runs/scan')
async def scan_runs(body: Scan, request: Request) -> dict:
    """Read the AI cost of an instance's newest finished runs that are not on record yet.

    One page of n8n's execution list is checked per request (``cursor`` continues to older runs). A run
    n8n no longer has, or whose data is larger than a response may be, is recorded as unreadable and not
    asked for again. Any other failure stops the check and is reported with what was read before it.
    """
    from .router import retention_days

    actor = require_role(request, 'viewer')
    if not request.app.state.limiter.hit(f"run_costs:{actor['username']}", limit=10, window=60):
        fail(429, 'rate_limited', 'Wait a minute before checking runs again.')
    client = N8nClient(request)
    params: dict[str, Any] = {'limit': body.limit}
    if body.cursor:
        params['cursor'] = body.cursor
    listed = await client.request(body.instance_id, 'GET', '/executions', params=params)
    items = [item for item in listed.get('data') or [] if isinstance(item, dict)][:body.limit]
    ids = [_text(item.get('id'), 128) for item in items]
    keys = [f'{body.instance_id}:{identifier}' for identifier in ids if identifier]
    known = {row['key'] for row in await request.app.state.db.fetchall(
        f"SELECT key FROM run_costs WHERE key IN ({','.join('?' for _ in keys)})", tuple(keys))} if keys else set()
    cutoff = _instant((datetime.now(timezone.utc) - timedelta(days=await retention_days(request.app.state.db))).isoformat())
    prices = await price_snapshot(request.app.state.db)
    deadline = time.monotonic() + SCAN_SECONDS
    counts = {'read': 0, 'known': 0, 'running': 0, 'unreadable': 0, 'errors': 0}
    error, finished, expired = None, True, False
    for item, identifier in zip(items, ids):
        started = _instant(item.get('startedAt'))
        if started and started < cutoff:
            # The list is newest first: this run and every one after it are past the retention period.
            expired = True
            break
        if not identifier or f'{body.instance_id}:{identifier}' in known:
            counts['known'] += bool(identifier)
            continue
        if item.get('status') not in TERMINAL:
            # Read on a later check of the newest runs, once it has finished.
            counts['running'] += 1
            continue
        if time.monotonic() > deadline:
            finished = False
            break
        state, calls, workflow, remote = 'read', [], {}, None
        try:
            remote = await client.request(body.instance_id, 'GET', f'/executions/{identifier}', params={'includeData': 'true'})
        except HTTPException as failure:
            code = failure.detail.get('code') if isinstance(failure.detail, dict) else None
            if code not in ('upstream_not_found', 'upstream_invalid_response'):
                error = {'code': code or 'upstream_error', 'detail': failure.detail.get('detail') if isinstance(failure.detail, dict) else 'The n8n instance did not answer.'}
                finished = False
                break
            state = 'unreadable'
            counts['unreadable'] += 1
        else:
            workflow = remote.get('workflowData') if isinstance(remote.get('workflowData'), dict) else {}
            calls = [{key: call[key] for key in CALL_FIELDS} for call in execution_calls(remote)]
            counts['read'] += 1
        failure = run_error(remote, (remote or {}).get('status') or item.get('status'))
        counts['errors'] += await _store(request, body.instance_id, item, state, price_calls(calls, prices), workflow, failure)
    if counts['errors']:
        await request.app.state.hub.broadcast({'event': 'errors.changed', 'instance_id': body.instance_id})
    more = listed.get('nextCursor') if isinstance(listed.get('nextCursor'), str) and not expired else None
    return {'instance_id': body.instance_id, 'listed': len(items), **counts, 'error': error,
            # Only a fully handled page moves on to older runs; otherwise the same page is checked again.
            'next_cursor': more if finished else body.cursor, 'complete': finished and not more}


def _run(row: dict) -> dict:
    try:
        calls = json.loads(row['calls_json'])
    except ValueError:
        calls = []
    return {'instance_id': row['instance_id'], 'execution_id': row['execution_id'], 'workflow_id': row['workflow_id'] or None,
            'workflow_name': row['workflow_name'] or None, 'status': row['status'] or None, 'started_at': row['started_at'] or None,
            'read_state': row['read_state'], 'ai_calls': row['ai_calls'], 'unpriced_calls': row['unpriced_calls'],
            'known_cost_usd': row['known_cost'], 'cost_usd': row['cost_exact'], 'calls': calls if isinstance(calls, list) else [],
            'checked_at': row['checked_at']}


@router.get('/runs')
async def list_runs(request: Request, instance_id: str | None = Query(None, min_length=1, max_length=128),
                    workflow_id: str | None = Query(None, min_length=1, max_length=128),
                    limit: int = Query(25, ge=1, le=100), before: str | None = Query(None, min_length=3, max_length=300),
                    ai: bool = False) -> dict:
    """Recorded runs, newest first, with each AI call and its cost; ``ai`` keeps only runs with AI calls."""
    require_role(request, 'viewer')
    if workflow_id and not instance_id:
        fail(422, 'instance_required', 'Choose an instance to filter by workflow.')
    where: list[str] = []
    params: list[str] = []
    if instance_id:
        where.append('instance_id=?')
        params.append(instance_id)
    if workflow_id:
        where.append('workflow_id=?')
        params.append(workflow_id)
    if ai:
        where.append('ai_calls>0')
    if before:
        started, _, key = before.partition('|')
        if not key:
            fail(422, 'invalid_cursor', 'The page cursor is not valid.')
        where.append('(started_at<? OR (started_at=? AND key<?))')
        params.extend((started, started, key))
    rows = await request.app.state.db.fetchall(
        'SELECT * FROM run_costs' + (' WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY started_at DESC,key DESC LIMIT ?',
        (*params, limit + 1))
    more = len(rows) > limit
    rows = rows[:limit]
    return {'data': [_run(row) for row in rows], 'next_cursor': f"{rows[-1]['started_at']}|{rows[-1]['key']}" if more else None}


@router.get('/runs/summary')
async def runs_summary(request: Request, instance_id: str | None = Query(None, min_length=1, max_length=128),
                       days: int = Query(7, ge=1, le=365)) -> dict:
    """AI cost of the recorded runs that started in the last ``days`` days, in total and per workflow.
    It covers only runs Inference has read; ``cost_usd`` is null while any AI call has no price."""
    require_role(request, 'viewer')
    since = _instant((datetime.now(timezone.utc) - timedelta(days=days)).isoformat())
    rows = await request.app.state.db.fetchall(
        'SELECT instance_id,workflow_id,workflow_name,read_state,ai_calls,unpriced_calls,known_cost FROM run_costs WHERE started_at>=?'
        + (' AND instance_id=?' if instance_id else '') + ' ORDER BY started_at DESC LIMIT ?',
        (since, *((instance_id,) if instance_id else ()), SUMMARY_ROWS + 1))
    truncated = len(rows) > SUMMARY_ROWS
    workflows: dict[tuple[str, str], dict] = {}
    total = {'runs': 0, 'ai_runs': 0, 'ai_calls': 0, 'unpriced_calls': 0, 'unreadable_runs': 0, 'known': Decimal(0)}
    for row in rows[:SUMMARY_ROWS]:
        entry = workflows.setdefault((row['instance_id'], row['workflow_id']), {
            'instance_id': row['instance_id'], 'workflow_id': row['workflow_id'] or None, 'workflow_name': row['workflow_name'] or None,
            'runs': 0, 'ai_runs': 0, 'ai_calls': 0, 'unpriced_calls': 0, 'known': Decimal(0)})
        entry['workflow_name'] = entry['workflow_name'] or row['workflow_name'] or None
        cost = Decimal(row['known_cost'])
        for bucket in (total, entry):
            bucket['runs'] += 1
            bucket['ai_runs'] += row['ai_calls'] > 0
            bucket['ai_calls'] += row['ai_calls']
            bucket['unpriced_calls'] += row['unpriced_calls']
            bucket['known'] += cost
        total['unreadable_runs'] += row['read_state'] != 'read'

    def money(bucket: dict) -> dict:
        known = bucket.pop('known')
        return {**bucket, 'known_cost_usd': plain(known), 'cost_usd': None if bucket['unpriced_calls'] else plain(known)}

    ranked = sorted((entry for entry in workflows.values() if entry['ai_calls']), key=lambda entry: (-entry['known'], -entry['ai_calls']))
    return {'days': days, 'since': since, **money(total), 'truncated': truncated,
            'by_workflow': [money(entry) for entry in ranked[:TOP_WORKFLOWS]]}
