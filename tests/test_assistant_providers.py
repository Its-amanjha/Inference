import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from backend.modules.assistant.providers import ProviderConfig, complete


TOOL = {'type': 'function', 'function': {'name': 'list_errors', 'description': 'List', 'parameters': {'type': 'object', 'properties': {}}}}


def request(client):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(http=client, vault=SimpleNamespace(resolve=lambda ref: 'secret-value'), settings=SimpleNamespace(allow_cidrs=('127.0.0.0/8',)))))


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['openai', 'openrouter', 'groq', 'custom', 'ollama'])
async def test_compatible_payload_isolated_headers_and_redaction(kind):
    def handler(req):
        assert str(req.url) == 'https://provider.example/v1/chat/completions'
        assert req.headers['authorization'] == 'Bearer secret-value'
        assert 'cookie' not in req.headers and 'x-private' not in req.headers
        body = json.loads(req.content)
        assert body['model'] == 'vendor/model-20260101:exact'
        assert body['tools'] == [TOOL]
        return httpx.Response(200, json={'choices': [{'finish_reason': 'tool_calls', 'message': {'content': 'secret-value', 'tool_calls': [{'id': 'call1', 'type': 'function', 'function': {'name': 'list_errors', 'arguments': '{"echo":"secret-value"}'}}]}}], 'usage': {'prompt_tokens': 2}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={'x-private': 'hidden'}, cookies={'session': 'hidden'}, auth=('hidden', 'hidden')) as client:
        result = await complete(request(client), ProviderConfig(kind=kind, base_url='https://provider.example/v1', model='vendor/model-20260101:exact', api_key_ref='$KEY'), [{'role': 'user', 'content': 'hello'}], [TOOL])
    assert result['content'] == '[redacted]'
    assert result['tool_calls'][0]['arguments'] == {'echo': '[redacted]'}


@pytest.mark.asyncio
async def test_anthropic_translation_and_local_keyless():
    def handler(req):
        body = json.loads(req.content)
        assert req.headers['x-api-key'] == 'secret-value'
        assert req.headers['anthropic-version'] == '2023-06-01'
        assert body['system'] == 'system'
        assert body['tools'][0]['input_schema'] == TOOL['function']['parameters']
        assert body['messages'][1]['content'][0]['type'] == 'tool_use'
        assert body['messages'][2]['content'][0]['tool_use_id'] == 'call1'
        return httpx.Response(200, json={'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'done'}], 'usage': {'input_tokens': 1}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        messages = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'hello'}, {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'call1', 'type': 'function', 'function': {'name': 'list_errors', 'arguments': '{}'}}]}, {'role': 'tool', 'tool_call_id': 'call1', 'content': '{}'}]
        result = await complete(request(client), ProviderConfig(kind='anthropic', base_url='https://provider.example/v1', model='exact', api_key_ref='$KEY'), messages, [TOOL])
        assert result['content'] == 'done'
    def local(req):
        assert 'authorization' not in req.headers
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': 'local'}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(local)) as client:
        assert (await complete(request(client), ProviderConfig(kind='ollama', base_url='http://127.0.0.1:11434/v1', model='local'), [{'role': 'user', 'content': 'hi'}]))['content'] == 'local'


@pytest.mark.asyncio
@pytest.mark.parametrize('response', [
    {'choices': []},
    {'choices': [{'finish_reason': 'length', 'message': {'content': 'truncated'}}]},
    {'choices': [{'finish_reason': 'tool_calls', 'message': {'tool_calls': [{'id': '1', 'type': 'function', 'function': {'name': 'list_errors', 'arguments': '[]'}}]}}]},
    {'choices': [{'finish_reason': 'tool_calls', 'message': {'tool_calls': [{'id': '1', 'type': 'function', 'function': {'name': 'unknown', 'arguments': '{}'}}]}}]},
])
async def test_malformed_response_fails_closed(response):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=response))) as client:
        with pytest.raises(HTTPException) as error:
            await complete(request(client), ProviderConfig(kind='ollama', base_url='https://provider.example/v1', model='x'), [{'role': 'user', 'content': 'hi'}], [TOOL])
        assert error.value.status_code == 502


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [302, 401, 429, 500])
async def test_errors_redacted_without_retry(status):
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(status, text='secret-value', headers={'location': 'https://other.example'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HTTPException) as error:
            await complete(request(client), ProviderConfig(kind='ollama', base_url='https://provider.example/v1', model='x'), [{'role': 'user', 'content': 'hi'}])
        assert 'secret-value' not in str(error.value)
        assert len(calls) == 1


@pytest.mark.parametrize('url', ['https://user:pass@example.com/v1', 'https://example.com/v1?q=x', 'https://example.com/#fragment'])
def test_bad_endpoint(url):
    with pytest.raises(ValidationError):
        ProviderConfig(kind='custom', base_url=url, model='x')


@pytest.mark.asyncio
async def test_response_bound_and_cancellation(monkeypatch):
    import backend.modules.assistant.providers as providers
    monkeypatch.setattr(providers, 'MAX_RESPONSE_BYTES', 50)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b'X' * 51))) as client:
        with pytest.raises(HTTPException):
            await complete(request(client), ProviderConfig(kind='ollama', base_url='https://provider.example/v1', model='x'), [{'role': 'user', 'content': 'hi'}])


