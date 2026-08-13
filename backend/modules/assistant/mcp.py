"""Bounded Streamable HTTP MCP 2025-11-25 client.

Every tool invocation requires the caller's durable human approval flow. Remote
annotations and descriptions are untrusted data, never authorization.
No stream resumption or retry is performed after a potentially effective call.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from collections.abc import Awaitable, Callable

import httpx
from fastapi import HTTPException
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.requests import HTTPConnection

from backend.auth_gate import fail, require_current_role
from backend.config import VaultError
from backend.net import validate_url
from backend.modules.n8n_proxy.client import _redact_echo

PROTOCOL_VERSION = '2025-11-25'
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = 128 * 1024
TOTAL_TIMEOUT = 45.0
MAX_TOOLS = 128
MAX_PAGES = 8
_NAME = re.compile(r'[A-Za-z0-9_.-]{1,128}\Z')


class MCPConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    url: str = Field(min_length=1, max_length=2048)
    api_key_ref: str | None = Field(default=None, max_length=256)

    @field_validator('url')
    @classmethod
    def valid_url(cls, value: str) -> str:
        parsed = validate_url(value)
        if parsed.query or parsed.fragment:
            raise ValueError('MCP URLs cannot contain queries or fragments')
        return str(parsed)

    @field_validator('api_key_ref')
    @classmethod
    def valid_ref(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r'\$[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?', value):
            raise ValueError('Use a vault or environment reference')
        return value


def _invalid() -> None:
    fail(502, 'mcp_error', 'The MCP server returned an unsupported or invalid response.')


def _json(raw: bytes) -> Any:
    try:
        def reject_constant(value: str) -> None:
            raise ValueError('Non-finite JSON number')
        result = json.loads(raw.decode('utf-8-sig'), parse_constant=reject_constant)
        _depth(result)
        return result
    except (ValueError, UnicodeError, RecursionError):
        _invalid()


def _depth(value: Any, level: int = 0) -> None:
    if level > 32:
        raise ValueError('JSON too deep')
    if isinstance(value, dict):
        for item in value.values():
            _depth(item, level + 1)
    elif isinstance(value, list):
        for item in value:
            _depth(item, level + 1)


def _local_schema(value: Any) -> None:
    """Never allow downstream validators to fetch schema references over egress."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {'$ref', '$dynamicRef', '$recursiveRef'} and (not isinstance(item, str) or not item.startswith('#')):
                _invalid()
            if key == '$id':
                _invalid()  # A nested base URI can turn a fragment into a remote reference.
            _local_schema(item)
    elif isinstance(value, list):
        for item in value:
            _local_schema(item)


