"""AI token usage read from n8n executions, provider-aware prices and re-pricing."""
from decimal import Decimal
import time

import httpx

from backend.modules.observability.pricing import calculate_cost, find_price
from backend.modules.observability.usage import execution_usage
from test_fleet import fleet  # noqa: F401  (fixture)
from test_observability import issue, send


def _lm_run(prompt, completion, estimate=False):
    key = 'tokenUsageEstimate' if estimate else 'tokenUsage'
    return {'data': {'ai_languageModel': [[{'json': {'response': {'generations': []}, key: {
        'promptTokens': prompt, 'completionTokens': completion, 'totalTokens': prompt + completion}}}]]}}


EXECUTION = {
    'id': 'ex-ai', 'status': 'success',
    'workflowData': {
        'nodes': [
            {'name': 'AI Agent', 'type': '@n8n/n8n-nodes-langchain.agent', 'parameters': {}},
            {'name': 'OpenAI Chat Model', 'type': '@n8n/n8n-nodes-langchain.lmChatOpenAi',
             'parameters': {'model': {'__rl': True, 'value': 'gpt-4o-mini', 'mode': 'list'}}},
            {'name': 'Groq Chat Model', 'type': '@n8n/n8n-nodes-langchain.lmChatGroq', 'parameters': {'model': 'openai/gpt-oss-20b'}},
            {'name': 'Compatible', 'type': '@n8n/n8n-nodes-langchain.lmChatOpenAi',
             'parameters': {'model': 'some-model', 'options': {'baseURL': 'https://router.example/v1'}}},
            {'name': 'Expression model', 'type': '@n8n/n8n-nodes-langchain.lmChatOpenAi', 'parameters': {'model': '={{ $json.model }}'}},
            {'name': 'Message a model', 'type': '@n8n/n8n-nodes-langchain.openAi', 'parameters': {'modelId': {'value': 'gpt-4.1-mini'}}},
            {'name': 'Set', 'type': 'n8n-nodes-base.set', 'parameters': {}},
        ],
        'connections': {'OpenAI Chat Model': {'ai_languageModel': [[{'node': 'AI Agent', 'type': 'ai_languageModel', 'index': 0}]]}},
    },
    'data': {'resultData': {'runData': {
        'AI Agent': [{'data': {'main': [[{'json': {'output': 'private answer'}}]]}}],
        'OpenAI Chat Model': [_lm_run(800, 100), _lm_run(200, 50)],
        'Groq Chat Model': [_lm_run(1000, 200, estimate=True)],
        'Compatible': [_lm_run(10, 5)],
        'Expression model': [_lm_run(10, 5)],
        'Message a model': [{'metadata': {'tokenUsage': {'inputTokens': 30, 'outputTokens': 12}}}],
        'Set': [{'data': {'main': [[{'json': {'tokenUsage': {'promptTokens': 1, 'completionTokens': 1}}}]]}}],
        'Broken': 'not a list',
    }}},
}


def test_usage_is_read_the_way_n8n_reads_it():
    usage = execution_usage(EXECUTION)
    assert set(usage) == {'OpenAI Chat Model', 'Groq Chat Model', 'Compatible', 'Expression model', 'Message a model'}
    chat = usage['OpenAI Chat Model']
    assert chat['model'] == 'gpt-4o-mini' and chat['provider'] == 'openai' and chat['consumers'] == ['AI Agent']
    assert [(run['tokens_in'], run['tokens_out']) for run in chat['runs']] == [(800, 100), (200, 50)]
    assert usage['Groq Chat Model']['runs'][0]['estimated'] is True and usage['Groq Chat Model']['provider'] == 'groq'
    # An OpenAI node pointed at another service, an expression model and a vendor node.
    assert usage['Compatible']['provider'] == 'openai-compatible'
    assert usage['Expression model']['model'] is None
    assert usage['Message a model'] == {'provider': 'openai', 'model': 'gpt-4.1-mini',
                                        'runs': [{'tokens_in': 30, 'tokens_out': 12, 'estimated': False}], 'consumers': []}
    assert 'private answer' not in str(usage)


def test_usage_ignores_malformed_and_absurd_counts():
    broken = {'workflowData': {'nodes': [{'name': 'M', 'type': '@n8n/n8n-nodes-langchain.lmChatOpenAi', 'parameters': {}}]},
              'data': {'resultData': {'runData': {'M': [_lm_run(-1, 5), _lm_run(1.5, 2), _lm_run(10**12, 1), {'data': 'x'}]}}}}
    assert execution_usage(broken) == {}
    assert execution_usage(None) == {} and execution_usage({'data': []}) == {}