@pytest.mark.asyncio
async def test_timeout_and_disabled_tools(monkeypatch):
    import backend.modules.assistant.providers as providers
    monkeypatch.setattr(providers, 'TOTAL_TIMEOUT', 0.01)
    monkeypatch.setattr(providers, 'LOCAL_TOTAL_TIMEOUT', 0.01)
    cancelled = asyncio.Event()
    async def slow(req):
        try:
            await asyncio.sleep(5)
        finally:
            cancelled.set()
    async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
        with pytest.raises(HTTPException):
            await complete(request(client), ProviderConfig(kind='ollama', base_url='https://provider.example/v1', model='x'), [{'role': 'user', 'content': 'hi'}])
        assert cancelled.is_set()
    def handler(req):
        assert 'tools' not in json.loads(req.content)
        return httpx.Response(200, json={'choices': [{'finish_reason': 'tool_calls', 'message': {'tool_calls': [{'id': '1', 'type': 'function', 'function': {'name': 'list_errors', 'arguments': '{}'}}]}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HTTPException):
            await complete(request(client), ProviderConfig(kind='ollama', base_url='https://provider.example/v1', model='x', tools_enabled=False), [{'role': 'user', 'content': 'hi'}], [TOOL])


@pytest.mark.asyncio
async def test_blocked_ip_and_missing_secret_never_send():
    def handler(req):
        pytest.fail('Configuration must fail before sending')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for config in [ProviderConfig(kind='openai', base_url='https://provider.example/v1', model='x'), ProviderConfig(kind='ollama', base_url='http://169.254.169.254/v1', model='x')]:
            with pytest.raises(HTTPException) as error:
                await complete(request(client), config, [{'role': 'user', 'content': 'hi'}])
            assert error.value.status_code == 400


@pytest.mark.asyncio
async def test_duplicate_argument_keys_and_compressed_responses_rejected():
    bodies = [httpx.Response(200, content=b'{"choices":[{"finish_reason":"tool_calls","message":{"tool_calls":[{"id":"1","type":"function","function":{"name":"list_errors","arguments":"{\\"x\\":1,\\"x\\":2}"}}]}}]}'), httpx.Response(200, headers={'content-encoding': 'unsupported'}, content=b'ignored')]
    for response in bodies:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: response)) as client:
            with pytest.raises(HTTPException):
                await complete(request(client), ProviderConfig(kind='ollama', base_url='https://provider.example/v1', model='x'), [{'role': 'user', 'content': 'hi'}], [TOOL])


def test_plaintext_secret_configuration_rejected():
    with pytest.raises(ValidationError):
        ProviderConfig(kind='openai', base_url='https://provider.example/v1', model='x', api_key_ref='sk-secret')


@pytest.mark.asyncio
@pytest.mark.parametrize('arguments', ['{"limit": 2}', {'limit': 2}])
async def test_tool_arguments_accept_json_string_or_object_and_stop_with_calls(arguments):
    response = {'choices': [{'finish_reason': 'stop', 'message': {'content': None, 'tool_calls': [
        {'id': 'call1', 'type': 'function', 'function': {'name': 'list_errors', 'arguments': arguments}}]}}]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=response))) as client:
        result = await complete(request(client), ProviderConfig(kind='custom', base_url='https://provider.example/v1', model='x'), [{'role': 'user', 'content': 'hi'}], [TOOL])
    assert result['tool_calls'] == [{'id': 'call1', 'name': 'list_errors', 'arguments': {'limit': 2}}]
    assert result['content'] == ''


