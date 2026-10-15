import asyncio
import hmac
import html
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from openai_codex import (
    CodexError,
    InvalidParamsError,
    InvalidRequestError,
    ServerBusyError,
    TransportClosedError,
)

from .backend import CodexBackend
from .config import Settings
from .manager import Manager
from .models import TERMINAL, CreateThread, Message, Run
from .store import Store

logger = logging.getLogger(__name__)
bearer = HTTPBearer(auto_error=False)
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
            {"name": "服务", "description": "连接、并发与任务队列"},
        ],
    )

    async def authenticate(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ):
        expected = request.app.state.settings.api_token.get_secret_value()
        if credentials is None or not hmac.compare_digest(
            credentials.credentials.encode(), expected.encode()
        ):
            raise HTTPException(401, "Invalid bearer token", headers={"WWW-Authenticate": "Bearer"})

    def manager(request: Request) -> Manager:
        return request.app.state.manager

    ManagerDep = Annotated[Manager, Depends(manager)]
    api = APIRouter(prefix="/v1", dependencies=[Depends(authenticate)])

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

    @api.get("/status", tags=["服务"], summary="查看服务状态")
    async def status(m: ManagerDep):
        return {
            "backend_ready": m.backend.healthy,
            "stopping": m.stopping,
            "max_concurrent_runs": m.settings.max_concurrent_runs,
            "running": sum(x.run.status == "running" for x in m.live.values()),
            "queued": sum(x.run.status == "queued" for x in m.live.values()),
            "busy_threads": m.owners.copy(),
        }

    @api.post("/threads", status_code=201, tags=["会话"], summary="创建会话")
    async def create_thread(body: CreateThread, m: ManagerDep):
        if not m.backend.healthy:
            raise HTTPException(503, "Codex backend is unavailable")
        if body.cwd is None:
            cwd = m.settings.workspace_root.resolve() / uuid4().hex
            cwd.mkdir(parents=True)
        else:
            cwd = workspace(m, body.cwd)
        thread = await m.backend.create(str(cwd))
        if body.name:
            await m.backend.rename(thread["id"], body.name)
            thread["name"] = body.name
        return thread

    @api.get("/threads", tags=["会话"], summary="列出会话")
    async def list_threads(
        m: ManagerDep,
        cursor: str | None = None,
        limit: int = Query(50, ge=1, le=100),
        archived: bool = False,
    ):
        return await m.backend.list(
            cursor=cursor,
            limit=limit,
            archived=archived,
        )

    @api.get("/threads/{thread_id}/history", tags=["会话"], summary="读取对话历史")
    async def history(
        thread_id: str,
        m: ManagerDep,
        cursor: str | None = None,
        limit: int = Query(50, ge=1, le=100),
    ):
        await m.check_thread(thread_id)
        return await m.backend.history(thread_id, cursor, limit)

    def workspace(m: Manager, cwd: str):
        try:
            return m.settings.workspace(cwd)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.post("/threads/{thread_id}/resume", tags=["会话"], summary="继续已有会话")
    async def resume(thread_id: str, m: ManagerDep):
        async with m.manage(thread_id):
            await m.check_thread(thread_id, require_idle=True)
            return await m.backend.resume(thread_id)

    @api.post("/threads/{thread_id}/archive", tags=["会话"], summary="归档会话")
    async def archive(thread_id: str, m: ManagerDep):
        async with m.manage(thread_id):
            await m.check_thread(thread_id, require_idle=True)
            return await m.backend.archive(thread_id)

    @api.post(
        "/threads/{thread_id}/messages",
        status_code=202,
        response_model=Run,
        tags=["对话"],
        summary="发送消息",
        responses={
            200: {
                "description": "stream=true：持续返回 SSE，直到 run.finished；X-Run-ID 为任务 ID",
                "content": {"text/event-stream": {"schema": {"type": "string"}}},
                "headers": {"X-Run-ID": {"schema": {"type": "string"}}},
            }
        },
    )
    async def message(thread_id: str, body: Message, request: Request, m: ManagerDep):
        run = await m.submit(thread_id, body)
        if body.stream:
            return event_response(run.id, request, m)
        return run

    @api.get("/runs/{run_id}", response_model=Run, tags=["对话"], summary="查看执行结果")
    async def get_run(run_id: str, m: ManagerDep):
        return await m.get(run_id)

    @api.post("/runs/{run_id}/cancel", response_model=Run, tags=["对话"], summary="中断执行")
    async def cancel(run_id: str, m: ManagerDep):
        return await m.cancel(run_id)

    def event_response(run_id: str, request: Request, m: Manager, after: int = 0):
        async def stream():
            cursor, last_ping = after, time.monotonic()
            while True:
                live = m.live.get(run_id)
                if live:
                    live.changed.clear()
                batch = await m.store.events(run_id, cursor)
                for event in batch:
                    cursor = event.id
                    payload = json.dumps(event.data, ensure_ascii=False, separators=(",", ":"))
                    yield f"id: {event.id}\nevent: {event.event}\ndata: {payload}\n\n"
                if batch:
                    continue
                # Snapshot first, then recheck events: completion must not race the final query.
                run = await m.get(run_id)
                if run.status in TERMINAL:
                    if await m.store.events(run_id, cursor, 1):
                        continue
                    return
                if time.monotonic() - last_ping >= 15:
                    yield ": ping\n\n"
                    last_ping = time.monotonic()
                if await request.is_disconnected():
                    return
                if live:
                    with suppress(TimeoutError):
                        await asyncio.wait_for(live.changed.wait(), timeout=1)
                else:
                    await asyncio.sleep(0.25)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Run-ID": run_id,
            },
        )

    @api.get("/runs/{run_id}/events", tags=["对话"], summary="订阅实时事件")
    async def events(
        run_id: str,
        request: Request,
        m: ManagerDep,
        after: int = Query(0, ge=0),
    ):
        await m.get(run_id)
        header = request.headers.get("last-event-id")
        if header is not None:
            try:
                after = max(after, int(header))
            except ValueError as exc:
                raise HTTPException(400, "Last-Event-ID must be a nonnegative integer") from exc
            if int(header) < 0:
                raise HTTPException(400, "Last-Event-ID must be a nonnegative integer")
        return event_response(run_id, request, m, after)

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

    app.include_router(api)
    return app


app = create_app()