class _Session:
    def __init__(self, request: HTTPConnection, config: MCPConfig, before_call: Callable[[], Awaitable[None]] | None = None) -> None:
        self.request = request
        self.config = config
        self.session_id: str | None = None
        self.initialized = False
        self.call_sent = False
        self.sequence = 0
        self.key = ''
        self.before_call = before_call
        if config.api_key_ref:
            try:
                key = request.app.state.vault.resolve(config.api_key_ref)
                if not isinstance(key, str) or not key or len(key) > 8192 or any(not 32 <= ord(c) <= 126 for c in key):
                    raise ValueError
                self.key = key
            except (ValueError, KeyError, VaultError):
                fail(400, 'mcp_configuration_invalid', 'The MCP credential reference could not be resolved.')

    async def authorize(self, role: str) -> None:
        async with self.request.app.state.db.transaction() as tx:
            await require_current_role(self.request, role, tx)

    def headers(self) -> dict[str, str]:
        headers = {'Accept': 'application/json, text/event-stream', 'Accept-Encoding': 'identity'}
        if self.key:
            headers['Authorization'] = f'Bearer {self.key}'
        if self.initialized:
            headers['MCP-Protocol-Version'] = PROTOCOL_VERSION
        if self.session_id:
            headers['MCP-Session-Id'] = self.session_id
        return headers

    async def initialize(self) -> None:
        await self.authorize('viewer')
        result = await self.post('initialize', {'protocolVersion': PROTOCOL_VERSION,
            'capabilities': {}, 'clientInfo': {'name': 'Inference', 'version': '1.0.0'}})
        if (result.get('protocolVersion') != PROTOCOL_VERSION or
                not isinstance(result.get('capabilities'), dict) or
                not isinstance(result['capabilities'].get('tools'), dict) or
                not isinstance(result.get('serverInfo'), dict)):
            _invalid()
        self.initialized = True
        await self.post('notifications/initialized', None, notification=True)

    async def post(self, method: str, params: dict[str, Any] | None, *, notification: bool = False) -> dict[str, Any]:
        self.sequence += 1
        expected = str(self.sequence)
        message: dict[str, Any] = {'jsonrpc': '2.0', 'method': method}
        if not notification:
            message['id'] = expected
        if params is not None:
            message['params'] = params
        try:
            _depth(message)
            body = json.dumps(message, allow_nan=False).encode()
            if len(body) > MAX_REQUEST_BYTES:
                raise ValueError
        except (ValueError, TypeError, RecursionError):
            fail(400, 'mcp_arguments_invalid', 'MCP arguments exceed the supported JSON limits.')
        outbound = httpx.Request('POST', self.config.url, content=body,
            headers={**self.headers(), 'Content-Type': 'application/json'},
            extensions={'timeout': {'connect': 5, 'read': 30, 'write': 15, 'pool': 5}})
        if method == 'tools/call':
            await self.authorize('operator')
            if self.before_call is not None:
                await self.before_call()
            self.call_sent = True
        response = await self.request.app.state.http.send(outbound, stream=True, auth=None, follow_redirects=False)
        try:
            if response.status_code != (202 if notification else 200):
                _invalid()
            if response.headers.get('content-encoding', 'identity').lower() != 'identity':
                _invalid()
            session_id = response.headers.get('MCP-Session-Id')
            if method == 'initialize' and session_id is not None:
                if not 1 <= len(session_id) <= 512 or any(not 33 <= ord(c) <= 126 for c in session_id):
                    _invalid()
                self.session_id = session_id
            elif session_id is not None and session_id != self.session_id:
                _invalid()
            media = response.headers.get('content-type', '').split(';')[0].strip().lower()
            if not notification and media not in {'application/json', 'text/event-stream'}:
                _invalid()
            buffer = b''
            count = 0
            previous_cr = False
            async for chunk in response.aiter_bytes():
                count += len(chunk)
                if count > MAX_RESPONSE_BYTES:
                    _invalid()
                if media == 'text/event-stream':
                    # SSE permits CR, LF and CRLF, including split CRLF chunks.
                    if previous_cr and chunk.startswith(b'\n'):
                        chunk = chunk[1:]
                    previous_cr = chunk.endswith(b'\r') if chunk else False
                    buffer += chunk.replace(b'\r\n', b'\n').replace(b'\r', b'\n')
                    while b'\n\n' in buffer:
                        event, buffer = buffer.split(b'\n\n', 1)
                        data = []
                        for line in event.split(b'\n'):
                            if line == b'data':
                                data.append(b'')
                            elif line.startswith(b'data:'):
                                data.append(line[5:].removeprefix(b' '))
                        payload = b'\n'.join(data)
                        if payload:
                            result = self.envelope(_json(payload), expected, allow_notification=True)
                            if result is not None:
                                return result
                else:
                    buffer += chunk
            if notification:
                if buffer:
                    _invalid()
                return {}
            if media == 'text/event-stream':
                _invalid()  # Disconnect never means the remote call was cancelled.
            return self.envelope(_json(buffer), expected)
        finally:
            await response.aclose()

    @staticmethod
    def envelope(message: Any, expected: str, *, allow_notification: bool = False) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
            _invalid()
        if 'method' in message:
            if ('id' in message or not allow_notification or
                    message['method'] not in {'notifications/progress', 'notifications/message', 'notifications/tools/list_changed'}):
                _invalid()  # Never service sampling, elicitation, or other server requests.
            return None
        if message.get('id') != expected or ('result' in message) == ('error' in message):
            _invalid()
        if 'error' in message:
            fail(502, 'mcp_error', 'The MCP server rejected the request.')
        if not isinstance(message['result'], dict):
            _invalid()
        return message['result']

    async def cleanup(self) -> None:
        if self.session_id:
            async def close() -> None:
                response = await self.request.app.state.http.send(
                    httpx.Request('DELETE', self.config.url, headers=self.headers(),
                        extensions={'timeout': {'connect': 2, 'read': 2, 'write': 2, 'pool': 2}}),
                    auth=None, follow_redirects=False, stream=True)
                await response.aclose()
            try:
                await asyncio.wait_for(close(), timeout=2)
            except Exception:
                pass  # Cleanup failure must not change the remote operation result.


