import json

import httpx

from test_fleet import fleet, add


async def configure(state, **overrides):
    config = {'kind': 'ollama', 'base_url': 'https://local-model.example/v1', 'model': 'local-model:exact', **overrides}
    result = await state.client.put('/api/assistant/config/provider', headers=state.headers, json={'id': 'local', 'config': config})
    assert result.status_code == 200, result.text


def answer(content='Hello', calls=None):
    return httpx.Response(200, json={'choices': [{'finish_reason': 'tool_calls' if calls else 'stop', 'message': {
        'content': content, **({'tool_calls': calls} if calls else {})}}], 'usage': {'total_tokens': 10}})


def call(name, arguments):
    return {'id': 'call1', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments)}}


async def test_keyless_local_chat_persists_encrypted_exact_model(fleet):
    await configure(fleet)
    fleet.replies[('local-model.example', 'POST', '/v1/chat/completions')] = answer()
    result = await fleet.client.post('/api/assistant/chat', headers=fleet.headers, json={'provider_id': 'local', 'message': 'private user message'})
    assert result.status_code == 200, result.text
    assert result.json()['content'] == 'Hello'
    outbound = fleet.calls[-1]
    assert json.loads(outbound.content)['model'] == 'local-model:exact'
    assert 'authorization' not in outbound.headers and 'cookie' not in outbound.headers
    row = await fleet.app.state.db.fetchone("SELECT value_json FROM documents WHERE key LIKE 'assistant:chat:%'")
    assert 'private user message' not in row['value_json']
    history = await fleet.client.get('/api/assistant/chats/' + result.json()['id'])
    assert history.status_code == 200
    assert history.json()['data'][0]['content'] == 'private user message'


async def test_model_mutation_returns_proposal_without_remote_write(fleet):
    instance = await add(fleet)
    await configure(fleet)
    fleet.replies[('local-model.example', 'POST', '/v1/chat/completions')] = answer('', [call('activate_workflow', {'instance_id': instance, 'workflow_id': 'same'})])
    result = await fleet.client.post('/api/assistant/chat', headers=fleet.headers, json={'provider_id': 'local', 'message': 'activate'})
    assert result.status_code == 200, result.text
    assert len(result.json()['pending_actions']) == 1
    assert not any(c.method == 'POST' and c.url.host == 'alpha.example' for c in fleet.calls)


async def test_read_tool_followup_never_sends_execution_payload(fleet):
    instance = await add(fleet)
    await configure(fleet)
    fleet.replies[('alpha.example', 'GET', '/api/v1/executions/same')] = httpx.Response(200, json={
        'id': 'same', 'status': 'success', 'data': {'secret': 'execution-sensitive'}})
    rounds = []
    async def provider(request):
        rounds.append(json.loads(request.content))
        if len(rounds) == 1:
            return answer('', [call('get_execution', {'instance_id': instance, 'execution_id': 'same'})])
        return answer('The execution succeeded.')
    fleet.replies[('local-model.example', 'POST', '/v1/chat/completions')] = provider
    result = await fleet.client.post('/api/assistant/chat', headers=fleet.headers, json={'provider_id': 'local', 'message': 'status?'})
    assert result.status_code == 200, result.text
    assert len(rounds) == 2 and 'execution-sensitive' not in json.dumps(rounds)
    assert result.json()['content'] == 'The execution succeeded.'


async def test_viewer_cannot_change_provider_or_get_mutation_tools(fleet):
    await configure(fleet)
    await fleet.app.state.db.execute("UPDATE users SET role='viewer' WHERE username='owner'")
    configs = await fleet.client.get('/api/assistant/config/provider')
    assert configs.status_code == 200 and 'base_url' not in configs.text
    denied = await fleet.client.put('/api/assistant/config/provider', headers=fleet.headers, json={'id': 'local', 'config': {}})
    assert denied.status_code == 403
    tools = (await fleet.client.get('/api/assistant/tools')).json()['data']
    assert {t['function']['name'] for t in tools} == {'list_workflows', 'list_errors', 'get_execution', 'get_workflow_context', 'get_error_context'}


async def test_disabled_tool_model_rejects_tool_calls(fleet):
    await configure(fleet, tools_enabled=False)
    fleet.replies[('local-model.example', 'POST', '/v1/chat/completions')] = answer('', [call('activate_workflow', {})])
    result = await fleet.client.post('/api/assistant/chat', headers=fleet.headers, json={'provider_id': 'local', 'message': 'hi'})
    assert result.status_code == 502
    assert await fleet.app.state.db.fetchall('SELECT * FROM approval_actions') == []


async def test_provider_validation_names_field_without_echoing_key(fleet):
    config = {'kind':'custom','base_url':'https://api.b.ai/v1','model':'hy3',
              'api_key_ref':'sk-private-test-value','tools_enabled':False}
    result = await fleet.client.put('/api/assistant/config/provider',headers=fleet.headers,
                                   json={'id':'main-config','config':config})
    assert result.status_code == 422
    assert 'Key reference' in result.json()['detail']
    assert 'sk-private-test-value' not in result.text
    config['api_key_ref'] = '$B_AI_API_KEY'
    result = await fleet.client.put('/api/assistant/config/provider',headers=fleet.headers,
                                   json={'id':'main-config','config':config})
    assert result.status_code == 200


