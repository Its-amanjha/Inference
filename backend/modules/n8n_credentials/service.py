"""Credential services. Secret values remain server side; callers journal writes."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from fastapi import HTTPException, Request
from jsonschema import validators
from jsonschema.exceptions import SchemaError, ValidationError

from backend.auth_gate import fail, require_current_role, require_role
from backend.config import VaultError
from backend.modules.n8n_proxy.client import N8nClient

IDENTIFIER = r'^[A-Za-z0-9_-]{1,128}$'


def mapping_key(source: str, target: str, resource: str) -> str:
    return hashlib.sha256(json.dumps([source, target, resource], ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def _metadata(value: Any, mutation: bool = False) -> dict[str, str]:
    if not isinstance(value, dict) or any(not isinstance(value.get(key), str) or not value[key] for key in ('id', 'name', 'type')) or not re.fullmatch(IDENTIFIER, value['id']) or not re.fullmatch(IDENTIFIER, value['type']) or len(value['name']) > 256:
        fail(502, 'upstream_outcome_unknown' if mutation else 'upstream_invalid_response', 'Credential response is invalid. Reconcile state before retrying.' if mutation else 'Credential discovery returned invalid metadata.')
    return {key: value[key] for key in ('id', 'name', 'type')}


async def discover(request: Request, instance_id: str) -> dict[str, Any]:
    require_role(request, 'admin')
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    ids: set[str] = set()
    cursor: str | None = None
    for _ in range(20):
        params: dict[str, Any] = {'limit': 250}
        if cursor is not None:
            params['cursor'] = cursor
        try:
            page = await N8nClient(request).request(instance_id, 'GET', '/credentials', params=params)
        except HTTPException as exc:
            code = exc.detail.get('code') if isinstance(exc.detail, dict) else None
            if code in {'upstream_forbidden', 'upstream_not_found'}:
                return {'supported': False, 'data': [], 'diagnostics': [{'code': 'credential_discovery_unavailable', 'message': 'Credential discovery is unavailable for this API key or n8n version. Supply explicit credential IDs and types; bindings cannot be verified automatically.'}]}
            raise
        if not isinstance(page.get('data'), list) or len(page['data']) > 250:
            fail(502, 'upstream_invalid_response', 'Credential discovery returned an invalid page.')
        for value in page['data']:
            item = _metadata(value)
            if item['id'] in ids:
                fail(502, 'upstream_invalid_response', 'Credential discovery returned duplicate identifiers.')
            ids.add(item['id'])
            result.append(item)
        cursor = page.get('nextCursor')
        if cursor is None or cursor == '':
            return {'supported': True, 'data': result, 'diagnostics': []}
        if not isinstance(cursor, str) or len(cursor) > 4096 or cursor in seen:
            fail(502, 'upstream_invalid_response', 'Credential discovery returned invalid pagination.')
        seen.add(cursor)
    fail(502, 'credential_discovery_limit', 'Credential discovery exceeded its page limit. Reduce the credential scope before retrying.')
    raise AssertionError('unreachable')


def _check_schema(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {'$ref', '$dynamicRef', '$recursiveRef'} and (not isinstance(child, str) or not child.startswith('#')):
                fail(502, 'credential_schema_unsupported', 'External schema references are not supported.')
            _check_schema(child)
    elif isinstance(value, list):
        for child in value:
            _check_schema(child)


async def schema(request: Request, instance_id: str, credential_type: str) -> dict[str, Any]:
    require_role(request, 'admin')
    if not re.fullmatch(IDENTIFIER, credential_type):
        fail(422, 'invalid_credential_type', 'Invalid credential type.')
    value = await N8nClient(request).request(instance_id, 'GET', f'/credentials/schema/{credential_type}')
    try:
        _check_schema(value)
        validators.validator_for(value).check_schema(value)
    except (SchemaError, RecursionError):
        fail(502, 'credential_schema_invalid', 'The credential schema is invalid or too complex.')
    return value


async def validate_secret(request: Request, instance_id: str, credential_type: str, reference: str) -> dict[str, Any]:
    require_role(request, 'admin')
    reference_name = r'[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*'
    if not isinstance(reference, str) or not re.fullmatch(rf'\$(?:{reference_name}|\{{{reference_name}\}})', reference):
        fail(422, 'invalid_secret_reference', 'Use an explicit vault or environment reference.')
    definition = await schema(request, instance_id, credential_type)
    async with request.app.state.db.transaction() as tx:
        await require_current_role(request, 'admin', tx)
    try:
        payload = request.app.state.vault.resolve(reference)
        if not isinstance(payload, dict):
            fail(422, 'invalid_credential_secret', 'Credential secret must contain a JSON object.')
        validators.validator_for(definition)(definition).validate(payload)
    except (VaultError, KeyError, ValueError, TypeError, ValidationError, RecursionError):
        fail(422, 'invalid_credential_secret', 'The credential secret is unavailable or does not match its schema.')
    except Exception:
        # Reference resolution and unsupported schema vocabulary must not echo secrets.
        fail(422, 'invalid_credential_secret', 'The credential secret could not be validated.')
    return payload


async def provision(request: Request, instance_id: str, credential_type: str, name: str, reference: str, *, expected_hash: str | None = None) -> dict[str, str]:
    require_role(request, 'admin')
    if not isinstance(name, str) or not name.strip() or len(name) > 256:
        fail(422, 'invalid_credential_name', 'A credential name of at most 256 characters is required.')
    payload = await validate_secret(request, instance_id, credential_type, reference)
    if expected_hash is not None:
        current_hash = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':')).encode()).hexdigest()
        if current_hash != expected_hash:
            fail(409, 'stale_preview', 'The credential secret changed after preview. Run preflight again.')
    result = await N8nClient(request).request(instance_id, 'POST', '/credentials', json={'name': name, 'type': credential_type, 'data': payload})
    # Return submitted metadata; remote servers may echo submitted secret fields.
    metadata = _metadata(result, mutation=True)
    if metadata['type'] != credential_type:
        fail(502, 'upstream_outcome_unknown', 'Credential creation returned an unexpected type. Reconcile state before retrying.')
    return {'id': metadata['id'], 'name': name, 'type': credential_type}