CATALOG = {'openai/gpt-4o-mini': {'input_per_million': '0.15', 'output_per_million': '0.6', 'source': 'openrouter'},
           'openai/gpt-oss-20b': {'input_per_million': '0.018', 'output_per_million': '0.09', 'source': 'openrouter'}}


def test_a_host_never_borrows_another_hosts_catalog_price():
    # Through OpenAI (or OpenRouter), a model's catalog price applies; Groq charges its own.
    assert find_price(CATALOG, 'gpt-4o-mini', 'openai')['input_per_million'] == '0.15'
    assert find_price(CATALOG, 'openai/gpt-4o-mini', 'openrouter')['input_per_million'] == '0.15'
    assert find_price(CATALOG, 'openai/gpt-oss-20b', None) is not None
    assert find_price(CATALOG, 'openai/gpt-oss-20b', 'groq') is None
    assert calculate_cost('openai/gpt-oss-20b', 1_000_000, 200_000, CATALOG, provider='groq')['cost_usd'] is None
    # An administrator's price applies whatever the host.
    prices = {**CATALOG, 'openai/gpt-oss-20b': {'input_per_million': '0.075', 'output_per_million': '0.30', 'source': 'override'}}
    assert Decimal(calculate_cost('openai/gpt-oss-20b', 1_000_000, 200_000, prices, provider='groq')['cost_usd']) == Decimal('0.135')


def _span(trace, span_id, name, parent=None, attrs=()):
    now = time.time_ns()
    span = {'traceId': trace, 'spanId': span_id, 'name': name, 'startTimeUnixNano': str(now - 5000), 'endTimeUnixNano': str(now),
            'status': {'code': 1}, 'attributes': [{'key': key, 'value': {'stringValue': value}} for key, value in attrs]}
    if parent:
        span['parentSpanId'] = parent
    return span


async def test_a_run_gets_its_ai_cost_from_its_n8n_execution(fleet):
    instance, token = await issue(fleet)
    await fleet.client.put('/api/otel/prices/gpt-4o-mini', headers=fleet.headers, json={'input_per_million': '1', 'output_per_million': '2'})
    run = [('n8n.workflow.id', 'wf-ai'), ('n8n.execution.id', 'ex-ai')]
    body = {'resourceSpans': [{'resource': {'attributes': []}, 'scopeSpans': [{'spans': [
        _span('7' * 32, '1' * 16, 'workflow.execute', attrs=run),
        _span('7' * 32, '2' * 16, 'node AI Agent', parent='1' * 16, attrs=[('n8n.node.name', 'AI Agent')])]}]}]}
    assert (await send(fleet, token['token'], body)).status_code == 200
    fleet.replies[('alpha.example', 'GET', '/api/v1/executions/ex-ai')] = httpx.Response(200, json=EXECUTION)
    first = await fleet.client.post('/api/otel/traces/' + '7' * 32 + '/usage', headers=fleet.headers, params={'instance_id': instance})
    assert first.status_code == 200, first.text
    assert first.json()['updated'] == 1
    detail = (await fleet.client.get('/api/otel/traces/' + '7' * 32, params={'instance_id': instance})).json()
    agent = next(span for span in detail['data'] if span['node_name'] == 'AI Agent')
    # 1000 in and 150 out at $1 and $2 per million, on the step the model sub-node feeds.
    assert (agent['tokens_in'], agent['tokens_out'], agent['model']) == (1000, 150, 'gpt-4o-mini')
    assert Decimal(detail['cost_usd']) == Decimal('0.0013') and detail['ai_spans'] == 1
    assert agent['pricing']['usage_source'] == 'n8n execution' and agent['pricing']['provider'] == 'openai'
    assert 'private answer' not in str(detail)
    # A finished run is read once.
    calls = len(fleet.calls)
    again = await fleet.client.post('/api/otel/traces/' + '7' * 32 + '/usage', headers=fleet.headers, params={'instance_id': instance})
    assert again.status_code == 200 and again.json()['read'] is False and len(fleet.calls) == calls


