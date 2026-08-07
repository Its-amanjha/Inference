import asyncio
import json
import sqlite3
from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from backend import audit
from backend.auth_gate import fail, require_role, require_current_role
from backend.modules.auth.service import password_hash

router=APIRouter()


class UserCreate(BaseModel):
    model_config=ConfigDict(extra='forbid')
    username: str=Field(min_length=1,max_length=64,pattern=r'^[A-Za-z0-9_.-]+$')
    password: str=Field(min_length=14,max_length=256)
    role: Literal['viewer','operator','admin']='viewer'


class UserUpdate(BaseModel):
    model_config=ConfigDict(extra='forbid')
    password: str | None=Field(default=None,min_length=14,max_length=256)
    role: Literal['viewer','operator','admin'] | None=None
    disabled: bool | None=None


class SecretInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name: str=Field(min_length=1,max_length=128,pattern=r'^[A-Za-z_][A-Za-z0-9_]*$')
    value: str | dict[str, Any]


class Preferences(BaseModel):
    model_config=ConfigDict(extra='forbid')
    theme: Literal['dark','light','n8n']='light'
    page_size: int=Field(default=25,ge=10,le=100)
    error_notifications: bool=True


@router.get('/users')
async def users(request: Request) -> dict:
    require_role(request,'admin')
    rows=await request.app.state.db.fetchall('SELECT username,role,disabled,created_at,totp_secret_enc IS NOT NULL AS totp_enabled FROM users ORDER BY username')
    return {'data':rows}


@router.post('/users')
async def create_user(body: UserCreate, request: Request) -> dict:
    require_role(request,'admin')
    hashed=await password_hash(request,body.password)
    try:
        async with request.app.state.db.transaction() as tx:
            await require_current_role(request,'admin',tx)
            await tx.execute('INSERT INTO users(username,password_hash,role) VALUES(?,?,?)',(body.username,hashed,body.role))
    except sqlite3.IntegrityError:
        fail(409,'user_exists','That username already exists.')
    await audit.record(request,'admin.user_created',body.username,{'role':body.role})
    return {'username':body.username,'role':body.role}


@router.patch('/users/{username}')
async def update_user(username: str, body: UserUpdate, request: Request) -> dict:
    require_role(request,'admin')
    changes=body.model_dump(exclude_none=True)
    if not changes:
        fail(400,'empty_update','Supply a role, disabled status or new password.')
    if 'password' in changes:
        changes['password_hash']=await password_hash(request,changes.pop('password'))
    async with request.app.state.db.transaction() as tx:
        await require_current_role(request,'admin',tx)
        user=await tx.fetchone('SELECT * FROM users WHERE username=?',(username,))
        if user is None:
            fail(404,'not_found','User was not found.')
        if user['role']=='admin' and not user['disabled'] and (changes.get('role','admin')!='admin' or changes.get('disabled',False)):
            count=await tx.fetchone("SELECT COUNT(*) AS n FROM users WHERE role='admin' AND disabled=0")
            if count['n']<=1:
                fail(409,'last_admin','Keep at least one enabled administrator.')
        # Column names originate only from the closed Pydantic schema above.
        assignment=','.join(f'{field}=?' for field in changes)
        await tx.execute(f'UPDATE users SET {assignment} WHERE username=?',(*changes.values(),username))
        await tx.execute('DELETE FROM auth_sessions WHERE username=?',(username,))
    await audit.record(request,'admin.user_updated',username,{'fields':list(changes)})
    return {'ok':True,'sessions_revoked':True}


@router.get('/secrets')
async def secrets_list(request: Request) -> dict:
    require_role(request,'admin')
    return {'data':await asyncio.to_thread(request.app.state.vault.list)}


@router.post('/secrets')
async def secret_set(body: SecretInput, request: Request) -> dict:
    require_role(request,'admin')
    if len(json.dumps(body.value,ensure_ascii=False).encode())>64*1024:
        fail(413,'secret_too_large','A secret must be at most 64 KiB.')
    async with request.app.state.db.transaction() as tx:
        await require_current_role(request,'admin',tx)
        await asyncio.to_thread(request.app.state.vault.set,body.name,body.value)
    await audit.record(request,'admin.secret_saved',body.name)
    return {'name':body.name}


@router.delete('/secrets/{name}')
async def secret_delete(name: str, request: Request) -> dict:
    require_role(request,'admin')
    try:
        async with request.app.state.db.transaction() as tx:
            await require_current_role(request,'admin',tx)
            await asyncio.to_thread(request.app.state.vault.delete,name)
    except KeyError:
        fail(404,'not_found','Secret was not found.')
    await audit.record(request,'admin.secret_deleted',name)
    return {'ok':True}


@router.get('/audit')
async def audit_list(request: Request, before: int | None=Query(default=None,ge=1), limit: int=Query(default=50,ge=1,le=200)) -> dict:
    require_role(request,'admin')
    rows=await request.app.state.db.fetchall('SELECT * FROM audit_events WHERE id<? ORDER BY id DESC LIMIT ?',(before or 9223372036854775807,limit+1))
    return {'data':rows[:limit],'next_cursor':rows[limit-1]['id'] if len(rows)>limit else None}


@router.get('/preferences')
async def preferences(request: Request) -> dict:
    user=require_role(request,'viewer')
    row=await request.app.state.db.fetchone('SELECT value_json FROM documents WHERE key=?',(f"preferences:{user['username']}",))
    return Preferences.model_validate_json(row['value_json']).model_dump() if row else request.app.state.system_overlay.default_preferences.model_dump()


@router.put('/preferences')
async def set_preferences(body: Preferences, request: Request) -> dict:
    user=require_role(request,'viewer')
    async with request.app.state.db.transaction() as tx:
        await require_current_role(request,'viewer',tx)
        await tx.execute("INSERT INTO documents(key,value_json) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=datetime('now')",(f"preferences:{user['username']}",body.model_dump_json()))
    await audit.record(request,'admin.preferences_updated',user['username'])
    return body.model_dump()


from .settings import router as settings_router
router.include_router(settings_router)
from .templates import router as templates_router
router.include_router(templates_router)
