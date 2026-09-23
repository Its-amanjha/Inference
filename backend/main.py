"""Application factory. Run one worker; persistent resources belong to lifespan."""
import asyncio
from contextlib import asynccontextmanager
import logging
from pathlib import Path
from typing import Any
from weakref import WeakValueDictionary

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from backend.auth_gate import authenticate, check_csrf, check_origin, fail
from backend.config import Settings, Vault
from backend.database import Database
from backend.modules import register_modules
from backend.net import create_http_client
from backend.ratelimit import RateLimiter
from backend.websocket import ConnectionManager

logger=logging.getLogger('inference')
PUBLIC_API={'/api/auth/status','/api/auth/login','/api/auth/setup'}
MUTATING={'POST','PUT','PATCH','DELETE'}


def error_response(exc: HTTPException) -> JSONResponse:
    content=exc.detail if isinstance(exc.detail,dict) else {'detail':str(exc.detail),'code':'request_failed'}
    return JSONResponse(status_code=exc.status_code,content=content,headers=exc.headers)


class SecurityMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app=app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope['type']!='http':
            await self.app(scope,receive,send)
            return
        request=Request(scope,receive)
        is_api=request.url.path=='/api' or request.url.path.startswith('/api/')
        machine=request.method=='POST' and request.url.path=='/api/otel/v1/traces'

        async def secure_send(message: dict[str,Any]) -> None:
            if message['type']=='http.response.start':
                headers=list(message.get('headers',[]))
                headers.extend([(b'x-content-type-options',b'nosniff'),(b'x-frame-options',b'DENY'),
                                (b'referrer-policy',b'no-referrer'),
                                (b'content-security-policy',b"default-src 'self'; script-src 'self' https://cdnjs.cloudflare.com; style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; font-src 'self' https://cdnjs.cloudflare.com; worker-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'self'; form-action 'self'")])
                # Raw, unversioned ES modules and theme JSON must not drift across releases.
                headers.append((b'cache-control',b'no-store'))
                message={**message,'headers':headers}
            await send(message)

        try:
            request.state.user=await authenticate(request) if is_api and not machine else None
            if is_api and not machine and request.url.path not in PUBLIC_API and not request.state.user:
                fail(401,'authentication_required','Sign in to continue.')
            if is_api and not machine and request.method in MUTATING:
                check_origin(request)
                if request.url.path not in PUBLIC_API:
                    check_csrf(request)
                if request.headers.get('content-encoding','identity')!='identity':
                    fail(415,'unsupported_encoding','Compressed browser API requests are not supported.')
                limit=request.app.state.settings.max_request_bytes
                async def read_body() -> bytes:
                    chunks=[]
                    total=0
                    async for chunk in request.stream():
                        total+=len(chunk)
                        if total>limit:
                            fail(413,'body_too_large','Request exceeds the configured size limit.')
                        chunks.append(chunk)
                    return b''.join(chunks)
                try:
                    body=await asyncio.wait_for(read_body(),timeout=15)
                except asyncio.TimeoutError:
                    fail(408,'body_timeout','Request body took too long to arrive.')
                if request.url.path not in PUBLIC_API:
                    request.state.user=await authenticate(request)
                    check_csrf(request)
                delivered=False

                async def buffered_receive() -> dict[str,Any]:
                    nonlocal delivered
                    if not delivered:
                        delivered=True
                        return {'type':'http.request','body':body,'more_body':False}
                    return await receive()

                await self.app(scope,buffered_receive,secure_send)
            else:
                await self.app(scope,receive,secure_send)
        except HTTPException as exc:
            await error_response(exc)(scope,receive,secure_send)


def create_app(data_dir: Path | None=None, settings: Settings | None=None) -> FastAPI:
    config=settings or Settings.from_env(data_dir=data_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.vault=await asyncio.to_thread(Vault,config.data_dir)
        app.state.db=Database(config.data_dir/'dashboard.db')
        app.state.limiter=RateLimiter()
        app.state.password_slots=asyncio.Semaphore(4)
        app.state.hub=ConnectionManager()
        app.state.instance_locks=WeakValueDictionary()
        app.state.ingest_slots=asyncio.Semaphore(4)
        await app.state.db.open()
        try:
            from backend.modules.admin.settings import initialize as initialize_admin_settings
            await initialize_admin_settings(app)
            from backend.modules.assistant.approvals import initialize as initialize_assistant
            await initialize_assistant(app)
            from backend.modules.docker_mgr.deployer import initialize as initialize_docker
            from backend.modules.docker_mgr.client import open_docker
            await initialize_docker(app)
            from backend.modules.notes.router import initialize as initialize_notes
            initialize_notes(app)
            # Skills ship with the release: sync the library from disk once the schema is ready.
            from backend.modules.skills.loader import initialize as initialize_skills
            await initialize_skills(app)
            app.state.backup_lock = asyncio.Lock()
            from backend.modules.backups.schedules import initialize as initialize_backups, maintenance as backup_maintenance
            await initialize_backups(app)
            app.state.docker = await open_docker(config.docker_socket)
            # Interrupted remote writes require reconciliation, never startup retries.
            await app.state.db.execute("UPDATE promotion_runs SET status='uncertain' WHERE status IN ('validating','provisioning','writing','activating','restoring')")
            async with create_http_client(config.allow_cidrs) as client:
                app.state.http=client
                from backend.modules.observability.router import cleanup, maintenance
                from backend.modules.observability.pricebook import seed_prices, maintenance as price_maintenance
                from backend.modules.observability.polling import maintenance as polling_maintenance
                await cleanup(app.state.db)
                await seed_prices(app.state.db)
                telemetry_task=asyncio.create_task(maintenance(app))
                price_task=asyncio.create_task(price_maintenance(app))
                polling_task=asyncio.create_task(polling_maintenance(app))
                backup_task=asyncio.create_task(backup_maintenance(app))
                try:
                    yield
                finally:
                    telemetry_task.cancel()
                    price_task.cancel()
                    polling_task.cancel()
                    backup_task.cancel()
                    await asyncio.gather(telemetry_task,price_task,polling_task,backup_task,return_exceptions=True)
                    await app.state.hub.close()
        finally:
            from backend.modules.docker_mgr.client import close_docker
            try:
                await close_docker(getattr(app.state, 'docker', None))
            finally:
                await app.state.db.close()

    app=FastAPI(title='Inference',version='0.1.0',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.settings=config
    app.add_middleware(SecurityMiddleware)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return error_response(exc)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic inputs can contain credentials; never return the input payload.
        return JSONResponse(status_code=422,content={'detail':'Request fields are invalid.','code':'validation_failed',
                            'fields':['.'.join(str(p) for p in item['loc']) for item in exc.errors()]})

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception) -> JSONResponse:
        logger.error('Unhandled request error: %s',type(exc).__name__)
        return JSONResponse(status_code=500,content={'detail':'The operation failed. Check service health before retrying.','code':'internal_error'})

    @app.get('/healthz')
    async def health(request: Request) -> dict[str,str]:
        await request.app.state.db.fetchone('SELECT 1')
        return {'status':'ok'}

    @app.websocket('/ws')
    async def notifications(ws: WebSocket) -> None:
        await ws.app.state.hub.handle(ws)

    app.state.manifests=register_modules(app)
    if config.frontend_dir.is_dir():
        app.mount('/',StaticFiles(directory=config.frontend_dir,html=True),name='frontend')
    return app