async def test_tool_calling_check_supported_unsupported_and_rate_limited(fleet):
    await configure(fleet, tools_enabled=False)
    provider = ('local-model.example', 'POST', '/v1/chat/completions')
    fleet.replies[provider] = answer('', [call('get_time', {'timezone': 'UTC'})])
    response = await fleet.client.post('/api/assistant/providers/local/tool-test', headers=fleet.headers)
    assert response.status_code == 200, response.text
    assert response.json()['tool_calling']['status'] == 'supported' and response.json()['detail']
    sent = json.loads(fleet.calls[-1].content)
    assert [tool['function']['name'] for tool in sent['tools']] == ['get_time']
    assert [message['role'] for message in sent['messages']] == ['user'] and 'workflow' not in json.dumps(sent).lower()
    listed = (await fleet.client.get('/api/assistant/config/provider')).json()['data'][0]['tool_calling']
    assert listed['status'] == 'supported' and listed['checked_at']
    before = len(fleet.calls)
    fleet.replies[provider] = httpx.Response(429, headers={'retry-after': '12'})
    limited = await fleet.client.post('/api/assistant/providers/local/tool-test', headers=fleet.headers)
    assert limited.status_code == 429 and limited.json()['code'] == 'provider_rate_limited' and limited.json()['retry_after_seconds'] == 12
    assert len(fleet.calls) == before + 1
    assert (await fleet.client.get('/api/assistant/config/provider')).json()['data'][0]['tool_calling'] == listed
    fleet.replies[provider] = answer('It is noon.')
    unsupported = await fleet.client.post('/api/assistant/providers/local/tool-test', headers=fleet.headers)
    assert unsupported.status_code == 200 and unsupported.json()['tool_calling']['status'] == 'unsupported'
    assert 'without calling' in unsupported.json()['detail']
    fleet.replies[provider] = answer('', [call('get_time', {'zone': 'UTC'})])
    assert (await fleet.client.post('/api/assistant/providers/local/tool-test', headers=fleet.headers)).json()['tool_calling']['status'] == 'unsupported'
    assert not await fleet.app.state.db.fetchone("SELECT key FROM documents WHERE key LIKE 'assistant:chat:%'")
    assert not await fleet.app.state.db.fetchone('SELECT id FROM approval_actions')
    await fleet.app.state.db.execute("UPDATE users SET role='viewer' WHERE username='owner'")
    viewer = (await fleet.client.get('/api/assistant/config/provider')).json()['data'][0]
    assert viewer['tool_calling']['status'] == 'unsupported' and 'base_url' not in viewer
    assert (await fleet.client.post('/api/assistant/providers/local/tool-test', headers=fleet.headers)).status_code == 403


async def test_tool_calling_result_resets_when_config_or_key_changes(fleet, monkeypatch):
    monkeypatch.setenv('INFERENCE_TOOL_CHECK_KEY', 'first-key')
    await configure(fleet, api_key_ref='$INFERENCE_TOOL_CHECK_KEY')
    fleet.replies[('local-model.example', 'POST', '/v1/chat/completions')] = answer('', [call('get_time', {'timezone': 'UTC'})])
    assert (await fleet.client.post('/api/assistant/providers/local/tool-test', headers=fleet.headers)).json()['tool_calling']['status'] == 'supported'
    status = lambda: fleet.client.get('/api/assistant/config/provider')
    assert (await status()).json()['data'][0]['tool_calling']['status'] == 'supported'
    monkeypatch.setenv('INFERENCE_TOOL_CHECK_KEY', 'rotated-key')
    assert (await status()).json()['data'][0]['tool_calling'] == {'status': 'untested', 'checked_at': None}
    monkeypatch.setenv('INFERENCE_TOOL_CHECK_KEY', 'first-key')
    await configure(fleet, api_key_ref='$INFERENCE_TOOL_CHECK_KEY', model='other-model:exact')
    assert (await status()).json()['data'][0]['tool_calling'] == {'status': 'untested', 'checked_at': None}
    assert 'first-key' not in (await status()).text


async def test_standard_chat_surfaces_provider_rate_limit_without_retry(fleet):
    await configure(fleet)
    fleet.replies[('local-model.example', 'POST', '/v1/chat/completions')] = httpx.Response(429)
    result = await fleet.client.post('/api/assistant/chat', headers=fleet.headers, json={'provider_id': 'local', 'message': 'hi'})
    assert result.status_code == 429 and result.json()['code'] == 'provider_rate_limited'
    assert sum(call.url.host == 'local-model.example' for call in fleet.calls) == 1


async def test_connection_test_never_sends_tools_or_saves_chat(fleet):
    await configure(fleet, tools_enabled=True)
    fleet.replies[('local-model.example', 'POST', '/v1/chat/completions')] = answer('Connected.')
    response = await fleet.client.post('/api/assistant/providers/local/test',headers=fleet.headers)
    assert response.status_code == 200
    assert response.json()['connected'] is True
    assert 'tools' not in json.loads(fleet.calls[-1].content)
    assert not await fleet.app.state.db.fetchone("SELECT key FROM documents WHERE key LIKE 'assistant:chat:%'")
    fleet.replies[('local-model.example', 'POST', '/v1/chat/completions')] = answer('',[call('activate_workflow',{})])
    response = await fleet.client.post('/api/assistant/providers/local/test',headers=fleet.headers)
    assert response.status_code == 502
    assert not await fleet.app.state.db.fetchone('SELECT id FROM approval_actions')
