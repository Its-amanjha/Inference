"""AI cost of n8n runs read from their executions: AI service responses to HTTP Request nodes, snapshot
prices, the scan/list/summary routes, re-pricing, retention and the schema migration."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import sqlite3
import time

import httpx
import pytest

from backend.modules.observability.pricing import calculate_cost
from backend.modules.observability.usage import execution_calls, response_usage
from test_fleet import add, fleet  # noqa: F401  (fixture)
from test_observability import issue, send


def _ago(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat().replace('+00:00', 'Z')


# The shape OpenAI's Responses API returned for a real run (counts only).
RESPONSES = {'id': 'resp_1', 'object': 'response', 'model': 'gpt-4.1-2025-04-14', 'output': [{'content': [{'text': 'private answer'}]}],
             'usage': {'input_tokens': 28567, 'input_tokens_details': {'cache_write_tokens': 0, 'cached_tokens': 0},
                       'output_tokens': 2699, 'output_tokens_details': {'reasoning_tokens': 0}, 'total_tokens': 31266}}


def _http(name, url, *bodies, runs=1):
    node = {'name': name, 'type': 'n8n-nodes-base.httpRequest', 'parameters': {'url': url, 'method': 'POST'}}
    return node, [{'data': {'main': [[{'json': body} for body in bodies]]}} for _ in range(runs)]


def _execution(*nodes, identifier='2027', status='success', started=None, workflow=('wf-pdf', 'Source Faithful PDF to DOCX')):
    return {'id': identifier, 'status': status, 'startedAt': started or _ago(minutes=5),
            'workflowData': {'id': workflow[0], 'name': workflow[1], 'nodes': [node for node, _ in nodes], 'connections': {}},
            'data': {'resultData': {'runData': {node['name']: runs for node, runs in nodes}}}}


def test_ai_service_responses_are_read_with_their_subsets():
    assert response_usage(RESPONSES) == {'model': 'gpt-4.1-2025-04-14', 'tokens_in': 28567, 'tokens_out': 2699,
                                         'cached_tokens': None, 'reasoning_tokens': None, 'cache_write_tokens': None}
    cached = {**RESPONSES, 'usage': {**RESPONSES['usage'], 'input_tokens_details': {'cached_tokens': 1000}, 'output_tokens_details': {'reasoning_tokens': 200}}}
    assert (response_usage(cached)['cached_tokens'], response_usage(cached)['reasoning_tokens']) == (1000, 200)
    chat = {'object': 'chat.completion', 'model': 'gpt-4o-mini-2024-07-18',
            'usage': {'prompt_tokens': 900, 'completion_tokens': 100, 'prompt_tokens_details': {'cached_tokens': 300},
                      'completion_tokens_details': {'reasoning_tokens': 40}}}
    assert response_usage(chat) == {'model': 'gpt-4o-mini-2024-07-18', 'tokens_in': 900, 'tokens_out': 100, 'cached_tokens': 300,
                                    'reasoning_tokens': 40, 'cache_write_tokens': None}
    embeddings = {'object': 'list', 'model': 'text-embedding-3-small', 'usage': {'prompt_tokens': 12, 'total_tokens': 12}}
    assert (response_usage(embeddings)['tokens_in'], response_usage(embeddings)['tokens_out']) == (12, 0)
    # Anthropic counts cache reads and writes apart from input_tokens; all three are input.
    anthropic = {'type': 'message', 'model': 'claude-sonnet-4-5-20250929',
                 'usage': {'input_tokens': 100, 'cache_read_input_tokens': 400, 'cache_creation_input_tokens': 50, 'output_tokens': 20}}
    assert response_usage(anthropic) == {'model': 'claude-sonnet-4-5-20250929', 'tokens_in': 550, 'tokens_out': 20,
                                         'cached_tokens': 400, 'reasoning_tokens': None, 'cache_write_tokens': 50}
    # Gemini reports thinking apart from the answer; both are billed as output.
    gemini = {'modelVersion': 'gemini-2.5-flash', 'usageMetadata': {'promptTokenCount': 100, 'candidatesTokenCount': 30,
                                                                     'thoughtsTokenCount': 10, 'cachedContentTokenCount': 20}}
    assert response_usage(gemini) == {'model': 'gemini-2.5-flash', 'tokens_in': 100, 'tokens_out': 40, 'cached_tokens': 20,
                                      'reasoning_tokens': 10, 'cache_write_tokens': None}
    assert response_usage({'model': 'llama3.2', 'prompt_eval_count': 26, 'eval_count': 290})['tokens_out'] == 290


def test_other_json_is_not_an_ai_response():
    for body in ({'usage': 'free tier'}, {'data': [1, 2]}, {'usage': {'prompt_tokens': -1}}, {'usage': {'prompt_tokens': 1.5}},
                 {'usage': {'prompt_tokens': 10, 'prompt_tokens_details': {'cached_tokens': 11}}},
                 {'usage': {'input_tokens': 10 ** 12, 'output_tokens': 1}}, None, [], 'text'):
        assert response_usage(body) is None


def test_http_request_calls_are_counted_by_host_and_model():
    calls = execution_calls(_execution(
        _http('Read source document', 'https://api.openai.com/v1/responses', RESPONSES, RESPONSES),
        _http('Claude', '=https://api.anthropic.com/v1/messages', {'type': 'message', 'model': 'claude-x', 'usage': {'input_tokens': 5, 'output_tokens': 7}}),
        _http('Built URL', '={{ $json.endpoint }}', RESPONSES),
        _http('Local', 'http://ollama:11434/api/chat', {'model': 'llama3.2', 'prompt_eval_count': 26, 'eval_count': 290}),
        _http('Azure', 'https://team.openai.azure.com/openai/deployments/x/chat/completions', {'model': 'gpt-4o', 'usage': {'prompt_tokens': 1, 'completion_tokens': 2}}),
        # With "Include Response Headers and Status", the answer sits in body.
        _http('Full response', 'https://api.groq.com/openai/v1/chat/completions',
              {'statusCode': 200, 'headers': {}, 'body': {'model': 'llama-3.3-70b-versatile', 'usage': {'prompt_tokens': 3, 'completion_tokens': 4}}}),
        _http('Plain API', 'https://api.openai.com/v1/files', {'id': 'file-1', 'object': 'file', 'bytes': 120}),
        ({'name': 'Parse source document', 'type': 'n8n-nodes-base.code', 'parameters': {}}, [{'data': {'main': [[{'json': RESPONSES}]]}}]),
    ))
    by_node = {call['node']: call for call in calls}
    # Two answers in one run are one call of their model; a Code node passing an answer along is not a call.
    assert set(by_node) == {'Read source document', 'Claude', 'Built URL', 'Local', 'Azure', 'Full response'}
    read = by_node['Read source document']
    assert (read['provider'], read['model'], read['tokens_in'], read['tokens_out'], read['source']) == ('openai', 'gpt-4.1-2025-04-14', 57134, 5398, 'response')
    assert {name: call['provider'] for name, call in by_node.items()} == {
        'Read source document': 'openai', 'Claude': 'anthropic', 'Built URL': 'openai-compatible', 'Local': 'ollama',
        'Azure': 'azure', 'Full response': 'groq'}
    assert 'private answer' not in str(calls)


def test_snapshot_names_are_priced_as_their_model_only_where_it_is_the_same_model():
    catalog = {'openai/gpt-4.1': {'input_per_million': '2', 'output_per_million': '8', 'source': 'openrouter'},
               'openai/gpt-4o': {'input_per_million': '2.5', 'output_per_million': '10', 'source': 'openrouter'}}
    priced = calculate_cost('gpt-4.1-2025-04-14', 28567, 2699, catalog, provider='openai')
    assert Decimal(priced['cost_usd']) == Decimal('0.078726') and priced['priced_as'] == 'openai/gpt-4.1'
    assert calculate_cost('openai/gpt-4.1-2025-04-14', 1, 1, catalog, provider='openrouter')['priced_as'] == 'openai/gpt-4.1'
    # Groq's own price, an unlisted snapshot and a differently priced snapshot are never borrowed.
    assert calculate_cost('gpt-4.1-2025-04-14', 1, 1, catalog, provider='groq')['cost_usd'] is None
    assert calculate_cost('gpt-4o-2024-05-13', 1, 1, catalog, provider='openai')['cost_usd'] is None
    assert 'priced_as' not in calculate_cost('openai/gpt-4.1', 1, 1, catalog, provider='openrouter')


def _listing(*items, cursor=None):
    return httpx.Response(200, json={'data': [{'id': item['id'], 'status': item['status'], 'startedAt': item['startedAt'],
                                               'workflowId': item['workflowData']['id']} for item in items], 'nextCursor': cursor})


async def _scan(fleet, instance, **extra):
    response = await fleet.client.post('/api/otel/runs/scan', headers=fleet.headers, json={'instance_id': instance, **extra})
    assert response.status_code == 200, response.text
    return response.json()


async def test_a_run_gets_its_ai_cost_straight_from_n8n(fleet):
    instance = await add(fleet)
    read = _execution(_http('Read source document', 'https://api.openai.com/v1/responses', RESPONSES), identifier='2027')
    running = {**read, 'id': '2029', 'status': 'running'}
    gone = {**read, 'id': '2026'}
    fleet.replies[('alpha.example', 'GET', '/api/v1/executions')] = _listing(running, read, gone, cursor='older')
    fleet.replies[('alpha.example', 'GET', '/api/v1/executions/2027')] = httpx.Response(200, json=read)
    fleet.replies[('alpha.example', 'GET', '/api/v1/executions/2026')] = httpx.Response(404, json={'message': 'Not Found'})
    first = await _scan(fleet, instance)
    assert (first['read'], first['unreadable'], first['running'], first['known'], first['error']) == (1, 1, 1, 0, None)
    assert first['next_cursor'] == 'older' and first['complete'] is False
    assert [call.url.path for call in fleet.calls] == ['/api/v1/executions', '/api/v1/executions/2027', '/api/v1/executions/2026']
    assert fleet.calls[1].url.params['includeData'] == 'true'
    # Recorded runs are not read again; the running one waits until it has finished.
    calls = len(fleet.calls)
    again = await _scan(fleet, instance)
    assert (again['read'], again['known'], again['running']) == (0, 2, 1) and len(fleet.calls) == calls + 1
    listed = (await fleet.client.get('/api/otel/runs', params={'instance_id': instance})).json()
    assert [row['execution_id'] for row in listed['data']] == ['2027', '2026'] and listed['next_cursor'] is None
    run = listed['data'][0]
    # 28,567 input and 2,699 output tokens at gpt-4.1's bundled $2 / $8 per million.
    assert run['cost_usd'] == '0.078726' and run['known_cost_usd'] == '0.078726' and run['unpriced_calls'] == 0
    assert run['workflow_name'] == 'Source Faithful PDF to DOCX' and run['status'] == 'success' and run['read_state'] == 'read'
    assert run['calls'][0]['node'] == 'Read source document' and run['calls'][0]['pricing']['pricing_source'] == 'bundled'
    assert listed['data'][1]['read_state'] == 'unreadable' and listed['data'][1]['ai_calls'] == 0
    assert 'private answer' not in str(listed)
    summary = (await fleet.client.get('/api/otel/runs/summary', params={'instance_id': instance})).json()
    assert (summary['runs'], summary['ai_runs'], summary['ai_calls'], summary['unreadable_runs']) == (2, 1, 1, 1)
    assert summary['cost_usd'] == '0.078726' and summary['by_workflow'][0]['workflow_name'] == 'Source Faithful PDF to DOCX'
    stored = await fleet.app.state.db.fetchone('SELECT calls_json FROM run_costs WHERE execution_id=?', ('2027',))
    assert 'private answer' not in stored['calls_json'] and 'consumers' not in stored['calls_json']


async def test_a_failed_read_stops_the_check_and_is_tried_again(fleet):
    instance = await add(fleet)
    newer = _execution(_http('Ask', 'https://api.openai.com/v1/responses', RESPONSES), identifier='5')
    older = {**newer, 'id': '4'}
    fleet.replies[('alpha.example', 'GET', '/api/v1/executions')] = _listing(newer, older)
    fleet.replies[('alpha.example', 'GET', '/api/v1/executions/5')] = httpx.Response(200, json=newer)
    fleet.replies[('alpha.example', 'GET', '/api/v1/executions/4')] = httpx.Response(500, json={'message': 'boom'})
    stopped = await _scan(fleet, instance)
    assert stopped['read'] == 1 and stopped['error']['code'] == 'upstream_error' and stopped['complete'] is False
    assert 'boom' not in str(stopped)
    fleet.replies[('alpha.example', 'GET', '/api/v1/executions/4')] = httpx.Response(200, json=older)
    resumed = await _scan(fleet, instance)
    assert (resumed['read'], resumed['known'], resumed['complete']) == (1, 1, True)


async def test_runs_past_retention_are_not_read(fleet):
    instance = await add(fleet)
    old = _execution(_http('Ask', 'https://api.openai.com/v1/responses', RESPONSES), identifier='9', started=_ago(days=30))
    fleet.replies[('alpha.example', 'GET', '/api/v1/executions')] = _listing(old, cursor='older')
    result = await _scan(fleet, instance)
    assert result['read'] == 0 and result['next_cursor'] is None and result['complete'] is True
    assert [call.url.path for call in fleet.calls] == ['/api/v1/executions']


async def test_a_new_price_prices_recorded_calls_and_retention_and_deletion_clear_them(fleet):
    instance = await add(fleet)
    house = _execution(_http('Ask', 'https://api.openai.com/v1/chat/completions',
                             {'model': 'house-model', 'usage': {'prompt_tokens': 2000, 'completion_tokens': 1000}}), identifier='7')
    fleet.replies[('alpha.example', 'GET', '/api/v1/executions')] = _listing(house)
    fleet.replies[('alpha.example', 'GET', '/api/v1/executions/7')] = httpx.Response(200, json=house)
    await _scan(fleet, instance)
    before = (await fleet.client.get('/api/otel/runs')).json()['data'][0]
    assert before['cost_usd'] is None and before['unpriced_calls'] == 1 and before['known_cost_usd'] == '0'
    saved = await fleet.client.put('/api/otel/prices/house-model', headers=fleet.headers, json={'input_per_million': '3', 'output_per_million': '6'})
    assert saved.status_code == 200 and saved.json()['repriced'] == 1
    after = (await fleet.client.get('/api/otel/runs')).json()['data'][0]
    assert after['cost_usd'] == '0.012' and after['calls'][0]['pricing']['pricing_source'] == 'override'
    # Retention removes runs that started before the period; deleting the instance removes the rest.
    from backend.modules.observability.router import cleanup
    await fleet.app.state.db.execute("UPDATE run_costs SET started_at='2020-01-01T00:00:00.000Z'")
    await cleanup(fleet.app.state.db)
    assert await fleet.app.state.db.fetchall('SELECT key FROM run_costs') == []
    await _scan(fleet, instance)
    assert (await fleet.client.delete(f'/api/n8n_proxy/instances/{instance}', headers=fleet.headers)).status_code == 200
    assert await fleet.app.state.db.fetchall('SELECT key FROM run_costs') == []


async def test_run_cost_routes_need_a_session_and_csrf(fleet):
    instance = await add(fleet)
    assert (await fleet.client.post('/api/otel/runs/scan', json={'instance_id': instance})).status_code == 403
    assert (await fleet.client.get('/api/otel/runs', params={'workflow_id': 'wf'})).status_code == 422
    assert (await fleet.client.post('/api/otel/runs/scan', headers=fleet.headers, json={'instance_id': 'missing'})).status_code == 404
    fleet.client.cookies.clear()
    assert (await fleet.client.get('/api/otel/runs')).status_code == 401
    assert (await fleet.client.get('/api/otel/runs/summary')).status_code == 401


async def test_the_trace_panel_counts_http_request_calls_too(fleet):
    instance, token = await issue(fleet)
    now = time.time_ns()
    attrs = lambda *pairs: [{'key': key, 'value': {'stringValue': value}} for key, value in pairs]
    spans = [{'traceId': 'b' * 32, 'spanId': '1' * 16, 'name': 'workflow.execute', 'startTimeUnixNano': str(now - 9000), 'endTimeUnixNano': str(now),
              'status': {'code': 1}, 'attributes': attrs(('n8n.workflow.id', 'wf-pdf'), ('n8n.execution.id', '2027'))},
             {'traceId': 'b' * 32, 'spanId': '2' * 16, 'parentSpanId': '1' * 16, 'name': 'node', 'startTimeUnixNano': str(now - 5000),
              'endTimeUnixNano': str(now), 'status': {'code': 1}, 'attributes': attrs(('n8n.node.name', 'Read source document'))}]
    assert (await send(fleet, token['token'], {'resourceSpans': [{'resource': {'attributes': []}, 'scopeSpans': [{'spans': spans}]}]})).status_code == 200
    fleet.replies[('alpha.example', 'GET', '/api/v1/executions/2027')] = httpx.Response(200, json=_execution(
        _http('Read source document', 'https://api.openai.com/v1/responses', RESPONSES)))
    read = await fleet.client.post('/api/otel/traces/' + 'b' * 32 + '/usage', headers=fleet.headers, params={'instance_id': instance})
    assert read.status_code == 200 and read.json()['updated'] == 1
    detail = (await fleet.client.get('/api/otel/traces/' + 'b' * 32, params={'instance_id': instance})).json()
    step = next(span for span in detail['data'] if span['node_name'] == 'Read source document')
    assert (step['tokens_in'], step['tokens_out'], step['model']) == (28567, 2699, 'gpt-4.1-2025-04-14')
    assert detail['cost_usd'] == '0.078726' and step['pricing']['usage_source'] == 'AI service response'


async def test_a_version_7_database_gains_the_run_costs_table(tmp_path):
    from backend.database import SCHEMA_VERSION, Database

    path = tmp_path / 'seven.db'
    database = Database(path)
    await database.open()
    await database.close()
    with sqlite3.connect(path) as connection:
        connection.execute('DROP TABLE run_costs')
        connection.execute('PRAGMA user_version=7')
    for _ in range(2):
        await database.open()
        try:
            assert (await database.fetchone('PRAGMA user_version'))['user_version'] == SCHEMA_VERSION == 8
            indexes = {row['name'] for row in await database.fetchall("PRAGMA index_list('run_costs')")}
            assert {'idx_run_costs_instance', 'idx_run_costs_started'} <= indexes
        finally:
            await database.close()