async def _run(request: HTTPConnection, config: MCPConfig, name: str | None, arguments: dict[str, Any] | None,
               before_call: Callable[[], Awaitable[None]] | None = None) -> Any:
    session = _Session(request, config, before_call)
    async def operation() -> Any:
        await session.initialize()
        if name is not None:
            result = await session.post('tools/call', {'name': name, 'arguments': arguments})
            if not isinstance(result.get('content'), list) or ('isError' in result and not isinstance(result['isError'], bool)):
                _invalid()
            return _redact_echo(result, (session.key,)) if session.key else result
        tools: list[dict[str, Any]] = []
        names: set[str] = set()
        cursors: set[str] = set()
        cursor = None
        total_size = 0
        for _ in range(MAX_PAGES):
            await session.authorize('viewer')
            result = await session.post('tools/list', {'cursor': cursor} if cursor else {})
            page = result.get('tools')
            if not isinstance(page, list) or len(page) + len(tools) > MAX_TOOLS:
                _invalid()
            for tool in page:
                if not isinstance(tool, dict) or not isinstance(tool.get('name'), str) or not _NAME.fullmatch(tool['name']) or tool['name'] in names:
                    _invalid()
                schema = tool.get('inputSchema')
                size = len(json.dumps(tool).encode())
                total_size += size
                if size > 65536 or total_size > MAX_RESPONSE_BYTES or not isinstance(schema, dict) or schema.get('type') != 'object':
                    _invalid()
                try:
                    _local_schema(schema)
                    Draft202012Validator.check_schema(schema)
                except (SchemaError, RecursionError):
                    _invalid()
                names.add(tool['name'])
                tools.append(_redact_echo(tool, (session.key,)) if session.key else tool)
            cursor = result.get('nextCursor')
            if cursor is None:
                return tools
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048 or cursor in cursors:
                _invalid()
            cursors.add(cursor)
        _invalid()
    try:
        return await asyncio.wait_for(operation(), timeout=TOTAL_TIMEOUT)
    except (HTTPException, httpx.HTTPError, asyncio.TimeoutError, ValueError, UnicodeError, RecursionError) as exc:
        if session.call_sent:
            fail(502, 'mcp_outcome_unknown', 'The MCP tool outcome is unknown. Check remote state before proposing another call.')
        if isinstance(exc, HTTPException):
            raise
        fail(502, 'mcp_error', 'The MCP server could not complete the request.')
    finally:
        await session.cleanup()


async def list_tools(request: HTTPConnection, config: MCPConfig) -> list[dict[str, Any]]:
    """Discover bounded schemas; readOnlyHint never grants invocation permission."""
    return await _run(request, config, None, None)


async def call_tool(request: HTTPConnection, config: MCPConfig, name: str, arguments: dict[str, Any], *,
                    before_call: Callable[[], Awaitable[None]] | None = None) -> dict[str, Any]:
    """Invoke once, after the caller consumes a durable approval for these args."""
    if not isinstance(name, str) or not _NAME.fullmatch(name) or not isinstance(arguments, dict):
        fail(400, 'mcp_arguments_invalid', 'A valid MCP tool name and argument object are required.')
    return await _run(request, config, name, arguments, before_call)
