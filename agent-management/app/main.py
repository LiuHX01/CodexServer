from contextlib import asynccontextmanager
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
from .logging_config import configure_logging
from .schemas import Login
from .security import Security, principal
from .workers.runtime import Worker

STATIC = Path(__file__).parent / "static"


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
        if config.embedded_worker:
            await worker.start()
        try:
            yield
        finally:
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
