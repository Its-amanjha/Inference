import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from backend.modules.assistant import mcp


@pytest.fixture
def setup_client(monkeypatch):
    monkeypatch.setattr(mcp, 'require_current_role', AsyncMock())
    @asynccontextmanager
    async def transaction():
        yield None
    def create(handler):
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                headers={'X-Unwanted': 'private'}, cookies={'session': 'private'},
                                auth=('unwanted', 'password'))
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            http=http, db=SimpleNamespace(transaction=transaction),
            vault=SimpleNamespace(resolve=lambda ref: 'super-secret'))))
        return request, http
    return create


def server(result, seen, sse=False):
    def handle(request):
        seen.append(request)
        assert 'cookie' not in request.headers and 'x-unwanted' not in request.headers
        assert request.headers['authorization'] == 'Bearer super-secret'
        if request.method == 'DELETE':
            return httpx.Response(405)
        message = json.loads(request.content)
        if message['method'] == 'initialize':
            return httpx.Response(200, json={'jsonrpc': '2.0', 'id': message['id'], 'result': {
                'protocolVersion': mcp.PROTOCOL_VERSION, 'capabilities': {'tools': {}},
                'serverInfo': {'name': 'fixture', 'version': '1'}}}, headers={'Mcp-Session-Id': 'session-1'})
        assert request.headers['Mcp-Session-Id'] == 'session-1'
        assert request.headers['Mcp-Protocol-Version'] == mcp.PROTOCOL_VERSION
        if message['method'] == 'notifications/initialized':
            return httpx.Response(202)
        payload = {'jsonrpc': '2.0', 'id': message['id'], 'result': result}
        if sse:
            body = 'id: first\ndata:\n\ndata: ' + json.dumps(payload) + '\n\n'
            return httpx.Response(200, text=body, headers={'Content-Type': 'text/event-stream'})
        return httpx.Response(200, json=payload)
    return handle


@pytest.mark.asyncio
@pytest.mark.parametrize('sse', [False, True])
async def test_handshake_call_isolated_credentials_and_redaction(setup_client, sse):
    seen = []
    request, http = setup_client(server({'content': [{'type': 'text', 'text': 'echo super-secret'}]}, seen, sse))
    async with http:
        result = await mcp.call_tool(request, mcp.MCPConfig(url='https://mcp.example/tools', api_key_ref='$MCP'), 'run', {})
    assert result['content'][0]['text'] == 'echo [redacted]'
    assert [r.method for r in seen] == ['POST', 'POST', 'POST', 'DELETE']


@pytest.mark.asyncio
async def test_discovery_does_not_call_annotated_tools(setup_client):
    seen = []
    tool = {'name': 'danger', 'inputSchema': {'type': 'object'}, 'annotations': {'readOnlyHint': True}}
    request, http = setup_client(server({'tools': [tool]}, seen))
    async with http:
        assert (await mcp.list_tools(request, mcp.MCPConfig(url='https://mcp.example', api_key_ref='$MCP')))[0]['name'] == 'danger'
    assert not any(b'tools/call' in r.content for r in seen)


@pytest.mark.asyncio
async def test_call_timeout_is_uncertain_without_retry(setup_client):
    seen = []
    normal = server({}, seen)
    def handler(request):
        if b'tools/call' in request.content:
            seen.append(request)
            raise httpx.ReadTimeout('super-secret')
        return normal(request)
    request, http = setup_client(handler)
    async with http:
        with pytest.raises(HTTPException) as exc:
            await mcp.call_tool(request, mcp.MCPConfig(url='https://mcp.example', api_key_ref='$MCP'), 'run', {})
    assert exc.value.detail['code'] == 'mcp_outcome_unknown'
    assert 'super-secret' not in str(exc.value)
    assert sum(b'tools/call' in r.content for r in seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [
    {'jsonrpc': '2.0', 'id': 'wrong', 'result': {'content': []}},
    {'jsonrpc': '2.0', 'id': '3', 'method': 'sampling/createMessage', 'params': {}},
    {'jsonrpc': '2.0', 'id': '3', 'result': {}, 'error': {}},
    {'jsonrpc': '2.0', 'id': '3', 'error': {'code': -1, 'message': 'super-secret'}},
])
async def test_bad_response_or_server_requests_fail_closed(setup_client, payload):
    seen = []
    normal = server({}, seen)
    def handler(request):
        if b'tools/call' in request.content:
            return httpx.Response(200, text='data: ' + json.dumps(payload) + '\n\n',
                                  headers={'Content-Type': 'text/event-stream'})
        return normal(request)
    request, http = setup_client(handler)
    async with http:
        with pytest.raises(HTTPException) as exc:
            await mcp.call_tool(request, mcp.MCPConfig(url='https://mcp.example', api_key_ref='$MCP'), 'run', {})
    assert exc.value.detail['code'] == 'mcp_outcome_unknown'
    assert 'super-secret' not in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['version', 'session', 'redirect', 'oversize', 'schema', 'repeated_cursor'])