async def test_a_run_whose_execution_n8n_did_not_keep_is_asked_for_once(fleet):
    instance, token = await issue(fleet)
    run = [('n8n.workflow.id', 'wf-ai'), ('n8n.execution.id', 'ex-gone')]
    body = {'resourceSpans': [{'resource': {'attributes': []}, 'scopeSpans': [{'spans': [_span('9' * 32, '4' * 16, 'workflow.execute', attrs=run)]}]}]}
    assert (await send(fleet, token['token'], body)).status_code == 200
    fleet.replies[('alpha.example', 'GET', '/api/v1/executions/ex-gone')] = httpx.Response(404, json={'message': 'Not Found'})
    first = await fleet.client.post('/api/otel/traces/' + '9' * 32 + '/usage', headers=fleet.headers, params={'instance_id': instance})
    assert first.status_code == 200, first.text
    assert first.json()['missing'] is True and first.json()['read'] is False
    calls = len(fleet.calls)
    again = await fleet.client.post('/api/otel/traces/' + '9' * 32 + '/usage', headers=fleet.headers, params={'instance_id': instance})
    assert again.status_code == 200 and again.json()['missing'] is True and len(fleet.calls) == calls
    # Any other failure is reported, not recorded, so the next read asks n8n again.
    flaky = [('n8n.workflow.id', 'wf-ai'), ('n8n.execution.id', 'ex-flaky')]
    body = {'resourceSpans': [{'resource': {'attributes': []}, 'scopeSpans': [{'spans': [_span('a' * 32, '5' * 16, 'workflow.execute', attrs=flaky)]}]}]}
    assert (await send(fleet, token['token'], body)).status_code == 200
    fleet.replies[('alpha.example', 'GET', '/api/v1/executions/ex-flaky')] = httpx.Response(500, json={'message': 'Internal error'})
    failed = await fleet.client.post('/api/otel/traces/' + 'a' * 32 + '/usage', headers=fleet.headers, params={'instance_id': instance})
    assert failed.status_code >= 500
    fleet.replies[('alpha.example', 'GET', '/api/v1/executions/ex-flaky')] = httpx.Response(200, json={**EXECUTION, 'id': 'ex-flaky'})
    read = await fleet.client.post('/api/otel/traces/' + 'a' * 32 + '/usage', headers=fleet.headers, params={'instance_id': instance})
    assert read.status_code == 200 and read.json()['read'] is True and read.json()['missing'] is False


async def test_setting_a_price_prices_steps_that_waited_for_one(fleet):
    instance, token = await issue(fleet)
    ints = [{'key': 'gen_ai.usage.input_tokens', 'value': {'intValue': '2000'}}, {'key': 'gen_ai.usage.output_tokens', 'value': {'intValue': '1000'}}]
    span = _span('8' * 32, '3' * 16, 'chat', attrs=[('gen_ai.request.model', 'house-model')])
    span['attributes'] += ints
    assert (await send(fleet, token['token'], {'resourceSpans': [{'resource': {'attributes': []}, 'scopeSpans': [{'spans': [span]}]}]})).status_code == 200
    before = (await fleet.client.get('/api/otel/traces/' + '8' * 32, params={'instance_id': instance})).json()
    assert before['cost_usd'] is None and before['unknown_cost_spans'] == 1
    saved = await fleet.client.put('/api/otel/prices/house-model', headers=fleet.headers, json={'input_per_million': '3', 'output_per_million': '6'})
    assert saved.status_code == 200 and saved.json()['repriced'] == 1
    after = (await fleet.client.get('/api/otel/traces/' + '8' * 32, params={'instance_id': instance})).json()
    assert Decimal(after['cost_usd']) == Decimal('0.012') and after['unknown_cost_spans'] == 0
    # Removing the price leaves what was already priced, and the model falls back to no price.
    assert (await fleet.client.delete('/api/otel/prices/house-model', headers=fleet.headers)).status_code == 200
    assert (await fleet.client.delete('/api/otel/prices/house-model', headers=fleet.headers)).status_code == 404
    listed = {row['model'] for row in (await fleet.client.get('/api/otel/prices')).json()['data']}
    assert 'house-model' not in listed


async def test_the_daily_refresh_can_be_stopped(fleet):
    stopped = await fleet.client.delete('/api/otel/prices/sync', headers=fleet.headers)
    assert stopped.status_code == 200 and stopped.json()['enabled'] is False
    assert (await fleet.client.get('/api/otel/prices')).json()['sync']['enabled'] is False
