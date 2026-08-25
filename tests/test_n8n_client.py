from __future__ import annotations

import asyncio
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from backend.config import Vault
from backend.database import Database
from backend.modules.n8n_proxy.client import N8nClient, instance_guard


Handler = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]


async def test_upstream_cookies_are_never_forwarded(tmp_path):
    seen=[]
    def handler(request):
        seen.append(request)
        return httpx.Response(200,json={},headers={'Set-Cookie':'session=remote-secret; Path=/'})
    async with client_environment(tmp_path,handler) as (client,request,db,http):
        await client.request('instance-one','GET','/workflows')
        await client.request('instance-one','GET','/executions')
    assert all('cookie' not in request.headers for request in seen)


async def test_success_payload_cannot_echo_instance_secrets(tmp_path):
    def handler(request):
        return httpx.Response(200,json={'data':[{'name':'echo key-one'}]})
    async with client_environment(tmp_path,handler) as (client,request,db,http):
        result=await client.request('instance-one','GET','/workflows')
    assert 'key-one' not in str(result)


@asynccontextmanager
async def client_environment(
    tmp_path: Path,
    handler: Handler,
    *,
    role: str = "operator",
    api_key: str = "key-one",
    base_url: str = "https://one.example/n8n",
) -> AsyncIterator[tuple[N8nClient, Request, Database, httpx.AsyncClient]]:
    db = Database(tmp_path / "dashboard.db")
    await db.open()
    vault = Vault(tmp_path)
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    await db.execute(
        "INSERT INTO users(username,password_hash,role) VALUES (?,?,?)",
        ("alice", "unused", role),
    )
    await db.execute(
        "INSERT INTO auth_sessions(id_hash,username,created_at,expires_at,last_seen,csrf_secret) "
        "VALUES (?,?,?,?,?,?)",
        ("session-hash", "alice", expires, expires, expires, "csrf"),
    )
    await db.execute(
        "INSERT INTO instances(id,name,url,api_key_enc) VALUES (?,?,?,?)",
        ("instance-one", "One", base_url, vault.encrypt(api_key)),
    )
    outbound = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = FastAPI()
    app.state.db = db
    app.state.vault = vault
    app.state.http = outbound
    app.state.instance_locks = weakref.WeakValueDictionary()
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "app": app,
        }
    )
    request.state.user = {
        "username": "alice",
        "role": role,
        "session_id": "session-hash",
    }
    try:
        yield N8nClient(request), request, db, outbound
    finally:
        await outbound.aclose()
        await db.close()


def assert_api_error(error: HTTPException, status: int, code: str) -> None:
    assert error.status_code == status
    assert error.detail["code"] == code
    assert "key-one" not in str(error.detail)


@pytest.mark.asyncio
async def test_request_uses_fixed_prefix_subpath_and_request_local_api_key(
    tmp_path: Path,
) -> None:
    # Catches losing a stored base subpath or leaking auth into shared client state.
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [{"id": "wf"}]})

    async with client_environment(tmp_path, handler) as (client, _request, _db, http):
        result = await client.request(
            "instance-one", "GET", "/workflows", params={"limit": 1}
        )

        assert result == {"data": [{"id": "wf"}]}
        assert len(seen) == 1
        assert str(seen[0].url) == "https://one.example/n8n/api/v1/workflows?limit=1"
        assert seen[0].headers["x-n8n-api-key"] == "key-one"
        assert seen[0].headers["accept-encoding"] == "identity"
        assert "cookie" not in seen[0].headers
        assert "x-n8n-api-key" not in http.headers


@pytest.mark.asyncio
async def test_concurrent_instances_keep_url_and_key_snapshots_paired(tmp_path: Path) -> None:
    # Catches global target selection or shared-header mutation across concurrent calls.
    seen: set[tuple[str, str]] = set()

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0)
        seen.add((request.url.host, request.headers["x-n8n-api-key"]))
        return httpx.Response(200, json={"id": request.url.host})

    async with client_environment(tmp_path, handler) as (client, _request, db, _http):
        vault = client._request.app.state.vault
        await db.execute(
            "INSERT INTO instances(id,name,url,api_key_enc) VALUES (?,?,?,?)",
            ("instance-two", "Two", "https://two.example/root", vault.encrypt("key-two")),
        )
        await asyncio.gather(
            client.request("instance-one", "GET", "/workflows/wf"),
            client.request("instance-two", "GET", "/workflows/wf"),
        )

    assert seen == {("one.example", "key-one"), ("two.example", "key-two")}


