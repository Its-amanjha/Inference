"""Password verification and single-use second factors."""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import secrets
import time
from typing import Any

from fastapi import Request, Response
import pyotp

from backend.auth_gate import CSRF_COOKIE, SESSION_COOKIE, constant_equal, csrf_token, fail, session_cookie, token_hash


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=32768, r=8, p=3, maxmem=64*1024*1024).hex()
    return f'scrypt:{salt}:{digest}'


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, salt, expected = encoded.split(':')
        if scheme != 'scrypt' or len(salt) != 32 or len(expected) != 128:
            return False
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=32768, r=8, p=3, maxmem=64*1024*1024).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


async def password_hash(request: Request, password: str) -> str:
    async with request.app.state.password_slots:
        return await asyncio.to_thread(hash_password, password)


async def password_matches(request: Request, password: str, encoded: str) -> bool:
    async with request.app.state.password_slots:
        return await asyncio.to_thread(verify_password, password, encoded)


def throttle(request: Request, operation: str, *, limit: int = 6, window: int = 60) -> None:
    ip = request.client.host if request.client else 'unknown'
    if not request.app.state.limiter.hit(f'{operation}:{ip}', limit=limit, window=window):
        fail(429, 'rate_limited', 'Too many attempts. Wait before trying again.')


async def consume_factor(tx: Any, vault: Any, user: dict[str, Any], code: str) -> bool:
    if not user['totp_secret_enc'] or not code:
        return False
    secret = vault.decrypt(user['totp_secret_enc'])
    counter = int(time.time()) // 30
    totp = pyotp.TOTP(secret)
    for step in (counter, counter-1, counter+1):
        if step > user['totp_last_counter'] and constant_equal(totp.at(step*30), code):
            await tx.execute('UPDATE users SET totp_last_counter=? WHERE username=?', (step,user['username']))
            return True
    recovery = await tx.fetchone('SELECT code_hash FROM recovery_codes WHERE username=? AND code_hash=?', (user['username'],token_hash(code)))
    if recovery:
        await tx.execute('DELETE FROM recovery_codes WHERE username=? AND code_hash=?', (user['username'],token_hash(code)))
        return True
    return False


async def new_session(request: Request, response: Response, user: dict[str, Any], tx: Any) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=request.app.state.settings.session_hours)
    raw = secrets.token_urlsafe(32)
    session = {'username':user['username'],'role':user['role'],'session_id':token_hash(raw),
               'csrf_secret':secrets.token_urlsafe(32),'expires_at':expires.isoformat()}
    previous = session_cookie(request)
    if previous:
        await tx.execute('DELETE FROM auth_sessions WHERE id_hash=?',(token_hash(previous),))
    await tx.execute('DELETE FROM auth_sessions WHERE expires_at<=?',(now.isoformat(),))
    await tx.execute('INSERT INTO auth_sessions(id_hash,username,created_at,expires_at,last_seen,user_agent,ip,csrf_secret) VALUES(?,?,?,?,?,?,?,?)',
                     (session['session_id'],user['username'],now.isoformat(),expires.isoformat(),now.isoformat(),
                      request.headers.get('user-agent','')[:256],request.client.host if request.client else '',session['csrf_secret']))
    max_age=int((expires-now).total_seconds())
    response.set_cookie(SESSION_COOKIE, raw, max_age=max_age, httponly=True, secure=request.app.state.settings.secure_cookies, samesite='strict', path='/')
    set_csrf_cookie(request,response,session,max_age=max_age)
    request.state.user=session
    return session


def set_csrf_cookie(request: Request, response: Response, session: dict[str, Any], *, max_age: int | None = None) -> None:
    if max_age is None:
        max_age=max(0,int((datetime.fromisoformat(session['expires_at'])-datetime.now(timezone.utc)).total_seconds()))
    response.set_cookie(CSRF_COOKIE,csrf_token(session),max_age=max_age,httponly=False,
                        secure=request.app.state.settings.secure_cookies,samesite='strict',path='/')