@pytest.mark.asyncio
async def test_anthropic_tool_use_input_is_normalized():
    body = {'stop_reason': 'tool_use', 'content': [{'type': 'text', 'text': 'Checking.'},
            {'type': 'tool_use', 'id': 'toolu_1', 'name': 'list_errors', 'input': {'limit': 3}}]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=body))) as client:
        result = await complete(request(client), ProviderConfig(kind='anthropic', base_url='https://provider.example/v1', model='x', api_key_ref='$KEY'), [{'role': 'user', 'content': 'hi'}], [TOOL])
    assert result['tool_calls'] == [{'id': 'toolu_1', 'name': 'list_errors', 'arguments': {'limit': 3}}]
    assert result['content'] == 'Checking.'


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,body', [
    ('custom', {'choices': [{'finish_reason': 'length', 'message': {'content': 'cut'}}]}),
    ('custom', {'choices': [{'finish_reason': 'content_filter', 'message': {'content': ''}}]}),
    ('anthropic', {'stop_reason': 'max_tokens', 'content': [{'type': 'text', 'text': 'cut'}]}),
])
async def test_truncated_output_is_provider_incomplete(kind, body):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=body))) as client:
        with pytest.raises(HTTPException) as error:
            await complete(request(client), ProviderConfig(kind=kind, base_url='https://provider.example/v1', model='x', api_key_ref='$KEY'), [{'role': 'user', 'content': 'hi'}], [TOOL])
    assert error.value.status_code == 502 and error.value.detail['code'] == 'provider_incomplete'


@pytest.mark.asyncio
async def test_tool_calls_finish_reason_without_calls_is_invalid():
    body = {'choices': [{'finish_reason': 'tool_calls', 'message': {'content': 'none'}}]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=body))) as client:
        with pytest.raises(HTTPException) as error:
            await complete(request(client), ProviderConfig(kind='custom', base_url='https://provider.example/v1', model='x'), [{'role': 'user', 'content': 'hi'}], [TOOL])
    assert error.value.detail['code'] == 'provider_invalid_response'


