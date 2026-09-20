"""Guarded, least-privilege access to configured n8n public APIs."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, aclosing
from typing import Any

import httpx
from fastapi import HTTPException
from starlette.requests import HTTPConnection

from backend.auth_gate import fail, require_current_role
from backend.config import VaultError
from backend.net import validate_url


_IDENTIFIER = r"[A-Za-z0-9_-]{1,128}"
# Workflow history versions are lowercase UUIDs, so action suffixes such as
# /activate can never be reached through the read-only history route.
_VERSION = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_READ_PATH = re.compile(
    rf"/(?:workflows|executions)(?:/{_IDENTIFIER})?\Z|/workflows/{_IDENTIFIER}/{_VERSION}\Z"
)
_MUTATION_PATHS = {
    "POST": re.compile(rf"/workflows/{_IDENTIFIER}/(?:activate|deactivate)\Z"),
    "DELETE": re.compile(rf"/executions/{_IDENTIFIER}\Z"),
}
_ALLOWED_KWARGS = frozenset({"params", "json"})


@asynccontextmanager
async def instance_guard(
    request: HTTPConnection, instance_id: str
) -> AsyncIterator[None]:
    """Serialize operations for one instance without blocking other targets."""

    if not isinstance(instance_id, str) or not instance_id:
        fail(400, "invalid_instance_id", "An explicit instance ID is required.")
    locks = request.app.state.instance_locks
    lock = locks.get(instance_id)
    if lock is None:
        lock = asyncio.Lock()
        locks[instance_id] = lock
    owner = asyncio.current_task()
    if getattr(lock, '_inference_owner', None) is owner:
        yield
        return
    async with lock:
        lock._inference_owner = owner
        try:
            yield
        finally:
            lock._inference_owner = None


def _operation(method: str, path: str) -> tuple[str, bool, str]:
    if not isinstance(method, str) or not isinstance(path, str):
        fail(400, "invalid_upstream_request", "The n8n operation is not permitted.")
    normalized = method.upper()
    if (normalized == 'GET' and (path == '/credentials' or re.fullmatch(rf'/credentials/schema/{_IDENTIFIER}', path))) or (normalized == 'POST' and path == '/credentials'):
        return normalized, normalized != 'GET', 'admin'
    if (normalized == 'POST' and path == '/workflows') or (normalized == 'PUT' and re.fullmatch(rf'/workflows/{_IDENTIFIER}', path)):
        return normalized, True, 'operator'
    if normalized == "GET" and _READ_PATH.fullmatch(path):
        return normalized, False, "viewer"
    pattern = _MUTATION_PATHS.get(normalized)
    if pattern is not None and pattern.fullmatch(path):
        return normalized, True, "operator"
    fail(400, "invalid_upstream_request", "The n8n operation is not permitted.")
    raise AssertionError("unreachable")


def _configured_instance(request: HTTPConnection, row: dict[str, Any]) -> tuple[str, str]:
    try:
        parsed = validate_url(row["url"])
        if parsed.userinfo or parsed.query or parsed.fragment:
            raise ValueError
        key = request.app.state.vault.decrypt(row["api_key_enc"])
        if not key or any(ord(character) < 32 or ord(character) == 127 for character in key):
            raise ValueError
    except (KeyError, TypeError, ValueError, UnicodeError, VaultError):
        fail(
            502,
            "instance_configuration_invalid",
            "The instance configuration is invalid. Update it before retrying.",
        )
    return str(parsed).rstrip("/"), key


def _upstream_status(status: int, mutation: bool) -> None:
    if status == 401:
        fail(502, "upstream_auth_failed", "The n8n instance rejected its API key.")
    if status == 403:
        fail(502, "upstream_forbidden", "The n8n instance denied this operation.")
    if status == 404:
        fail(404, "upstream_not_found", "The requested n8n resource was not found.")
    if status == 429:
        fail(503, "upstream_rate_limited", "The n8n instance is rate limiting requests.")
    if mutation and status in {400, 409, 422}:
        fail(status, "upstream_rejected", "The n8n instance rejected the operation.")
    if mutation and 500 <= status <= 599:
        fail(
            502,
            "upstream_outcome_unknown",
            "The n8n mutation outcome is unknown. Reconcile state before retrying.",
        )
    fail(502, "upstream_error", "The n8n instance returned an unexpected response.")


def _invalid_success(mutation: bool) -> None:
    if mutation:
        fail(
            502,
            "upstream_outcome_unknown",
            "The n8n mutation outcome is unknown. Reconcile state before retrying.",
        )
    fail(502, "upstream_invalid_response", "The n8n instance returned an invalid response.")


def _redact_echo(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, '[redacted]')
        return value
    if isinstance(value, list):
        return [_redact_echo(item, secrets) for item in value]
    if isinstance(value, dict):
        return {_redact_echo(key, secrets): _redact_echo(item, secrets) for key, item in value.items()}
    return value


class N8nClient:
    """Issue one explicitly targeted request through the verified app HTTP client."""

    MAX_RESPONSE_BYTES = 4 * 1024 * 1024
    TOTAL_TIMEOUT = 35.0

    def __init__(self, request: HTTPConnection) -> None:
        self._request = request

    async def _authorize(self, tx, required_role, instance_id, method, path):
        await require_current_role(self._request, required_role, tx)

    async def request(
        self,
        instance_id: str,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        method, mutation, required_role = _operation(method, path)
        return await self._send(instance_id, method, path, mutation, required_role, **kwargs)

    async def retry_execution(self, instance_id: str, execution_id: str) -> dict[str, Any]:
        """Retry with the saved workflow, for the assistant's claimed approval only.

        Keep retry outside the generic request allowlist. This explicit operation has no
        caller-controlled path/body/options and shares all authorization and egress checks.
        """
        if not isinstance(execution_id, str) or not re.fullmatch(_IDENTIFIER, execution_id):
            fail(400, 'invalid_upstream_request', 'A valid execution ID is required.')
        return await self._send(instance_id, 'POST', f'/executions/{execution_id}/retry', True, 'operator',
                                json={'loadWorkflow': True})

    async def _send(self, instance_id: str, method: str, path: str, mutation: bool,
                    required_role: str, **kwargs: Any) -> dict[str, Any]:
        if set(kwargs) - _ALLOWED_KWARGS:
            fail(
                400,
                "invalid_upstream_request",
                "Only query parameters and JSON bodies may be forwarded to n8n.",
            )

        async with instance_guard(self._request, instance_id):
            async with self._request.app.state.db.transaction() as tx:
                await self._authorize(tx, required_role, instance_id, method, path)
                row = await tx.fetchone(
                    "SELECT id,url,api_key_enc FROM instances WHERE id=?",
                    (instance_id,),
                )
            if row is None:
                fail(404, "instance_not_found", "The requested instance does not exist.")
            base_url, api_key = _configured_instance(self._request, row)
            url = f"{base_url}/api/v1{path}"

            async def perform() -> dict[str, Any]:
                headers = {
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    "X-N8N-API-KEY": api_key,
                }
                http = self._request.app.state.http
                # Construct independently of the shared client's cookie jar and auth.
                outbound = httpx.Request(method, url, headers=headers, **kwargs,
                                         extensions={'timeout': http.timeout.as_dict()})
                response = await http.send(outbound, stream=True, auth=None, follow_redirects=False)
                async with aclosing(response):
                    if not 200 <= response.status_code <= 299:
                        _upstream_status(response.status_code, mutation)
                    encoding = response.headers.get("content-encoding", "identity").lower()
                    if encoding not in {"", "identity"}:
                        _invalid_success(mutation)
                    length = response.headers.get("content-length")
                    if length is not None:
                        try:
                            if int(length) > self.MAX_RESPONSE_BYTES:
                                _invalid_success(mutation)
                        except ValueError:
                            _invalid_success(mutation)
                    raw = bytearray()
                    if response.is_stream_consumed:
                        if len(response.content) > self.MAX_RESPONSE_BYTES:
                            _invalid_success(mutation)
                        raw.extend(response.content)
                    else:
                        async for chunk in response.aiter_raw():
                            if len(raw) + len(chunk) > self.MAX_RESPONSE_BYTES:
                                _invalid_success(mutation)
                            raw.extend(chunk)
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
                    _invalid_success(mutation)
                if not isinstance(payload, dict):
                    _invalid_success(mutation)
                try:
                    return _redact_echo(payload, (api_key, row['api_key_enc']))
                except RecursionError:
                    _invalid_success(mutation)

            try:
                return await asyncio.wait_for(perform(), timeout=self.TOTAL_TIMEOUT)
            except HTTPException:
                raise
            except (asyncio.TimeoutError, httpx.TimeoutException):
                if mutation:
                    fail(
                        504,
                        "upstream_outcome_unknown",
                        "The n8n mutation timed out. Reconcile state before retrying.",
                    )
                fail(504, "upstream_timeout", "The n8n instance did not respond in time.")
            except httpx.RequestError:
                if mutation:
                    fail(
                        502,
                        "upstream_outcome_unknown",
                        "The n8n mutation outcome is unknown. Reconcile state before retrying.",
                    )
                fail(502, "upstream_unavailable", "The n8n instance is unavailable.")
        raise AssertionError("unreachable")
