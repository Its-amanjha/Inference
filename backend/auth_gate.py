"""Session, authorization and request-origin boundaries shared by all modules."""
from datetime import datetime, timezone
import hashlib
import hmac
import secrets
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from starlette.requests import HTTPConnection

SESSION_COOKIE = 'inference_session'
CSRF_COOKIE = 'agd_csrf'
ROLES = {'viewer': 0, 'operator': 1, 'admin': 2}


def session_cookie(connection: HTTPConnection) -> str:
    """The raw session token the client holds, or an empty string."""
    return connection.cookies.get(SESSION_COOKIE) or ''


def fail(status: int, code: str, message: str) -> None:
    raise HTTPException(status_code=status, detail={'detail': message, 'code': code})


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def constant_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode('utf-8'),right.encode('utf-8'))


async def authenticate(connection: HTTPConnection) -> dict[str, Any] | None:
    raw = session_cookie(connection)
    if not raw or len(raw) > 128:
        return None
    db = connection.app.state.db
    row = await db.fetchone(
        'SELECT s.*,u.role,u.disabled FROM auth_sessions s JOIN users u ON u.username=s.username WHERE s.id_hash=?',
        (token_hash(raw),),
    )
    if row is None:
        return None
    try:
        expires = datetime.fromisoformat(row['expires_at'])
        expired = expires.tzinfo is None or expires <= datetime.now(timezone.utc)
    except (ValueError, TypeError):
        expired = True
    if expired or row['disabled'] or not row['csrf_secret']:
        await db.execute('DELETE FROM auth_sessions WHERE id_hash=?', (row['id_hash'],))
        return None
    return {'username': row['username'], 'role': row['role'], 'session_id': row['id_hash'],
            'csrf_secret': row['csrf_secret'], 'expires_at': row['expires_at']}


def require_role(request: HTTPConnection, role: str) -> dict[str, Any]:
    user = getattr(request.state, 'user', None)
    if user is None:
        fail(401, 'authentication_required', 'Sign in to continue.')
    if ROLES.get(user['role'], -1) < ROLES[role]:
        fail(403, 'permission_denied', 'Your role does not permit this action.')
    return user


def public_user(user: dict[str, Any] | None) -> dict[str, str] | None:
    return {'username': user['username'], 'role': user['role']} if user else None


async def require_current_role(request: HTTPConnection, role: str, tx: Any) -> dict[str, Any]:
    """Revalidate inside the caller's write transaction, after all awaited work."""
    actor=require_role(request,role)
    current=await tx.fetchone('SELECT u.role,u.disabled,s.expires_at FROM users u JOIN auth_sessions s ON s.username=u.username WHERE s.id_hash=? AND u.username=?',
                              (actor['session_id'],actor['username']))
    if not current or current['disabled']:
        fail(401,'session_revoked','Your session changed. Sign in again.')
    try:
        expires=datetime.fromisoformat(current['expires_at'])
        valid=expires.tzinfo is not None and expires>datetime.now(timezone.utc)
    except (ValueError,TypeError):
        valid=False
    if not valid:
        fail(401,'session_expired','Your session expired. Sign in again.')
    if ROLES.get(current['role'],-1)<ROLES[role]:
        fail(403,'permission_denied','Your role no longer permits this action.')
    return {**actor,'role':current['role']}


def csrf_token(user: dict[str, Any]) -> str:
    nonce = secrets.token_urlsafe(24)
    signature = hmac.new(user['csrf_secret'].encode(), f"{user['session_id']}:{nonce}".encode(), hashlib.sha256).hexdigest()
    return f'{nonce}.{signature}'


def valid_csrf(user: dict[str, Any], token: str) -> bool:
    if not token or len(token) > 256 or token.count('.') != 1:
        return False
    nonce, signature = token.split('.')
    expected = hmac.new(user['csrf_secret'].encode(), f"{user['session_id']}:{nonce}".encode(), hashlib.sha256).hexdigest()
    return bool(nonce) and constant_equal(signature, expected)


def check_csrf(request: Request) -> None:
    user = require_role(request, 'viewer')
    cookie = request.cookies.get(CSRF_COOKIE, '')
    header = request.headers.get('X-AGD-CSRF', '')
    if not constant_equal(cookie, header) or not valid_csrf(user, header):
        fail(403, 'csrf_failed', 'CSRF check failed. Refresh your session and retry once.')


def check_origin(connection: HTTPConnection, *, required: bool = False) -> None:
    origin = connection.headers.get('origin')
    if connection.headers.get('sec-fetch-site') == 'cross-site':
        fail(403, 'origin_rejected', 'Cross-site requests are not accepted.')
    if origin is None:
        if required:
            fail(403, 'origin_rejected', 'An Origin header is required.')
        return
    try:
        parsed = urlsplit(origin)
    except ValueError:
        fail(403, 'origin_rejected', 'Request origin is not allowed.')
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username:
        fail(403, 'origin_rejected', 'Request origin is not allowed.')
    scheme = {'ws': 'http', 'wss': 'https'}.get(connection.url.scheme, connection.url.scheme)
    expected = f'{scheme}://{connection.url.netloc}'
    allowed = connection.app.state.settings.allowed_origins or (expected,)
    if origin not in allowed:
        fail(403, 'origin_rejected', 'Request origin is not allowed.')