@pytest.mark.asyncio
async def test_invalid_route_and_override_kwargs_are_rejected_before_io(tmp_path: Path) -> None:
    # Catches turning the service into an arbitrary authenticated n8n proxy.
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    async with client_environment(tmp_path, handler) as (client, _request, _db, _http):
        for method, path, kwargs in (
            ("GET", "/users", {}),
            ("POST", "/workflows/wf", {"json": {}}),
            ("GET", "/workflows", {"headers": {"X-N8N-API-KEY": "override"}}),
            ("GET", "/workflows?limit=1", {}),
        ):
            with pytest.raises(HTTPException) as raised:
                await client.request("instance-one", method, path, **kwargs)
            assert_api_error(raised.value, 400, "invalid_upstream_request")
    assert calls == 0


@pytest.mark.asyncio
async def test_version_history_read_is_allowed_but_actions_stay_blocked(tmp_path: Path) -> None:
    # Catches widening the history route into activation, archive, delete, retry or stop.
    version = "7c1e2d3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f"
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"versionId": version, "nodes": [], "connections": {}})

    async with client_environment(tmp_path, handler, role="viewer") as (client, _request, _db, _http):
        result = await client.request("instance-one", "GET", f"/workflows/wf/{version}")
        assert result["versionId"] == version
        for method, path in (
            ("GET", "/workflows/wf/activate"),
            ("GET", "/workflows/wf/deactivate"),
            ("GET", f"/workflows/wf/{version.upper()}"),
            ("GET", f"/workflows/wf/{version}/activate"),
            ("GET", f"/executions/ex/{version}"),
            ("PUT", f"/workflows/wf/{version}"),
            ("POST", f"/workflows/wf/{version}"),
            ("DELETE", "/workflows/wf"),
            ("DELETE", f"/workflows/wf/{version}"),
            ("POST", "/workflows/wf/archive"),
            ("POST", "/workflows/wf/unarchive"),
            ("POST", "/executions/ex/retry"),
            ("POST", "/executions/ex/stop"),
        ):
            with pytest.raises(HTTPException) as raised:
                await client.request("instance-one", method, path)
            assert_api_error(raised.value, 400, "invalid_upstream_request")
    assert seen == [("GET", f"/n8n/api/v1/workflows/wf/{version}")]


@pytest.mark.asyncio
async def test_live_role_is_rechecked_before_mutation_io(tmp_path: Path) -> None:
    # Catches authorizing from stale request.state after an operator is downgraded.
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    async with client_environment(tmp_path, handler) as (client, _request, db, _http):
        await db.execute("UPDATE users SET role='viewer' WHERE username='alice'")
        with pytest.raises(HTTPException) as raised:
            await client.request(
                "instance-one", "POST", "/workflows/wf/activate", json={}
            )

    assert_api_error(raised.value, 403, "permission_denied")
    assert calls == 0


@pytest.mark.asyncio
async def test_missing_instance_and_corrupt_key_fail_without_outbound_io(
    tmp_path: Path,
) -> None:
    # Catches outbound attempts with deleted targets or header-injection key material.
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    async with client_environment(tmp_path, handler) as (client, _request, db, _http):
        with pytest.raises(HTTPException) as missing:
            await client.request("deleted", "GET", "/workflows")
        assert_api_error(missing.value, 404, "instance_not_found")

        vault = client._request.app.state.vault
        await db.execute(
            "UPDATE instances SET api_key_enc=? WHERE id='instance-one'",
            (vault.encrypt("bad\r\nX-Injected: yes"),),
        )
        with pytest.raises(HTTPException) as corrupt:
            await client.request("instance-one", "GET", "/workflows")
        assert_api_error(corrupt.value, 502, "instance_configuration_invalid")
    assert calls == 0