async def test_discovery_bounds_and_protocol_validation(setup_client, monkeypatch, kind):
    seen = []
    normal = server({'tools': []}, seen)
    def handler(request):
        response = normal(request)
        if request.method == 'DELETE' or b'notifications/initialized' in request.content:
            return response
        if kind == 'version' and b'initialize' in request.content:
            payload = json.loads(response.content)
            payload['result']['protocolVersion'] = 'unknown'
            return httpx.Response(200, json=payload)
        if kind == 'session' and b'initialize' in request.content:
            response.headers['MCP-Session-Id'] = 'invalid space'
        if kind == 'redirect':
            return httpx.Response(307, headers={'Location': 'https://evil.example'})
        if b'tools/list' in request.content:
            payload = json.loads(response.content)
            if kind == 'oversize':
                return httpx.Response(200, content=b' ' * (mcp.MAX_RESPONSE_BYTES + 1), headers={'Content-Type': 'application/json'})
            if kind == 'schema':
                payload['result']['tools'] = [{'name': 'run', 'inputSchema': {'type': 'not-real'}}]
            if kind == 'repeated_cursor':
                payload['result']['nextCursor'] = 'same'
            return httpx.Response(200, json=payload)
        return response
    request, http = setup_client(handler)
    async with http:
        with pytest.raises(HTTPException) as exc:
            await mcp.list_tools(request, mcp.MCPConfig(url='https://mcp.example', api_key_ref='$MCP'))
    assert exc.value.detail['code'] == 'mcp_error'
    assert len(seen) <= 5


@pytest.mark.asyncio
async def test_pagination_and_cleanup_failure_preserve_result(setup_client):
    seen = []
    normal = server({'tools': []}, seen)
    def handler(request):
        if request.method == 'DELETE':
            raise httpx.ConnectError('cleanup failed')
        response = normal(request)
        if b'tools/list' in request.content:
            message = json.loads(request.content)
            page = 'second' if message['params'].get('cursor') else 'first'
            result = {'tools': [{'name': page, 'inputSchema': {'type': 'object'}}]}
            if page == 'first':
                result['nextCursor'] = 'next'
            return httpx.Response(200, json={'jsonrpc': '2.0', 'id': message['id'], 'result': result})
        return response
    request, http = setup_client(handler)
    async with http:
        result = await mcp.list_tools(request, mcp.MCPConfig(url='https://mcp.example', api_key_ref='$MCP'))
    assert [tool['name'] for tool in result] == ['first', 'second']


@pytest.mark.asyncio
async def test_live_role_rechecked_before_call(setup_client, monkeypatch):
    seen = []
    request, http = setup_client(server({'content': []}, seen))
    async def authorize(request, role, tx):
        if role == 'operator':
            raise HTTPException(403, detail={'code': 'forbidden'})
    monkeypatch.setattr(mcp, 'require_current_role', authorize)
    async with http:
        with pytest.raises(HTTPException) as exc:
            await mcp.call_tool(request, mcp.MCPConfig(url='https://mcp.example', api_key_ref='$MCP'), 'run', {})
    assert exc.value.status_code == 403
    assert not any(b'tools/call' in r.content for r in seen)


@pytest.mark.parametrize('config', [
    {'url': 'https://user:password@example.com'}, {'url': 'https://example.com?token=secret'},
    {'url': 'file:///tmp/x'}, {'url': 'https://example.com', 'api_key_ref': 'plaintext'},
])
def test_configuration_rejects_unsafe_urls_and_plaintext_keys(config):
    with pytest.raises(ValueError):
        mcp.MCPConfig(**config)


class Chunks(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        for byte in self.data:
            yield bytes([byte])


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['\n', '\r', '\r\n'])
async def test_sse_incremental_lines_and_notifications(setup_client, ending):
    seen = []
    normal = server({}, seen)
    def handler(request):
        if b'tools/call' in request.content:
            expected = json.loads(request.content)['id']
            notification = json.dumps({'jsonrpc': '2.0', 'method': 'notifications/progress', 'params': {}})
            result = json.dumps({'jsonrpc': '2.0', 'id': expected, 'result': {'content': []}})
            data = ending.join([': comment', '', 'data: ' + notification, '', 'data: ' + result, '', ''])
            return httpx.Response(200, stream=Chunks(data.encode()), headers={'Content-Type': 'text/event-stream'})
        return normal(request)
    request, http = setup_client(handler)
    async with http:
        assert (await mcp.call_tool(request, mcp.MCPConfig(url='https://mcp.example', api_key_ref='$MCP'), 'run', {})) == {'content': []}


@pytest.mark.asyncio
async def test_external_schema_references_rejected(setup_client):
    tool = {'name': 'run', 'inputSchema': {'type': 'object', 'properties': {'data': {'$ref': 'http://169.254.169.254/latest'}}}}
    request, http = setup_client(server({'tools': [tool]}, []))
    async with http:
        with pytest.raises(HTTPException):
            await mcp.list_tools(request, mcp.MCPConfig(url='https://mcp.example', api_key_ref='$MCP'))
