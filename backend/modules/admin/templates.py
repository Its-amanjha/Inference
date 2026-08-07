"""Small, built-in schemas for create-only compound vault secrets."""
import asyncio
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from backend.auth_gate import fail, require_current_role, require_role
from backend.database import _settle

router = APIRouter()

TEMPLATES = {
    'api_key': {'title': 'API key', 'fields': [
        {'name': 'api_key', 'label': 'API key', 'required': True, 'sensitive': True}]},
    'basic_auth': {'title': 'Basic authentication', 'fields': [
        {'name': 'username', 'label': 'Username', 'required': True, 'sensitive': False},
        {'name': 'password', 'label': 'Password', 'required': True, 'sensitive': True}]},
    's3': {'title': 'S3 credentials', 'fields': [
        {'name': 'access_key_id', 'label': 'Access key ID', 'required': True, 'sensitive': True},
        {'name': 'secret_access_key', 'label': 'Secret access key', 'required': True, 'sensitive': True},
        {'name': 'session_token', 'label': 'Session token', 'required': False, 'sensitive': True}]},
}


class ApplyTemplate(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    name: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z_][A-Za-z0-9_]*$')
    values: dict[str, str] = Field(max_length=3)


@router.get('/secret-templates')
async def catalog(request: Request) -> dict[str, Any]:
    require_role(request, 'admin')
    return {'data': [{'id': key, **value} for key, value in TEMPLATES.items()]}


@router.post('/secret-templates/{template_id}/apply')
async def apply_template(template_id: str, body: ApplyTemplate, request: Request) -> dict[str, str]:
    require_role(request, 'admin')
    template = TEMPLATES.get(template_id)
    if template is None:
        fail(404, 'template_not_found', 'Secret template was not found.')
    allowed = {field['name'] for field in template['fields']}
    required = {field['name'] for field in template['fields'] if field['required']}
    if set(body.values) - allowed or any(not body.values.get(key, '').strip() for key in required):
        fail(422, 'invalid_template_values', 'Supply all required fields and only fields declared by the template.')
    values = {key: value for key, value in body.values.items() if key in required or value.strip()}
    try:
        size = len(json.dumps(body.values, ensure_ascii=False).encode('utf-8'))
    except UnicodeError:
        fail(422, 'invalid_template_values', 'Secret fields must contain valid UTF-8 text.')
    if size > 64 * 1024:
        fail(413, 'secret_too_large', 'A secret must be at most 64 KiB.')

    vault = request.app.state.vault
    applied = False

    def create() -> None:
        nonlocal applied
        # The same reentrant vault lock protects ordinary secret writes too.
        with vault._lock:
            if body.name in vault.list():
                fail(409, 'secret_exists', 'A secret with that name already exists.')
            vault.set(body.name, values)
            applied = True

    async def commit() -> None:
        async with request.app.state.db.transaction() as tx:
            await require_current_role(request, 'admin', tx)
            try:
                await asyncio.to_thread(create)
            except HTTPException:
                raise
            except Exception:
                fail(503, 'secret_outcome_unknown', 'Vault persistence could not be confirmed. Check the secret list before retrying.')
            await tx.execute('INSERT INTO audit_events(actor,action,target,details_json) VALUES(?,?,?,?)',
                (request.state.user['username'], 'admin.secret_template_applied', body.name,
                 json.dumps({'template_id': template_id})))

    try:
        # Cancellation must not release the DB lock before the file write/audit finish.
        await _settle(commit())
    except Exception as exc:
        if applied and not isinstance(exc, HTTPException):
            fail(503, 'secret_outcome_unknown', 'The secret was saved but audit persistence failed. Check the secret list before retrying.')
        raise
    return {'name': body.name, 'template_id': template_id}