@pytest.mark.parametrize(
    ("upstream", "method", "expected_status", "expected_code"),
    [
        (401, "GET", 502, "upstream_auth_failed"),
        (403, "GET", 502, "upstream_forbidden"),
        (404, "GET", 404, "upstream_not_found"),
        (429, "GET", 503, "upstream_rate_limited"),
        (500, "GET", 502, "upstream_error"),
        (400, "POST", 400, "upstream_rejected"),
        (409, "POST", 409, "upstream_rejected"),
        (422, "POST", 422, "upstream_rejected"),
        (500, "POST", 502, "upstream_outcome_unknown"),
    ],
)
@pytest.mark.asyncio
async def test_upstream_statuses_are_mapped_without_forwarding_error_bodies(
    tmp_path: Path,
    upstream: int,
    method: str,
    expected_status: int,
    expected_code: str,
) -> None:
    # Catches leaking upstream bodies or treating uncertain 5xx mutations as retryable.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            upstream,
            text="secret upstream diagnostic key-one",
            headers={"Set-Cookie": "n8n=secret", "Location": "https://internal/"},
        )

    path = "/workflows" if method == "GET" else "/workflows/wf/activate"
    async with client_environment(tmp_path, handler) as (client, _request, _db, _http):
        with pytest.raises(HTTPException) as raised:
            await client.request("instance-one", method, path)

    assert_api_error(raised.value, expected_status, expected_code)
    assert "diagnostic" not in str(raised.value.detail)
    assert "internal" not in str(raised.value.detail)


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.asyncio
async def test_timeout_distinguishes_read_failure_from_uncertain_mutation(
    tmp_path: Path,
    method: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Catches suggesting that a timed-out remote mutation is safe to retry.
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        return httpx.Response(200, json={})

    path = "/workflows" if method == "GET" else "/workflows/wf/activate"
    async with client_environment(tmp_path, handler) as (client, _request, _db, _http):
        monkeypatch.setattr(client, "TOTAL_TIMEOUT", 0.01)
        with pytest.raises(HTTPException) as raised:
            await client.request("instance-one", method, path)

    expected = "upstream_timeout" if method == "GET" else "upstream_outcome_unknown"
    assert_api_error(raised.value, 504, expected)


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.asyncio
async def test_success_must_be_identity_encoded_bounded_json_object(
    tmp_path: Path,
    method: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Catches decompression bombs and ambiguous successful mutation responses.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"12345")

    path = "/workflows" if method == "GET" else "/workflows/wf/activate"
    async with client_environment(tmp_path, handler) as (client, _request, _db, _http):
        monkeypatch.setattr(client, "MAX_RESPONSE_BYTES", 4)
        with pytest.raises(HTTPException) as raised:
            await client.request("instance-one", method, path)

    expected = (
        "upstream_invalid_response" if method == "GET" else "upstream_outcome_unknown"
    )
    assert_api_error(raised.value, 502, expected)


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.asyncio
async def test_successful_json_must_be_an_object(tmp_path: Path, method: str) -> None:
    # Catches accepting scalar/list success payloads that callers cannot safely merge.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": "unexpected-envelope"}])

    path = "/workflows" if method == "GET" else "/workflows/wf/activate"
    async with client_environment(tmp_path, handler) as (client, _request, _db, _http):
        with pytest.raises(HTTPException) as raised:
            await client.request("instance-one", method, path)

    expected = (
        "upstream_invalid_response" if method == "GET" else "upstream_outcome_unknown"
    )
    assert_api_error(raised.value, 502, expected)


@pytest.mark.asyncio
async def test_instance_guard_serializes_same_instance_but_not_other_instances(
    tmp_path: Path,
) -> None:
    # Catches one global lock or separate locks for the same configuration target.
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    async with client_environment(tmp_path, handler) as (_client, request, _db, _http):
        entered = asyncio.Event()
        release = asyncio.Event()
        order: list[str] = []

        async def first() -> None:
            async with instance_guard(request, "instance-one"):
                order.append("first")
                entered.set()
                await release.wait()

        async def same() -> None:
            await entered.wait()
            async with instance_guard(request, "instance-one"):
                order.append("same")

        async def other() -> None:
            await entered.wait()
            async with instance_guard(request, "instance-two"):
                order.append("other")
                release.set()

        await asyncio.gather(first(), same(), other())

    assert order == ["first", "other", "same"]
