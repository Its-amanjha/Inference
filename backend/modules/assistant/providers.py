"""Bounded non-streaming adapters; tool output never authorizes execution.

Wire contracts verified against official references:
https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create
https://platform.claude.com/docs/en/api/messages/create
https://docs.ollama.com/api/openai-compatibility
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
from typing import Any, Literal

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.requests import HTTPConnection

from backend.auth_gate import fail
from backend.config import VaultError
from backend.net import allowed_ip, validate_url

MAX_RESPONSE_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = 256 * 1024
# Hosted APIs answer a non-streaming turn in seconds; these budgets fail a hung call fast.
READ_TIMEOUT = 45.0
TOTAL_TIMEOUT = 60.0
# Local models (Ollama, or a custom server on a loopback/private address) run on the operator's
# own hardware, often CPU-only: a non-streaming reply arrives only after the whole answer is
# generated. Measured on qwen2.5:3b (CPU, 4-5.5 tokens/s) with an n8n skill guide in context:
# 124-176s standalone and 228s inside a live chat, for 550-900 token answers. A 4096-token
# context caps an answer near 2000 tokens, roughly 400-500s at that speed, so 600s covers the
# worst case rather than the typical one. There is no per-token cost and the endpoint is the
# operator's own machine, so a longer wait wastes nothing; a hung server is still cut off.
LOCAL_READ_TIMEOUT = 600.0
LOCAL_TOTAL_TIMEOUT = 630.0  # read + connect/write/pool + response body read.
MAX_RETRY_AFTER_SECONDS = 24 * 60 * 60
# Output cut short by a token limit or a content filter is never treated as an answer.
INCOMPLETE_OPENAI = frozenset({'length', 'content_filter'})
INCOMPLETE_ANTHROPIC = frozenset({'max_tokens', 'pause_turn'})
_REFERENCE = re.compile(r'\$(?:[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?|\{[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?\})\Z')
_SCHEMA_DROPPED = frozenset({'title', 'discriminator', '$defs', 'definitions', '$schema', '$id', 'examples'})
_MAX_SCHEMA_DEPTH = 48


class _Incomplete(ValueError):
    """The provider stopped before completing its answer."""


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['openai', 'openrouter', 'groq', 'ollama', 'custom', 'anthropic']
    base_url: str = Field(min_length=1, max_length=2048)
    model: str = Field(min_length=1, max_length=256)
    api_key_ref: str | None = Field(default=None, max_length=256)
    tools_enabled: bool = True

    @field_validator('base_url')
    @classmethod
    def endpoint(cls, value: str) -> str:
        parsed = validate_url(value)
        if parsed.query or parsed.fragment:
            raise ValueError('Provider base URL must not contain query or fragment')
        return str(parsed).rstrip('/')

    @field_validator('api_key_ref')
    @classmethod
    def reference(cls, value: str | None) -> str | None:
        if value is not None and not _REFERENCE.fullmatch(value):
            raise ValueError('Use a whole vault or environment reference')
        return value


def _json(value: str | bytes) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError('Non-finite JSON')
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = item
        return result
    return json.loads(value, parse_constant=reject_constant, object_pairs_hook=unique)


def _redact(value: Any, key: str | None) -> Any:
    if not key:
        return value
    if isinstance(value, str):
        return value.replace(key, '[redacted]')
    if isinstance(value, list):
        return [_redact(item, key) for item in value]
    if isinstance(value, dict):
        return {_redact(name, key): _redact(item, key) for name, item in value.items()}
    return value


def _anthropic(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
    system: list[str] = []
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = message['role']
        content = message.get('content')
        if role == 'system':
            system.append(content)
            continue
        blocks: list[dict[str, Any]] = []
        if role == 'tool':
            blocks.append({'type': 'tool_result', 'tool_use_id': message['tool_call_id'], 'content': content})
            role = 'user'
        else:
            if content:
                blocks.append({'type': 'text', 'text': content})
            for call in message.get('tool_calls', []):
                arguments = _json(call['function']['arguments'])
                if not isinstance(arguments, dict):
                    raise ValueError('Tool arguments must be objects')
                blocks.append({'type': 'tool_use', 'id': call['id'], 'name': call['function']['name'], 'input': arguments})
        if converted and converted[-1]['role'] == role:
            converted[-1]['content'].extend(blocks)
        else:
            converted.append({'role': role, 'content': blocks})
    result: dict[str, Any] = {'messages': converted, 'max_tokens': 4096}
    if system:
        result['system'] = '\n\n'.join(system)
    if tools:
        result['tools'] = [{'name': tool['function']['name'], 'description': tool['function'].get('description', ''), 'input_schema': tool['function']['parameters']} for tool in tools]
    return result


def flatten_schema(schema: Any) -> Any:
    """Return a provider-portable JSON Schema: local references inlined, no $defs,
    discriminator or title annotations, and discriminated unions merged into one
    object whose discriminator property is an enum. Pydantic models stay the
    authoritative validators; this only shapes what models are shown.
    """
    definitions = schema.get('$defs', {}) if isinstance(schema, dict) else {}
    if not isinstance(definitions, dict):
        raise ValueError('Invalid schema definitions')

    def canonical(value: Any) -> str:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)

    def optional(value: Any) -> Any:
        # `X | None = None` becomes an omittable X; explicit nulls stay where they matter.
        if (isinstance(value, dict) and value.get('default', 0) is None and isinstance(value.get('anyOf'), list)
                and len(value['anyOf']) == 2 and {'type': 'null'} in value['anyOf']):
            other = next(item for item in value['anyOf'] if item != {'type': 'null'})
            extra = {key: item for key, item in value.items() if key not in {'anyOf', 'default'}}
            return {**other, **extra}
        return value

    def merge(variants: list[Any], property_name: str, description: Any) -> dict[str, Any]:
        if not variants or any(not isinstance(item, dict) or item.get('type') != 'object'
                               or not isinstance(item.get('properties'), dict) for item in variants):
            raise ValueError('Only object unions can be merged')
        tags: list[str] = []
        collected: dict[str, list[Any]] = {}
        hints: list[str] = []
        for variant in variants:
            tag = variant['properties'].get(property_name, {})
            value = tag.get('const', (tag.get('enum') or [None])[0]) if isinstance(tag, dict) else None
            if not isinstance(value, str):
                raise ValueError('Union variants need a string discriminator')
            tags.append(value)
            required = [name for name in variant.get('required', []) if name != property_name]
            hints.append(f"{value}: {', '.join(required) if required else 'no other fields'}")
            for name, child in variant['properties'].items():
                if name == property_name:
                    continue
                known = collected.setdefault(name, [])
                if canonical(child) not in {canonical(item) for item in known}:
                    known.append(child)
        properties: dict[str, Any] = {property_name: {'type': 'string', 'enum': tags}}
        for name, options in collected.items():
            properties[name] = options[0] if len(options) == 1 else {} if {} in options else {'anyOf': options}
        text = f"Set {property_name} to one of the listed values; required fields per value: {'; '.join(hints)}."
        if isinstance(description, str) and description:
            text = description + ' ' + text
        return {'type': 'object', 'description': text, 'properties': properties,
                'required': [property_name], 'additionalProperties': False}

    def resolve(node: Any, depth: int, stack: frozenset[str]) -> Any:
        if depth > _MAX_SCHEMA_DEPTH:
            raise ValueError('Schema nesting is too deep')
        if isinstance(node, list):
            return [resolve(item, depth + 1, stack) for item in node]
        if not isinstance(node, dict):
            return node
        if '$ref' in node:
            reference = node['$ref']
            if not isinstance(reference, str) or not reference.startswith('#/$defs/'):
                raise ValueError('Only local schema references can be flattened')
            name = reference[len('#/$defs/'):]
            if name in stack or name not in definitions:
                raise ValueError('Recursive or missing schema reference')
            local = {key: value for key, value in node.items() if key != '$ref'}
            return resolve({**definitions[name], **local}, depth + 1, stack | {name})
        discriminator = node.get('discriminator')
        result: dict[str, Any] = {}
        for key, value in node.items():
            if key in _SCHEMA_DROPPED:
                continue
            if key == 'properties' and isinstance(value, dict):
                result[key] = {name: optional(resolve(child, depth + 1, stack)) for name, child in value.items()}
            else:
                result[key] = resolve(value, depth + 1, stack)
        if isinstance(discriminator, dict):
            union = result.pop('oneOf', None) or result.pop('anyOf', None)
            if not isinstance(union, list) or not isinstance(discriminator.get('propertyName'), str):
                raise ValueError('Unsupported discriminated union')
            result = {**merge(union, discriminator['propertyName'], result.pop('description', None)), **result}
        return result

    return resolve(schema, 0, frozenset())


def _arguments(value: Any) -> dict[str, Any]:
    """OpenAI-compatible arguments arrive as a JSON string (duplicate keys rejected) or an object."""
    if isinstance(value, str):
        value = _json(value) if value.strip() else {}
    if not isinstance(value, dict):
        raise ValueError('Tool arguments must be objects')
    return value


def _normalize(data: Any, kind: str, names: set[str]) -> dict[str, Any]:
    """Normalize every provider kind into {content, tool_calls: [{id, name, arguments}], usage}."""
    calls: list[dict[str, Any]] = []
    if not isinstance(data, dict):
        raise ValueError('Invalid provider response')
    if kind == 'anthropic':
        reason = data['stop_reason']
        if reason in INCOMPLETE_ANTHROPIC:
            raise _Incomplete('Truncated provider response')
        if reason not in {'end_turn', 'tool_use', 'stop_sequence'}:
            raise ValueError('Unsupported stop reason')
        content = ''
        blocks = data['content']
        if not isinstance(blocks, list) or not blocks:
            raise ValueError('Missing content')
        for block in blocks:
            if block['type'] == 'text':
                if not isinstance(block['text'], str):
                    raise ValueError('Invalid text block')
                content += block['text']
            elif block['type'] == 'tool_use':
                if not isinstance(block['input'], dict):
                    raise ValueError('Tool input must be an object')
                calls.append({'id': block['id'], 'name': block['name'], 'arguments': block['input']})
            else:
                raise ValueError('Unsupported content block')
        if bool(calls) != (reason == 'tool_use'):
            raise ValueError('Inconsistent tool response')
    else:
        choices = data['choices']
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError('Expected one choice')
        choice = choices[0]
        reason = choice['finish_reason']
        if reason in INCOMPLETE_OPENAI:
            raise _Incomplete('Truncated provider response')
        if reason not in {'stop', 'tool_calls'}:
            raise ValueError('Incomplete provider response')
        message = choice['message']
        content = message.get('content')
        raw_calls = message.get('tool_calls')
        raw_calls = [] if raw_calls is None else raw_calls
        if not isinstance(raw_calls, list):
            raise ValueError('Invalid tool calls')
        for call in raw_calls:
            if not isinstance(call, dict) or call.get('type', 'function') != 'function':
                raise ValueError('Unsupported tool type')
            calls.append({'id': call['id'], 'name': call['function']['name'], 'arguments': _arguments(call['function']['arguments'])})
        # Some compatible servers report "stop" alongside complete tool calls; "tool_calls" needs calls.
        if reason == 'tool_calls' and not calls:
            raise ValueError('Inconsistent tool response')
        if content is None and calls:
            content = ''
    if not isinstance(content, str) or len(calls) > 16:
        raise ValueError('Invalid provider content')
    ids: set[str] = set()
    for call in calls:
        if not isinstance(call['id'], str) or not 1 <= len(call['id']) <= 256 or call['id'] in ids:
            raise ValueError('Invalid tool ID')
        ids.add(call['id'])
        if not isinstance(call['name'], str) or call['name'] not in names or not isinstance(call['arguments'], dict):
            raise ValueError('Unexpected tool invocation')
    usage = data.get('usage') or {}
    if not isinstance(usage, dict):
        raise ValueError('Invalid usage')
    return {'content': content, 'tool_calls': calls, 'usage': usage}


def retry_after_seconds(value: str | None) -> int | None:
    """Only a plain numeric Retry-After is reported; HTTP dates and junk become null."""
    if not isinstance(value, str) or not re.fullmatch(r'\d{1,9}', value.strip()):
        return None
    return min(int(value.strip()), MAX_RETRY_AFTER_SECONDS)


def is_local(config: ProviderConfig) -> bool:
    """Ollama, or a custom endpoint whose host is localhost or a loopback/private IP literal.

    Hostnames are not resolved here: a public name never earns the local budget by DNS alone.
    """
    if config.kind == 'ollama':
        return True
    if config.kind != 'custom':
        return False
    host = (httpx.URL(config.base_url).host or '').lower()
    if host == 'localhost' or host.endswith('.localhost'):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private


def _budget(config: ProviderConfig) -> tuple[float, float]:
    """(read, total) seconds for one provider call."""
    return (LOCAL_READ_TIMEOUT, LOCAL_TOTAL_TIMEOUT) if is_local(config) else (READ_TIMEOUT, TOTAL_TIMEOUT)


def _declaration(tool: dict[str, Any]) -> dict[str, Any]:
    function = tool['function']
    if tool.get('type', 'function') != 'function' or not isinstance(function['name'], str):
        raise ValueError('Unsupported tool declaration')
    declaration = {'name': function['name'], 'description': function.get('description', ''),
                   'parameters': flatten_schema(function['parameters'])}
    if not isinstance(declaration['description'], str) or not isinstance(declaration['parameters'], dict):
        raise ValueError('Invalid tool declaration')
    return {'type': 'function', 'function': declaration}


def _compatible_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Forward only wire fields; local annotations such as action_ids never leave the server."""
    result: list[dict[str, Any]] = []
    for message in messages:
        item: dict[str, Any] = {'role': message['role'], 'content': message.get('content')}
        if message['role'] == 'assistant' and message.get('tool_calls'):
            item['tool_calls'] = [{'id': call['id'], 'type': 'function', 'function': {
                'name': call['function']['name'], 'arguments': call['function']['arguments']}} for call in message['tool_calls']]
        if message['role'] == 'tool':
            item['tool_call_id'] = message['tool_call_id']
        result.append(item)
    return result


