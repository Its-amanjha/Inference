"""Privilege revocation must win over an in-flight administrator mutation."""

import asyncio

import httpx
import pytest

from backend.main import create_app
from backend.modules.admin import router as admin_router


@pytest.mark.parametrize("revocation", ["disable", "demote", "logout"])
async def test_in_flight_admin_creation_rechecks_role_and_session(tmp_path, monkeypatch, revocation):
    app = create_app(data_dir=tmp_path)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 51000))
        async with (
            httpx.AsyncClient(transport=transport, base_url="https://localhost") as owner,
            httpx.AsyncClient(transport=transport, base_url="https://localhost") as revoker,
        ):
            setup = await owner.post("/api/auth/setup", json={
                "username": "owner", "password": "a long unique password 2026",
            })
            assert setup.status_code == 200, setup.text
            owner_headers = {"X-AGD-CSRF": owner.cookies["agd_csrf"]}
            created = await owner.post("/api/admin/users", headers=owner_headers, json={
                "username": "revoker", "password": "another long password 2026", "role": "admin",
            })
            assert created.status_code == 200, created.text
            login = await revoker.post("/api/auth/login", json={
                "username": "revoker", "password": "another long password 2026",
            })
            assert login.status_code == 200, login.text
            revoker_headers = {"X-AGD-CSRF": revoker.cookies["agd_csrf"]}

            hashing_started = asyncio.Event()
            allow_hashing = asyncio.Event()
            original_hash = admin_router.password_hash

            async def paused_hash(request, password):
                # Keep real password hashing; control only its scheduling boundary.
                hashing_started.set()
                await allow_hashing.wait()
                return await original_hash(request, password)

            monkeypatch.setattr(admin_router, "password_hash", paused_hash)
            pending = asyncio.create_task(owner.post("/api/admin/users", headers=owner_headers, json={
                "username": "after_revocation", "password": "new admin password 2026", "role": "admin",
            }))
            try:
                await asyncio.wait_for(hashing_started.wait(), timeout=5)
                if revocation == "logout":
                    revoked = await owner.post("/api/auth/logout", headers=owner_headers)
                else:
                    change = {"disabled": True} if revocation == "disable" else {"role": "viewer"}
                    revoked = await revoker.patch("/api/admin/users/owner", headers=revoker_headers, json=change)
                assert revoked.status_code == 200, revoked.text
            finally:
                allow_hashing.set()
                response = await asyncio.wait_for(pending, timeout=5)

            assert response.status_code in (401, 403), response.text
            assert await app.state.db.fetchone(
                "SELECT username FROM users WHERE username='after_revocation'",
            ) is None
            assert (await owner.get("/api/auth/status")).json()["user"] is None


async def test_malformed_origin_is_rejected_without_an_internal_error(tmp_path):
    app = create_app(data_dir=tmp_path)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 51000))
        async with httpx.AsyncClient(transport=transport, base_url="https://localhost") as client:
            response = await client.post("/api/auth/login", headers={"Origin": "http://["}, json={
                "username": "owner", "password": "a long unique password 2026",
            })
            assert response.status_code == 403, response.text
            assert response.json()["code"] == "origin_rejected"
            assert await app.state.db.fetchall("SELECT * FROM auth_sessions") == []


async def test_unicode_totp_is_rejected_without_activating_enrollment(tmp_path):
    app = create_app(data_dir=tmp_path)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 51000))
        async with httpx.AsyncClient(transport=transport, base_url="https://localhost") as client:
            setup = await client.post("/api/auth/setup", json={
                "username": "owner", "password": "a long unique password 2026",
            })
            assert setup.status_code == 200, setup.text
            headers = {"X-AGD-CSRF": client.cookies["agd_csrf"]}
            enrolled = await client.post("/api/auth/totp/enroll", headers=headers, json={
                "password": "a long unique password 2026",
            })
            assert enrolled.status_code == 200, enrolled.text
            response = await client.post("/api/auth/totp/confirm", headers=headers, json={"code": "é" * 6})
            assert response.status_code == 400, response.text
            assert response.json()["code"] == "invalid_code"
            account = await app.state.db.fetchone("SELECT totp_secret_enc,totp_pending_enc FROM users WHERE username='owner'")
            assert account["totp_secret_enc"] is None
            assert account["totp_pending_enc"] is not None
            assert await app.state.db.fetchall("SELECT * FROM recovery_codes") == []
