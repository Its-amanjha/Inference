"""Application-append-only audit records; local administrators control storage."""
import json
from typing import Any

from fastapi import Request

SENSITIVE = ('password', 'secret', 'token', 'authorization', 'cookie', 'api_key', 'credential', 'recovery', 'code')


def redact(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return '[depth limit]'
    if isinstance(value, dict):
        return {str(k)[:128]: '[redacted]' if any(s in str(k).lower() for s in SENSITIVE) else redact(v, depth+1)
                for k, v in list(value.items())[:64]}
    if isinstance(value, (list, tuple)):
        return [redact(v, depth+1) for v in value[:64]]
    if isinstance(value, str):
        return value[:1024]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return '[unsupported value]'


async def record(request: Request, action: str, target: str, details: Any = None, outcome: str = 'success') -> None:
    user = getattr(request.state, 'user', None)
    await request.app.state.db.execute(
        'INSERT INTO audit_events(actor,action,target,details_json,outcome) VALUES(?,?,?,?,?)',
        (user['username'] if user else 'anonymous', action, target, json.dumps(redact(details or {})), outcome),
    )
