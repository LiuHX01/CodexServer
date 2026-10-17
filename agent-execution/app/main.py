import asyncio
import html
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from openai_codex import (
    CodexError,
    InvalidParamsError,
    InvalidRequestError,
    ServerBusyError,
    TransportClosedError,
)

from .api import router
from .backend import CodexBackend
from .config import Settings
from .idempotency import IdempotencyMiddleware
from .manager import Manager
from .store import Store

logger = logging.getLogger(__name__)
DOCS_ROOT = Path(__file__).resolve().parent / "static"
DOCS_HTML = (DOCS_ROOT / "docs" / "index.html").read_text(encoding="utf-8")
DOCS_FILES = {
    "swagger-ui-bundle.js": DOCS_ROOT / "swagger" / "swagger-ui-bundle.js",
    "swagger-ui.css": DOCS_ROOT / "swagger" / "swagger-ui.css",
    "favicon-32x32.png": DOCS_ROOT / "swagger" / "favicon-32x32.png",
    "console.css": DOCS_ROOT / "docs" / "console.css",
    "console.js": DOCS_ROOT / "docs" / "console.js",
    "codex.svg": DOCS_ROOT / "docs" / "codex.svg",
}


def create_app(settings: Settings | None = None, backend: CodexBackend | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config = settings or Settings()
        config.execution_options(None)
        # SDK streams use blocking waits in worker threads; reserve capacity for RPCs as well.
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(
                max_workers=config.max_concurrent_runs * 2 + 16, thread_name_prefix="codex-sdk"
            )
        )
        store = Store(config.data_dir)
        await store.open()
        runtime = backend or CodexBackend(config)
        manager = Manager(runtime, store, config)
        app.state.settings, app.state.manager = config, manager
        try:
            await runtime.start()
            yield
        finally:
            try:
                await manager.shutdown()
            finally:
                try:
                    await runtime.close()
                finally:
                    await store.close()

    app = FastAPI(
        title="Codex Server",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        openapi_tags=[
            {"name": "会话", "description": "项目的工作上下文"},
            {"name": "对话", "description": "发送指令与接收执行结果"},
            {"name": "交互", "description": "Agent 发起的提问与审批"},
            {"name": "服务", "description": "连接、并发与任务队列"},
        ],
    )

    @app.exception_handler(CodexError)
    async def codex_error(request: Request, exc: CodexError):
        logger.exception("Codex backend error", exc_info=exc)
        if isinstance(exc, (ServerBusyError, TransportClosedError)):
            code = 503
        elif isinstance(exc, InvalidParamsError):
            code = 400
        elif isinstance(exc, InvalidRequestError):
            code = 404 if "no rollout found" in exc.message else 409
        else:
            code = 502
        return JSONResponse(status_code=code, content={"detail": "Codex backend request failed"})

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"status": "ok"}

    @app.get("/docs-assets/{filename}", include_in_schema=False)
    async def docs_asset(filename: str, request: Request):
        if not request.app.state.settings.enable_docs or filename not in DOCS_FILES:
            raise HTTPException(404)
        return FileResponse(DOCS_FILES[filename])

    @app.get("/openapi.json", include_in_schema=False)
    async def openapi(request: Request):
        if not request.app.state.settings.enable_docs:
            raise HTTPException(404)
        return app.openapi()

    @app.get("/docs", include_in_schema=False)
    async def docs(request: Request):
        if not request.app.state.settings.enable_docs:
            raise HTTPException(404)
        prefix = request.scope.get("root_path", "").rstrip("/")
        config = json.dumps(
            {
                "rootPath": prefix,
                "schemaUrl": f"{prefix}/openapi.json",
                "healthUrl": f"{prefix}/healthz",
                "model": request.app.state.settings.codex_model,
                "effort": request.app.state.settings.codex_reasoning_effort,
            },
            ensure_ascii=False,
        ).replace("<", "\\u003c")
        page = DOCS_HTML.replace("__ASSET_PREFIX__", html.escape(prefix, quote=True))
        return HTMLResponse(page.replace("__DOCS_CONFIG__", config))

    app.add_middleware(IdempotencyMiddleware)
    app.include_router(router)
    return app


app = create_app()