async def complete(request: HTTPConnection, config: ProviderConfig, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Call a configured provider once. Caller owns RBAC and history selection.

    The shared pinned transport checks DNS and CIDRs at connection time; no
    alternate transport, redirects, client cookies or client auth are used.
    """
    key: str | None = None
    try:
        endpoint = validate_url(config.base_url)
        try:
            literal = ipaddress.ip_address(endpoint.host)
        except ValueError:
            literal = None
        if literal is not None and not allowed_ip(str(literal), request.app.state.settings.allow_cidrs):
            raise ValueError('Blocked endpoint')
        if config.api_key_ref:
            key = request.app.state.vault.resolve(config.api_key_ref)
            if not isinstance(key, str) or not key or not key.isascii() or any(ord(c) < 33 or ord(c) == 127 for c in key):
                raise ValueError('Invalid provider key')
        elif config.kind not in {'ollama', 'custom'}:
            raise ValueError('Provider key required')
    except (TypeError, ValueError, VaultError, KeyError):
        fail(400, 'provider_configuration_invalid', 'Check the provider endpoint and secret reference.')

    try:
        if not isinstance(messages, list) or not 1 <= len(messages) <= 100:
            raise ValueError('Invalid messages')
        for message in messages:
            if message['role'] not in {'system', 'user', 'assistant', 'tool'}:
                raise ValueError('Invalid message role')
            if not isinstance(message.get('content'), str) and not (message['role'] == 'assistant' and message.get('tool_calls')):
                raise ValueError('Only text messages supported')
        selected = tools or [] if config.tools_enabled else []
        if not isinstance(selected, list) or len(selected) > 64:
            raise ValueError('Too many tools')
        selected = [_declaration(tool) for tool in selected]
        names = {tool['function']['name'] for tool in selected}
        if len(names) != len(selected):
            raise ValueError('Duplicate tool names')
        body = _anthropic(messages, selected) if config.kind == 'anthropic' else {'messages': _compatible_messages(messages), **({'tools': selected} if selected else {})}
        body.update(model=config.model, stream=False)
        encoded = json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(encoded) > MAX_REQUEST_BYTES:
            raise ValueError('Request too large')
    except (KeyError, TypeError, ValueError, RecursionError, UnicodeError):
        fail(400, 'provider_input_invalid', 'The provider messages or tool declarations are invalid or too large.')

    read_timeout, total_timeout = _budget(config)

    async def perform() -> dict[str, Any]:
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json', 'Accept-Encoding': 'identity'}
        if config.kind == 'anthropic':
            headers['anthropic-version'] = '2023-06-01'
            headers['x-api-key'] = key
            suffix = '/messages'
        else:
            if key:
                headers['Authorization'] = f'Bearer {key}'
            suffix = '/chat/completions'
        outbound = httpx.Request('POST', config.base_url + suffix, headers=headers, content=encoded, extensions={'timeout': {'connect': 5.0, 'read': read_timeout, 'write': 10.0, 'pool': 5.0}})
        response = await request.app.state.http.send(outbound, stream=True, auth=None, follow_redirects=False)
        try:
            if response.status_code == 429:
                raise HTTPException(status_code=429, detail={
                    'detail': 'The provider is rate limiting requests. No automatic retry was attempted.',
                    'code': 'provider_rate_limited',
                    'retry_after_seconds': retry_after_seconds(response.headers.get('retry-after'))})
            if response.status_code != 200:
                fail(502, 'provider_error', 'The provider rejected or could not complete the request.')
            if response.headers.get('content-encoding', 'identity').lower() != 'identity':
                fail(502, 'provider_invalid_response', 'The provider returned an unsupported response encoding.')
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                if len(chunks) + len(chunk) > MAX_RESPONSE_BYTES:
                    fail(502, 'provider_invalid_response', 'The provider response exceeded the size limit.')
                chunks.extend(chunk)
            try:
                result = _normalize(_json(bytes(chunks)), config.kind, names)
                return _redact(result, key)
            except _Incomplete:
                fail(502, 'provider_incomplete', 'The provider stopped before completing its answer (output limit or content filter). Nothing was proposed.')
            except (KeyError, TypeError, ValueError, IndexError, RecursionError, UnicodeError):
                fail(502, 'provider_invalid_response', 'The provider returned an invalid or incomplete response.')
        finally:
            await response.aclose()

    try:
        return await asyncio.wait_for(perform(), timeout=total_timeout)
    except (httpx.HTTPError, asyncio.TimeoutError, OSError, ValueError):
        fail(502, 'provider_unavailable', 'The provider request failed. No automatic retry was attempted.')
