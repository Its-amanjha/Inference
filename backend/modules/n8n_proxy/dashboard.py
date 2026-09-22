"""Bounded aggregation of available n8n history; incomplete totals remain null."""
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from fastapi import HTTPException, Request
from backend.auth_gate import require_role, fail
from .client import N8nClient


async def aggregate(request: Request, instance_id: str | None) -> dict[str, Any]:
    actor = require_role(request, 'viewer')
    if not request.app.state.limiter.hit(f"dashboard:{actor['username']}", limit=4, window=60):
        fail(429, 'rate_limited', 'Wait before refreshing dashboard metrics.')
    instances = await request.app.state.db.fetchall('SELECT id,name FROM instances' + (' WHERE id=?' if instance_id else ''), (instance_id,) if instance_id else ())
    if instance_id and not instances:
        fail(404, 'instance_not_found', 'Instance not found.')
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=24)
    client = N8nClient(request)

    async def scan(instance: str, resource: str) -> tuple[list, bool]:
        rows, ids, cursors = [], set(), set()
        cursor = None
        try:
            for _ in range(10):
                params = {'limit':250, **({'includeData':'false'} if resource == 'executions' else {'excludePinnedData':'true'})}
                if cursor:
                    params['cursor'] = cursor
                page = await client.request(instance, 'GET', '/' + resource, params=params)
                if not isinstance(page, dict) or not isinstance(page.get('data'), list) or len(page['data']) > 250:
                    return rows, False
                for row in page['data']:
                    if not isinstance(row, dict) or not isinstance(row.get('id'), (str, int)) or str(row['id']) in ids:
                        return rows, False
                    ids.add(str(row['id']))
                    rows.append(row)
                cursor = page.get('nextCursor')
                if not cursor:
                    return rows, True
                if not isinstance(cursor, str) or len(cursor) > 2048 or cursor in cursors:
                    return rows, False
                cursors.add(cursor)
        except HTTPException as error:
            if error.status_code in (401, 403):
                raise
        return rows, False

    async def one(instance: dict) -> dict:
        workflows, workflow_complete = await scan(instance['id'], 'workflows')
        executions, execution_complete = await scan(instance['id'], 'executions')
        count = success = terminal = 0
        for row in executions:
            try:
                stamp = datetime.fromisoformat(row['startedAt'].replace('Z', '+00:00'))
                if stamp.tzinfo is None:
                    raise ValueError('Timezone required')
            except (KeyError, TypeError, ValueError, AttributeError):
                execution_complete = False
                continue
            if not start <= stamp <= now:
                continue
            count += 1
            status = row.get('status')
            if status in ('success', 'error', 'crashed', 'canceled'):
                terminal += 1
                success += status == 'success'
            elif status not in ('running', 'waiting', 'new'):
                execution_complete = False
        return {'instance_id':instance['id'], 'name':instance['name'], 'observed_workflows':len(workflows),
                'active_workflows':sum(1 for row in workflows if isinstance(row, dict) and row.get('active') is True),
                'observed_executions_24h':count, 'successes':success, 'terminal':terminal,
                'workflows_complete':workflow_complete, 'executions_complete':execution_complete}

    pending = iter(instances)
    results = []
    async def worker() -> None:
        for instance in pending:
            try:
                result = await asyncio.wait_for(one(instance), timeout=30)
            except asyncio.TimeoutError:
                result = {'instance_id':instance['id'], 'name':instance['name'], 'observed_workflows':0, 'active_workflows':0,
                          'observed_executions_24h':0, 'successes':0, 'terminal':0,
                          'workflows_complete':False, 'executions_complete':False}
            results.append(result)
    workers = [asyncio.create_task(worker()) for _ in range(min(3,len(instances)))]
    try:
        await asyncio.gather(*workers)
    finally:
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
    workflow_complete = all(row['workflows_complete'] for row in results)
    execution_complete = all(row['executions_complete'] for row in results)
    terminal = sum(row['terminal'] for row in results)
    silent = await request.app.state.db.fetchone(
        "SELECT COUNT(*) AS count FROM otel_spans WHERE silent=1 AND health_status='error' AND start_ns>=? AND start_ns<=?" + (' AND instance_id=?' if instance_id else ''),
        (int(start.timestamp()*1_000_000_000),int(now.timestamp()*1_000_000_000)) + ((instance_id,) if instance_id else ()))
    return {'workflows':sum(row['observed_workflows'] for row in results) if workflow_complete else None,
            'executions_24h':sum(row['observed_executions_24h'] for row in results) if execution_complete else None,
            'terminal_executions_24h':terminal if execution_complete else None,
            'success_rate_percent':round(sum(row['successes'] for row in results)*100/terminal,2) if execution_complete and terminal else None,
            'confirmed_silent_error_spans_24h':silent['count'], 'window_start':start.isoformat(), 'window_end':now.isoformat(),
            'data':results, 'scope':'Available saved n8n executions started in the last 24 hours; pruned or unsaved executions are not included.',
            'limits':{'pages_per_resource':10,'rows_per_page':250,'concurrency':3,'instance_timeout_seconds':30}}
