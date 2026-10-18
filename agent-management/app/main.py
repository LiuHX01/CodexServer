import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .adapters.base import ProviderError
from .adapters.codex import AdapterRegistry
from .api import router
from .config import Settings
from .database import Database
from .integrations.container_cloud import (
    ContainerCloudWorker,
    ContainerCloudWorkerConfig,
)
from .integrations.container_cloud.worker import run_worker
from .logging_config import configure_logging
from .schemas import Login
from .security import Security, principal
from .workers.runtime import Worker

STATIC = Path(__file__).parent / "static"
logger = logging.getLogger(__name__)


def container_cloud_config(settings):
    if not settings.container_cloud_endpoint or not settings.container_cloud_token:
        return None
    management_token = settings.container_cloud_management_token or settings.admin_token
    return ContainerCloudWorkerConfig(
        endpoint=settings.container_cloud_endpoint,
        token=settings.container_cloud_token,
        management_token=management_token,
        management_url=settings.container_cloud_management_url,
        reconnect_initial_seconds=settings.container_cloud_reconnect_initial_seconds,
        reconnect_max_seconds=settings.container_cloud_reconnect_max_seconds,
        connect_timeout_seconds=settings.container_cloud_connect_timeout_seconds,
        management_timeout_seconds=settings.container_cloud_management_timeout_seconds,
        local_mode=settings.container_cloud_local_mode,
        preset_dir=settings.container_cloud_preset_dir,
        result_poll_seconds=settings.container_cloud_result_poll_seconds,
        result_timeout_seconds=settings.container_cloud_result_timeout_seconds,
    )


def create_app(settings=None, database=None, adapters=None):
    @asynccontextmanager
    async def lifespan(app):
        config = settings or Settings()
        db = database or Database(config.database_url)
        security = Security(config)
        registry = adapters or AdapterRegistry(security)
        worker = Worker(db, registry, config)
        app.state.settings, app.state.db = config, db
        app.state.security, app.state.adapters, app.state.worker = security, registry, worker
        cloud_worker = None
        cloud_task = None
        if config.embedded_worker:
            await worker.start()
        if config.container_cloud_endpoint or config.container_cloud_token:
            cloud_config = container_cloud_config(config)
            if cloud_config is None:
                logger.error("event=container_cloud_disabled reason=endpoint_and_token_required")
            else:
                cloud_worker = ContainerCloudWorker(cloud_config)
                cloud_task = asyncio.create_task(
                    run_worker(cloud_worker), name="container-cloud-worker"
                )
                logger.info("event=container_cloud_worker_started")
        try:
            yield
        finally:
            if cloud_worker is not None:
                cloud_worker.stop()
            if cloud_task is not None:
                try:
                    with suppress(asyncio.CancelledError):
                        await asyncio.wait_for(cloud_task, timeout=5)
                except TimeoutError:
                    cloud_task.cancel()
                    await asyncio.gather(cloud_task, return_exceptions=True)
                    logger.warning("event=container_cloud_worker_shutdown_timeout")
            await worker.close()
            await db.close()

    app = FastAPI(
        title="AgentRelay", version="0.1.0", lifespan=lifespan, docs_url="/api-docs", redoc_url=None
    )

    @app.middleware("http")
    async def no_cache_static_assets(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/assets/"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    app.include_router(router)

    @app.exception_handler(ProviderError)
    async def provider_error(request, exc):
        return JSONResponse(status_code=502, content={"detail": exc.message})

    @app.exception_handler(httpx.HTTPError)
    async def connection_error(request, exc):
        return JSONResponse(status_code=502, content={"detail": "无法联系执行端，请检查连接状态"})

    @app.get("/healthz")
    async def health():
        from sqlalchemy import text

        async with app.state.db.transaction() as db:
            await db.execute(text("SELECT 1"))
        return {"status": "ok"}

    @app.post("/auth/login")
    async def login(body: Login, request: Request):
        # Check login origin too; another site must not switch a user's identity.
        from urllib.parse import urlsplit

        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.headers.get("host"):
            raise HTTPException(403, "拒绝跨站登录")
        actor = app.state.security.authenticate_key(body.token.get_secret_value())
        response = JSONResponse({"id": actor.id, "role": actor.role})
        response.set_cookie(
            "relay_session",
            app.state.security.signer.dumps(actor.id),
            httponly=True,
            samesite="lax",
            secure=app.state.settings.cookie_secure,
            max_age=8 * 3600,
        )
        return response

    @app.post("/auth/logout")
    async def logout(request: Request):
        principal(request)
        response = JSONResponse({"ok": True})
        response.delete_cookie("relay_session")
        return response

    @app.get("/auth/me")
    async def me(request: Request):
        actor = principal(request)
        return {"id": actor.id, "role": actor.role}

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    app.mount("/assets", StaticFiles(directory=STATIC), name="assets")
    return app


configure_logging()
app = create_app()
