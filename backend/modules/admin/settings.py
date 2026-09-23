"""Persisted application policy with deployment security settings kept explicit."""
import asyncio
from dataclasses import replace
import hashlib
import json
from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from backend.auth_gate import fail, require_current_role, require_role
from backend.config import env_is_set
from backend.database import _settle

router = APIRouter()
KEY = 'admin:system_settings'


class DefaultPreferences(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    theme: Literal['dark', 'light', 'n8n'] = 'light'
    page_size: int = Field(default=25, ge=10, le=100)
    error_notifications: bool = True


class Overlay(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    session_hours: int = Field(default=12, ge=1, le=168)
    default_preferences: DefaultPreferences = Field(default_factory=DefaultPreferences)


class UpdateSettings(Overlay):
    expected_revision: str = Field(pattern=r'^[0-9a-f]{64}$')


def revision(value: Overlay) -> str:
    return hashlib.sha256(json.dumps(value.model_dump(), sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def apply(app: Any, value: Overlay) -> None:
    app.state.system_overlay = value
    hours = app.state.deployment_session_hours if app.state.session_hours_locked else value.session_hours
    app.state.settings = replace(app.state.settings, session_hours=hours)


async def initialize(app: Any) -> None:
    app.state.system_settings_lock = asyncio.Lock()
    app.state.deployment_session_hours = app.state.settings.session_hours
    app.state.session_hours_locked = env_is_set('SESSION_HOURS')
    row = await app.state.db.fetchone('SELECT value_json FROM documents WHERE key=?', (KEY,))
    # Invalid persisted policy fails startup instead of silently changing behavior.
    value = Overlay.model_validate_json(row['value_json']) if row else Overlay(session_hours=app.state.settings.session_hours)
    apply(app, value)


def view(app: Any) -> dict[str, Any]:
    config = app.state.settings
    value = app.state.system_overlay
    return {'secure_cookies': config.secure_cookies, 'session_hours': config.session_hours,
            'allowed_origins': list(config.allowed_origins), 'allow_cidrs': list(config.allow_cidrs),
            'modules': [m.model_dump() for m in app.state.manifests],
            'default_preferences': value.default_preferences.model_dump(),
            'saved': value.model_dump(), 'revision': revision(value),
            'environment_locked_fields': ['session_hours'] if app.state.session_hours_locked else []}


@router.get('/settings')
async def get_settings(request: Request) -> dict[str, Any]:
    require_role(request, 'admin')
    async with request.app.state.system_settings_lock:
        async with request.app.state.db.transaction() as tx:
            await require_current_role(request, 'admin', tx)
        return view(request.app)


@router.put('/settings')
async def update_settings(body: UpdateSettings, request: Request) -> dict[str, Any]:
    require_role(request, 'admin')
    value = Overlay.model_validate(body.model_dump(exclude={'expected_revision'}))
    async with request.app.state.system_settings_lock:
        async def persist() -> dict[str, Any]:
            async with request.app.state.db.transaction() as tx:
                actor = await require_current_role(request, 'admin', tx)
                if body.expected_revision != revision(request.app.state.system_overlay):
                    fail(409, 'settings_changed', 'Settings changed. Reload before saving.')
                if request.app.state.session_hours_locked and value.session_hours != request.app.state.system_overlay.session_hours:
                    fail(409, 'setting_environment_locked', 'Session length is controlled by INFERENCE_SESSION_HOURS.')
                await tx.execute("INSERT INTO documents(key,value_json) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=datetime('now')", (KEY, value.model_dump_json()))
                await tx.execute('INSERT INTO audit_events(actor,action,target) VALUES(?,?,?)',
                                 (actor['username'], 'admin.settings_updated', 'system'))
            # No await between committed state and publishing the policy.
            apply(request.app, value)
            return view(request.app)
        return await _settle(persist())