@pytest.mark.asyncio
@pytest.mark.parametrize('header,expected', [('42', 42), ('Wed, 21 Oct 2026 07:28:00 GMT', None), ('1.5', None), (None, None)])
async def test_provider_429_is_rate_limited_once_with_numeric_retry_after(header, expected):
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(429, text='secret-value', headers={'retry-after': header} if header else {})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HTTPException) as error:
            await complete(request(client), ProviderConfig(kind='custom', base_url='https://provider.example/v1', model='x', api_key_ref='$KEY'), [{'role': 'user', 'content': 'hi'}], [TOOL])
    assert error.value.status_code == 429
    assert error.value.detail == {'detail': error.value.detail['detail'], 'code': 'provider_rate_limited', 'retry_after_seconds': expected}
    assert 'secret-value' not in str(error.value.detail)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_only_wire_fields_are_forwarded():
    def handler(req):
        body = json.loads(req.content)
        assert body['messages'] == [{'role': 'user', 'content': 'hi'}, {'role': 'assistant', 'content': 'done'}]
        assert 'action_ids' not in req.content.decode()
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': 'ok'}}]})
    messages = [{'role': 'user', 'content': 'hi', 'action_ids': ['private-action']},
                {'role': 'assistant', 'content': 'done', 'action_ids': ['private-action'], 'extra': 'x'}]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert (await complete(request(client), ProviderConfig(kind='ollama', base_url='https://provider.example/v1', model='x'), messages))['content'] == 'ok'


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['openai', 'anthropic'])
async def test_declarations_are_flattened_for_every_kind(kind):
    from backend.modules.assistant.repair_contract import PatchArguments
    raw = PatchArguments.model_json_schema()
    assert '$defs' in raw
    def handler(req):
        body = json.loads(req.content)
        text = json.dumps(body['tools'])
        for marker in ('$ref', '$defs', 'discriminator', '"title"'):
            assert marker not in text
        if kind == 'anthropic':
            return httpx.Response(200, json={'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'ok'}]})
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': 'ok'}}]})
    tool = {'type': 'function', 'function': {'name': 'propose_workflow_patch', 'description': 'Patch.', 'parameters': raw}}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await complete(request(client), ProviderConfig(kind=kind, base_url='https://provider.example/v1', model='x', api_key_ref='$KEY'), [{'role': 'user', 'content': 'hi'}], [tool])
    assert result['content'] == 'ok'


def test_flatten_schema_rejects_remote_or_recursive_references():
    from backend.modules.assistant.providers import flatten_schema
    with pytest.raises(ValueError):
        flatten_schema({'properties': {'a': {'$ref': 'https://example.com/schema.json'}}})
    with pytest.raises(ValueError):
        flatten_schema({'$defs': {'Loop': {'properties': {'next': {'$ref': '#/$defs/Loop'}}}}, 'properties': {'x': {'$ref': '#/$defs/Loop'}}})


@pytest.mark.asyncio
async def test_external_cancellation_propagates():
    async def handler(req):
        raise asyncio.CancelledError
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(asyncio.CancelledError):
            await complete(request(client), ProviderConfig(kind='ollama', base_url='https://provider.example/v1', model='x'), [{'role': 'user', 'content': 'hi'}])


def _answer(seen):
    def handler(req):
        seen.append(req.extensions['timeout'])
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': 'ok'}}]})
    return handler


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,base_url,local', [
    ('ollama', 'http://127.0.0.1:11434/v1', True),
    ('ollama', 'https://provider.example/v1', True),
    ('custom', 'http://localhost:1234/v1', True),
    ('custom', 'http://127.0.0.1:8080/v1', True),
    ('custom', 'http://192.168.1.20:8000/v1', True),
    ('custom', 'https://provider.example/v1', False),
    ('custom', 'https://8.8.8.8/v1', False),
    ('openai', 'https://provider.example/v1', False),
    ('openrouter', 'https://provider.example/v1', False),
    ('groq', 'https://provider.example/v1', False),
    ('anthropic', 'https://provider.example/v1', False),
])
async def test_local_providers_get_the_long_budget_and_hosted_keep_45_60(monkeypatch, kind, base_url, local):
    import backend.modules.assistant.providers as providers
    waits = []
    real_wait_for = asyncio.wait_for
    async def recording_wait_for(awaitable, timeout):
        waits.append(timeout)
        return await real_wait_for(awaitable, timeout)
    monkeypatch.setattr(providers.asyncio, 'wait_for', recording_wait_for)
    seen = []
    handler = _answer(seen)
    if kind == 'anthropic':
        def handler(req):
            seen.append(req.extensions['timeout'])
            return httpx.Response(200, json={'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'ok'}]})
    settings = request(None).app.state.settings
    settings.allow_cidrs = ('0.0.0.0/0', '::/0')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        conn = request(client)
        conn.app.state.settings = settings
        assert providers.is_local(ProviderConfig(kind=kind, base_url=base_url, model='x')) is local
        result = await complete(conn, ProviderConfig(kind=kind, base_url=base_url, model='x', api_key_ref='$KEY'), [{'role': 'user', 'content': 'hi'}])
    assert result['content'] == 'ok'
    read, total = (600.0, 630.0) if local else (45.0, 60.0)
    assert seen == [{'connect': 5.0, 'read': read, 'write': 10.0, 'pool': 5.0}]
    assert waits == [total]


@pytest.mark.parametrize('kind,base_url,local', [
    ('custom', 'http://[::1]:8000/v1', True),
    ('custom', 'http://10.0.0.5:8000/v1', True),
    ('custom', 'http://llm.localhost:8000/v1', True),
    ('custom', 'https://localhost.example.com/v1', False),
    ('openai', 'http://127.0.0.1:8000/v1', False),
])
def test_is_local_uses_kind_and_literal_hosts_only(kind, base_url, local):
    from backend.modules.assistant.providers import is_local
    assert is_local(ProviderConfig(kind=kind, base_url=base_url, model='x')) is local
