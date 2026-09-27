"""Errors grouped by problem, not by run; failures read straight from n8n's executions; one record per run
whichever source reports it first; the live "errors changed" event."""
from datetime import datetime, timedelta, timezone
import sqlite3

import httpx

from backend.modules.errors.grouping import group_key, normalize
from test_fleet import add, fleet  # noqa: F401  (fixture)
from test_observability import _otlp, _span, issue, send


def _ago(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat().replace('+00:00', 'Z')


def test_run_specific_values_do_not_split_a_problem():
    assert normalize('Order 1042 not found') == normalize('Order 1043 not found') == 'order # not found'
    assert normalize('Request 3f2b9c1e-4a5d-4e6f-8a7b-9c0d1e2f3a4b failed at 2026-09-27T10:15:00Z') == 'request # failed at #'
    assert normalize('Mail to a.person@example.com bounced (id 9f86d081884c7d65)') == 'mail to # bounced (id #)'
    # Status codes and plain words stay, so different problems stay apart.
    assert normalize('Request failed with status code 404') != normalize('Request failed with status code 500')
    assert normalize('  Two\n  lines  ') == 'two lines'
    same = group_key('i', 'wf', 'HTTP Request', 'ExecutionError', 'Order 1042 not found')
    assert same == group_key('i', 'wf', 'HTTP Request', 'ExecutionError', 'Order 7 not found'.replace('7', '99999'))
    assert same != group_key('i', 'wf', 'Other step', 'ExecutionError', 'Order 1042 not found')


async def _fail(fleet, token, trace, execution, message, node='HTTP Request'):
    attrs = [('n8n.workflow.id', 'wf-1'), ('n8n.workflow.name', 'Orders'), ('n8n.execution.id', execution)]
    response = await send(fleet, token, _otlp(_span(trace, '2' * 16, 'node ' + node, 2, message, [('n8n.node.name', node), *attrs], parent='1' * 16),
                                              _span(trace, '1' * 16, 'workflow.execute', 2, 'Workflow failed', attrs)))
    assert response.status_code == 200, response.text


async def _open(fleet, instance, status='open'):
    return (await fleet.client.get('/api/errors', params={'instance_id': instance, 'status': status})).json()['data']


async def test_the_same_failure_in_many_runs_is_one_error(fleet):
    instance, token = await issue(fleet)
    await _fail(fleet, token['token'], 'a' * 32, 'ex1', 'Order 1042 not found')
    await _fail(fleet, token['token'], 'b' * 32, 'ex2', 'Order 1043 not found')
    await _fail(fleet, token['token'], 'c' * 32, 'ex3', 'Request failed with status code 500')
    rows = await _open(fleet, instance)
    assert sorted((row['count'], row['error_message']) for row in rows) == [(1, 'Request failed with status code 500'), (2, 'Order 1043 not found')]
    order = next(row for row in rows if row['count'] == 2)
    assert order['execution_id'] == 'ex2' and order['first_seen'] <= order['occurred_at'] and order['returned'] is False
    # Resolving the problem resolves every run of it; the undo brings back exactly those runs.
    assert (await fleet.client.post(f"/api/errors/{order['id']}/resolve", headers=fleet.headers)).status_code == 200
    assert [row['count'] for row in await _open(fleet, instance)] == [1]
    resolved = await _open(fleet, instance, 'resolved')
    assert [(row['count'], row['status']) for row in resolved] == [(2, 'resolved')] and resolved[0]['resolved_at']
    assert (await fleet.client.post(f"/api/errors/{order['id']}/reopen", headers=fleet.headers)).status_code == 200
    reopened = next(row for row in await _open(fleet, instance) if row['count'] == 2)
    assert reopened['returned'] is False and await _open(fleet, instance, 'resolved') == []
    # A problem that happens again after it was resolved is open again, marked as come back.
    await fleet.client.post(f"/api/errors/{order['id']}/resolve", headers=fleet.headers)
    await _fail(fleet, token['token'], 'd' * 32, 'ex4', 'Order 1044 not found')
    back = next(row for row in await _open(fleet, instance) if row['error_message'].startswith('Order'))
    assert (back['count'], back['returned'], back['execution_id']) == (3, True, 'ex4')
    assert (await fleet.client.post('/api/errors/999999/resolve', headers=fleet.headers)).status_code == 404


def _run(identifier, status, error=None, node='Fetch order', started=None):
    started = started or _ago(minutes=10)
    result = {'runData': {}, **({'error': error} if error else {}), 'lastNodeExecuted': node}
    return {'id': identifier, 'status': status, 'startedAt': started, 'stoppedAt': started, 'workflowId': 'wf-orders',
            'workflowData': {'id': 'wf-orders', 'name': 'Order sync', 'nodes': [], 'connections': {}},
            'data': {'resultData': result}}


def _listing(*runs):
    return httpx.Response(200, json={'data': [{key: run[key] for key in ('id', 'status', 'startedAt', 'stoppedAt', 'workflowId')} for run in runs],
                                     'nextCursor': None})


async def test_failed_runs_read_from_n8n_become_grouped_errors_without_telemetry(fleet):
    instance = await add(fleet)
    events = []
    original = fleet.app.state.hub.broadcast

    async def capture(event):
        events.append(event)
        await original(event)
    fleet.app.state.hub.broadcast = capture
    secret = 'sk-live-abcdefghijklmnop1234567890'
    error = lambda order: {'name': 'NodeApiError', 'node': {'name': 'Fetch order', 'type': 'n8n-nodes-base.httpRequest'},
                           'message': f'Order {order} not found. Authorization: Bearer {secret}',
                           'description': f'GET https://shop.example/orders/{order} with key {secret}', 'stack': f'at fetch ({secret})'}
    first, second = _run('11', 'error', error(1042), started='2026-09-27T10:00:00.000Z'), _run('12', 'error', error(1043))
    crashed, gone, fine = _run('13', 'crashed', node='Wait'), _run('14', 'error'), _run('15', 'success')
    first['startedAt'] = first['stoppedAt'] = _ago(minutes=30)
    fleet.replies[('alpha.example', 'GET', '/api/v1/executions')] = _listing(fine, gone, crashed, second, first)
    for run in (first, second, crashed, fine):
        fleet.replies[('alpha.example', 'GET', f"/api/v1/executions/{run['id']}")] = httpx.Response(200, json=run)
    fleet.replies[('alpha.example', 'GET', '/api/v1/executions/14')] = httpx.Response(404, json={'message': 'Not Found'})
    result = (await fleet.client.post('/api/otel/runs/scan', headers=fleet.headers, json={'instance_id': instance})).json()
    assert (result['read'], result['unreadable'], result['errors']) == (4, 1, 4)
    assert [event for event in events if event['event'] == 'errors.changed'] == [{'event': 'errors.changed', 'instance_id': instance}]
    rows = await _open(fleet, instance)
    order = next(row for row in rows if row['node_name'] == 'Fetch order' and row['count'] == 2)
    assert order['workflow_name'] == 'Order sync' and order['execution_id'] == '12' and order['error_type'] == 'ExecutionError'
    assert order['first_seen'] < order['occurred_at']
    assert {row['error_message'] for row in rows if row['count'] == 1} == {
        'n8n stopped while this run was going.', 'The run failed; Inference could not read its error from n8n.'}
    # Only the message is kept, with its secret masked; descriptions and stacks never are.
    stored = str(await fleet.app.state.db.fetchall('SELECT * FROM errors'))
    assert secret not in stored and secret not in str(rows) and 'shop.example' not in stored and 'at fetch' not in stored
    # A second check reads nothing again and records nothing twice.
    again = (await fleet.client.post('/api/otel/runs/scan', headers=fleet.headers, json={'instance_id': instance})).json()
    assert (again['read'], again['errors']) == (0, 0) and len(await fleet.app.state.db.fetchall('SELECT id FROM errors')) == 4


async def test_a_run_is_recorded_once_whichever_source_reports_it_first(fleet):
    instance, token = await issue(fleet)
    failed = _run('ex1', 'error', {'message': 'Request failed', 'node': {'name': 'HTTP Request'}}, node='HTTP Request')
    fleet.replies[('alpha.example', 'GET', '/api/v1/executions')] = _listing(failed)
    fleet.replies[('alpha.example', 'GET', '/api/v1/executions/ex1')] = httpx.Response(200, json=failed)
    # n8n first: the telemetry for the same run adds nothing.
    await fleet.client.post('/api/otel/runs/scan', headers=fleet.headers, json={'instance_id': instance})
    await _fail(fleet, token['token'], 'e' * 32, 'ex1', 'Request failed')
    assert [row['count'] for row in await _open(fleet, instance)] == [1]
    # Telemetry first: reading the run from n8n adds nothing.
    await _fail(fleet, token['token'], 'f' * 32, 'ex2', 'Request failed')
    later = _run('ex2', 'error', {'message': 'Request failed', 'node': {'name': 'HTTP Request'}}, node='HTTP Request')
    fleet.replies[('alpha.example', 'GET', '/api/v1/executions')] = _listing(later)
    fleet.replies[('alpha.example', 'GET', '/api/v1/executions/ex2')] = httpx.Response(200, json=later)
    await fleet.app.state.db.execute('DELETE FROM run_costs')
    result = (await fleet.client.post('/api/otel/runs/scan', headers=fleet.headers, json={'instance_id': instance})).json()
    assert result['errors'] == 0
    rows = await fleet.app.state.db.fetchall('SELECT execution_id FROM errors ORDER BY execution_id')
    assert [row['execution_id'] for row in rows] == ['ex1', 'ex2']


async def test_telemetry_failures_announce_themselves(fleet):
    instance, token = await issue(fleet)
    events = []
    original = fleet.app.state.hub.broadcast

    async def capture(event):
        events.append(event)
        await original(event)
    fleet.app.state.hub.broadcast = capture
    await _fail(fleet, token['token'], 'a' * 32, 'ex1', 'Request failed')
    await _fail(fleet, token['token'], 'a' * 32, 'ex1', 'Request failed')  # the same spans again: nothing new
    assert events == [{'event': 'errors.changed', 'instance_id': instance}]


async def test_a_version_8_database_gains_error_groups_and_old_records_are_grouped(tmp_path):
    from backend.database import SCHEMA_VERSION, Database
    from backend.modules.observability.router import cleanup

    path = tmp_path / 'eight.db'
    database = Database(path)
    await database.open()
    await database.close()
    with sqlite3.connect(path) as connection:
        connection.execute('DROP INDEX idx_errors_group')
        connection.execute('ALTER TABLE errors DROP COLUMN group_key')
        connection.execute("INSERT INTO instances(id,name,url,api_key_enc) VALUES ('one','One','https://n8n.example','x')")
        for execution in ('ex1', 'ex2'):
            connection.execute("INSERT INTO errors(instance_id,workflow_id,workflow_name,execution_id,node_name,error_type,error_message,error_key) "
                               "VALUES ('one','wf','Orders',?,'Fetch','ExecutionError','Order 1042 not found',?)", (execution, execution))
        connection.execute('PRAGMA user_version=8')
    for _ in range(2):
        await database.open()
        try:
            assert (await database.fetchone('PRAGMA user_version'))['user_version'] == SCHEMA_VERSION == 9
            await cleanup(database)
            keys = {row['group_key'] for row in await database.fetchall('SELECT group_key FROM errors')}
            assert keys == {group_key('one', 'wf', 'Fetch', 'ExecutionError', 'Order 1042 not found')}
        finally:
            await database.close()
