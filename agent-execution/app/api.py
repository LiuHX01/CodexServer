"""Authenticated Agent capability endpoints; no business workflow decisions."""

import hmac
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .manager import Manager
from .models import AgentRequest, CreateThread, Message, RequestResponse, Run, TextInput
from .streams import event_response, replay_cursor

bearer = HTTPBearer(auto_error=False)


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
router = APIRouter(prefix="/v1", dependencies=[Depends(authenticate)])


@router.get("/capabilities", tags=["服务"], summary="查看协议和能力")
async def capabilities(m: ManagerDep):
    return {
        "protocol_version": 1,
        "instance_id": m.store.instance_id,
        "provider": "codex",
        "capabilities": {
            "steering": True,
            "human_requests": True,
            "thread_events": True,
            "idempotency": True,
            "structured_input_modes": ["plan"],
            "resource_stop_confirmation": False,
            "workspace_transfer": False,
        },
    }


@router.get("/operations/{key}", tags=["服务"], summary="查询操作回执")
async def operation_receipt(key: str, m: ManagerDep):
    row = await m.store.operation(key)
    if row is None:
        raise HTTPException(404, "Operation receipt not found")
    return {key: value for key, value in row.items() if key != "fingerprint"}


def workspace(m: Manager, cwd: str):
    try:
        return m.settings.workspace(cwd)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/status", tags=["服务"], summary="查看服务状态")
async def status(m: ManagerDep):
    return {
        "backend_ready": m.backend.healthy,
        "stopping": m.stopping,
        "max_concurrent_runs": m.settings.max_concurrent_runs,
        "running": sum(x.run.status == "running" for x in m.live.values()),
        "queued": sum(x.run.status == "queued" for x in m.live.values()),
        "busy_threads": m.owners.copy(),
    }


@router.post("/threads", status_code=201, tags=["会话"], summary="创建会话")
async def create_thread(body: CreateThread, m: ManagerDep):
    if not m.backend.healthy:
        raise HTTPException(503, "Codex backend is unavailable")
    requested_cwd = body.cwd if body.cwd is not None else m.settings.default_cwd
    if requested_cwd is None:
        cwd = m.settings.workspace_root.resolve() / uuid4().hex
        cwd.mkdir(parents=True)
    else:
        cwd = workspace(m, requested_cwd)
    thread = await m.backend.create(str(cwd))
    if body.name:
        await m.backend.rename(thread["id"], body.name)
        thread["name"] = body.name
    return thread


@router.get("/threads", tags=["会话"], summary="列出会话")
async def list_threads(
    m: ManagerDep,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=100),
    archived: bool = False,
):
    return await m.backend.list(cursor=cursor, limit=limit, archived=archived)


@router.get("/threads/{thread_id}/history", tags=["会话"], summary="读取对话历史")
async def history(
    thread_id: str, m: ManagerDep, cursor: str | None = None, limit: int = Query(50, ge=1, le=100)
):
    await m.check_thread(thread_id)
    return await m.backend.history(thread_id, cursor, limit)


@router.get("/threads/{thread_id}", tags=["会话"], summary="读取会话当前状态")
async def read_thread(thread_id: str, m: ManagerDep):
    return await m.check_thread(thread_id)


@router.post("/threads/{thread_id}/resume", tags=["会话"], summary="继续已有会话")
async def resume(thread_id: str, m: ManagerDep):
    async with m.manage(thread_id):
        await m.check_thread(thread_id, require_idle=True)
        return await m.backend.resume(thread_id)


@router.post("/threads/{thread_id}/archive", tags=["会话"], summary="归档会话")
async def archive(thread_id: str, m: ManagerDep):
    async with m.manage(thread_id):
        await m.check_thread(thread_id, require_idle=True)
        return await m.backend.archive(thread_id)


@router.post(
    "/threads/{thread_id}/messages",
    status_code=202,
    response_model=Run,
    tags=["对话"],
    summary="开始执行",
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
    return event_response(request, m, run_id=run.id) if body.stream else run


@router.get("/runs/{run_id}", response_model=Run, tags=["对话"], summary="查看执行结果")
async def get_run(run_id: str, m: ManagerDep):
    return await m.get(run_id)


@router.post("/runs/{run_id}/input", tags=["对话"], summary="向执行中的 Agent 追加输入")
async def steer(run_id: str, body: TextInput, m: ManagerDep):
    return await m.steer(run_id, body.prompt)


@router.post("/runs/{run_id}/cancel", response_model=Run, tags=["对话"], summary="中断执行")
async def cancel(run_id: str, m: ManagerDep):
    return await m.cancel(run_id)


@router.get("/runs/{run_id}/events", tags=["对话"], summary="订阅执行事件")
async def run_events(run_id: str, request: Request, m: ManagerDep, after: int = Query(0, ge=0)):
    await m.get(run_id)
    return event_response(request, m, run_id=run_id, after=replay_cursor(request, after))


@router.get("/threads/{thread_id}/events", tags=["会话"], summary="订阅会话事件")
async def thread_events(
    thread_id: str,
    request: Request,
    m: ManagerDep,
    after: int = Query(0, ge=0),
    follow: bool = True,
):
    await m.check_thread(thread_id)
    return event_response(
        request, m, thread_id=thread_id, after=replay_cursor(request, after), follow=follow
    )


@router.get(
    "/threads/{thread_id}/requests",
    response_model=list[AgentRequest],
    tags=["交互"],
    summary="查询 Agent 请求",
)
async def requests(thread_id: str, m: ManagerDep, pending_only: bool = True):
    await m.check_thread(thread_id)
    return await m.store.requests(thread_id, pending_only=pending_only)


@router.post(
    "/requests/{request_id}/response",
    response_model=AgentRequest,
    tags=["交互"],
    summary="回复 Agent 提问或审批",
)
async def respond(request_id: str, body: RequestResponse, m: ManagerDep):
    return await m.interactions.respond(request_id, body.result)
