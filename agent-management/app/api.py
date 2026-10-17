import asyncio
import json
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import exists, func, select
from sqlalchemy.exc import IntegrityError

from . import schemas
from .models import Agent, Command, Event, HumanRequest, Run, Session, SessionMember
from .presenters import public
from .security import Principal, authorize_session, principal, require_admin, require_operator
from .services.agents import inspect_agent
from .services.commands import enqueue
from .services.sessions import create_session

router = APIRouter(prefix="/v1")
Actor = Annotated[Principal, Depends(principal)]
Key = Annotated[str | None, Header(alias="Idempotency-Key")]


def accessible(actor):
    if actor.role == "admin":
        return True
    return (Session.owner == actor.id) | exists(
        select(SessionMember.session_id).where(
            SessionMember.session_id == Session.id,
            SessionMember.principal == actor.id,
        )
    )


async def get_agent(db, agent_id):
    row = await db.get(Agent, agent_id)
    if row is None:
        raise HTTPException(404, "执行端不存在")
    return row


@router.get("/overview")
async def overview(request: Request, actor: Actor):
    async with request.app.state.db.transaction() as db:
        sessions = select(Session.id).where(accessible(actor))

        async def count(model, *conditions):
            return await db.scalar(select(func.count()).select_from(model).where(*conditions))

        return {
            "agents": await count(Agent),
            "online_agents": await count(Agent, Agent.connection == "online"),
            "sessions": await count(Session, accessible(actor), Session.archived.is_(False)),
            "running": await count(
                Run, Run.session_id.in_(sessions), Run.status.in_(["queued", "running"])
            ),
            "pending_requests": await count(
                HumanRequest,
                HumanRequest.session_id.in_(sessions),
                HumanRequest.status == "pending",
            ),
            "unknown_commands": await count(
                Command, Command.session_id.in_(sessions), Command.status == "unknown"
            ),
        }


@router.get("/agents")
async def agents(request: Request, actor: Actor):
    async with request.app.state.db.transaction() as db:
        rows = (await db.scalars(select(Agent).order_by(Agent.created_at))).all()
        return [public(row) for row in rows]


@router.post("/agents", status_code=201)
async def register_agent(body: schemas.AgentCreate, request: Request, actor: Actor):
    require_admin(actor)
    async with request.app.state.db.transaction() as db:
        row = Agent(
            name=body.name,
            provider=body.provider,
            base_url=body.base_url,
            credential=request.app.state.security.encrypt(body.token.get_secret_value()),
        )
        db.add(row)
        await db.flush()
        agent_id = row.id
    await inspect_agent(request.app.state.db, request.app.state.adapters, agent_id)
    async with request.app.state.db.transaction() as db:
        return public(await db.get(Agent, agent_id))


@router.patch("/agents/{agent_id}")
async def edit_agent(agent_id: str, body: schemas.AgentPatch, request: Request, actor: Actor):
    require_admin(actor)
    async with request.app.state.db.transaction() as db:
        row = await get_agent(db, agent_id)
        for key, value in body.model_dump(exclude_none=True, exclude={"token"}).items():
            setattr(row, key, value)
        if body.token:
            row.credential = request.app.state.security.encrypt(body.token.get_secret_value())
        await db.flush()
        return public(row)


@router.post("/agents/{agent_id}/probe")
async def probe(agent_id: str, request: Request, actor: Actor):
    require_admin(actor)
    async with request.app.state.db.transaction() as db:
        await get_agent(db, agent_id)
    await inspect_agent(request.app.state.db, request.app.state.adapters, agent_id)
    async with request.app.state.db.transaction() as db:
        return public(await get_agent(db, agent_id))


@router.get("/agents/{agent_id}/remote-sessions")
async def remote_sessions(agent_id: str, request: Request, actor: Actor, cursor: str | None = None):
    # Listing the provider's entire account history is an administrative operation.
    require_admin(actor)
    async with request.app.state.db.transaction() as db:
        agent = await get_agent(db, agent_id)
    adapter = request.app.state.adapters.open(agent)
    try:
        from urllib.parse import urlencode

        return await adapter.get(
            "/v1/threads?" + urlencode({"limit": 50, **({"cursor": cursor} if cursor else {})})
        )
    finally:
        await adapter.close()


@router.post("/agents/{agent_id}/sessions", status_code=202)
async def new_session(
    agent_id: str,
    body: schemas.SessionCreate,
    request: Request,
    actor: Actor,
    idempotency_key: Key = None,
):
    require_operator(actor)
    command = await create_session(
        request.app.state.db, actor, agent_id, body.model_dump(exclude_none=True), idempotency_key
    )
    return public(command)


@router.post("/agents/{agent_id}/import", status_code=201)
async def import_session(
    agent_id: str, body: schemas.SessionImport, request: Request, actor: Actor
):
    require_admin(actor)
    async with request.app.state.db.transaction() as db:
        agent = await get_agent(db, agent_id)
    adapter = request.app.state.adapters.open(agent)
    try:
        thread = await adapter.get(f"/v1/threads/{body.thread_id}")
    finally:
        await adapter.close()
    try:
        async with request.app.state.db.transaction() as db:
            old = await db.scalar(
                select(Session).where(
                    Session.agent_id == agent_id, Session.provider_thread_id == body.thread_id
                )
            )
            if old:
                return public(old)
            row = Session(
                agent_id=agent_id,
                provider_thread_id=body.thread_id,
                source_instance_id=agent.instance_id or "legacy",
                owner=actor.id,
                name=thread.get("name") or thread.get("preview") or "导入的会话",
                cwd=thread.get("cwd"),
                state="unknown",
            )
            db.add(row)
            await db.flush()
            return public(row)
    except IntegrityError as exc:
        raise HTTPException(409, "会话已被导入，请刷新列表") from exc


@router.get("/sessions")
async def sessions(
    request: Request, actor: Actor, agent_id: str | None = None, archived: bool = False
):
    async with request.app.state.db.transaction() as db:
        query = select(Session).where(accessible(actor), Session.archived == archived)
        if agent_id:
            query = query.where(Session.agent_id == agent_id)
        rows = (await db.scalars(query.order_by(Session.updated_at.desc()).limit(200))).all()
        return [public(row) for row in rows]


@router.get("/sessions/{session_id}")
async def session_detail(session_id: str, request: Request, actor: Actor):
    async with request.app.state.db.transaction() as db:
        row = await authorize_session(db, session_id, actor)

        async def children(model):
            records = (
                await db.scalars(
                    select(model)
                    .where(model.session_id == session_id)
                    .order_by(model.created_at.desc())
                    .limit(300)
                )
            ).all()
            return [public(record) for record in reversed(records)]

        return {
            **public(row),
            "runs": await children(Run),
            "requests": await children(HumanRequest),
            "commands": await children(Command),
        }


@router.get("/sessions/{session_id}/history")
async def history(session_id: str, request: Request, actor: Actor, cursor: str | None = None):
    async with request.app.state.db.transaction() as db:
        row = await authorize_session(db, session_id, actor)
        agent = await db.get(Agent, row.agent_id)
    if not row.provider_thread_id:
        return {"data": [], "nextCursor": None}
    from urllib.parse import urlencode

    adapter = request.app.state.adapters.open(agent)
    try:
        return await adapter.get(
            f"/v1/threads/{row.provider_thread_id}/history?"
            + urlencode({"limit": 50, **({"cursor": cursor} if cursor else {})})
        )
    finally:
        await adapter.close()


@router.post("/sessions/{session_id}/messages", status_code=202)
async def send(
    session_id: str,
    body: schemas.Message,
    request: Request,
    actor: Actor,
    idempotency_key: Key = None,
):
    return public(
        await enqueue(
            request.app.state.db,
            actor,
            session_id,
            "start",
            body.model_dump(exclude_none=True),
            idempotency_key,
        )
    )


@router.post("/sessions/{session_id}/archive", status_code=202)
async def archive(session_id: str, request: Request, actor: Actor, idempotency_key: Key = None):
    return public(
        await enqueue(request.app.state.db, actor, session_id, "archive", {}, idempotency_key)
    )


async def run_command(request, actor, run_id, kind, payload, key):
    async with request.app.state.db.transaction() as db:
        run = await db.get(Run, run_id)
        if run is None:
            raise HTTPException(404, "运行不存在")
        await authorize_session(db, run.session_id, actor, write=True)
    return public(
        await enqueue(
            request.app.state.db, actor, run.session_id, kind, {"run_id": run_id, **payload}, key
        )
    )


@router.post("/runs/{run_id}/input", status_code=202)
async def steer(
    run_id: str, body: schemas.Text, request: Request, actor: Actor, idempotency_key: Key = None
):
    return await run_command(request, actor, run_id, "steer", body.model_dump(), idempotency_key)


@router.post("/runs/{run_id}/interrupt", status_code=202)
async def interrupt(run_id: str, request: Request, actor: Actor, idempotency_key: Key = None):
    return await run_command(request, actor, run_id, "interrupt", {}, idempotency_key)


@router.get("/requests")
async def requests(request: Request, actor: Actor, status: str | None = "pending"):
    async with request.app.state.db.transaction() as db:
        query = select(HumanRequest).join(Session).where(accessible(actor))
        if status:
            query = query.where(HumanRequest.status == status)
        rows = (await db.scalars(query.order_by(HumanRequest.created_at.desc()).limit(200))).all()
        return [public(row) for row in rows]


@router.post("/requests/{request_id}/response", status_code=202)
async def answer(
    request_id: str,
    body: schemas.Answer,
    request: Request,
    actor: Actor,
    idempotency_key: Key = None,
):
    async with request.app.state.db.transaction() as db:
        row = await db.get(HumanRequest, request_id)
        if row is None:
            raise HTTPException(404, "人工请求不存在")
        await authorize_session(db, row.session_id, actor, write=True)
    return public(
        await enqueue(
            request.app.state.db,
            actor,
            row.session_id,
            "reply",
            {"request_id": request_id, **body.model_dump()},
            idempotency_key,
        )
    )


@router.get("/commands")
async def commands(request: Request, actor: Actor):
    async with request.app.state.db.transaction() as db:
        rows = (
            await db.scalars(
                select(Command)
                .join(Session)
                .where(accessible(actor))
                .order_by(Command.created_at.desc())
                .limit(200)
            )
        ).all()
        return [public(row) for row in rows]


@router.get("/commands/{command_id}")
async def command_detail(command_id: str, request: Request, actor: Actor):
    async with request.app.state.db.transaction() as db:
        row = await db.get(Command, command_id)
        if row is None:
            raise HTTPException(404, "操作不存在")
        await authorize_session(db, row.session_id, actor)
        return public(row)


@router.post("/commands/{command_id}/reconcile")
async def reconcile(command_id: str, request: Request, actor: Actor):
    require_operator(actor)
    await command_detail(command_id, request, actor)
    await request.app.state.worker.dispatcher.reconcile(command_id)
    return await command_detail(command_id, request, actor)


@router.put("/sessions/{session_id}/members")
async def share(session_id: str, body: schemas.Member, request: Request, actor: Actor):
    require_operator(actor)
    if body.principal not in {key.principal for key in request.app.state.settings.principals()}:
        raise HTTPException(422, "该身份尚未配置访问密钥")
    async with request.app.state.db.transaction() as db:
        session = await authorize_session(db, session_id, actor, write=True)
        if actor.role != "admin" and session.owner != actor.id:
            raise HTTPException(403, "只有会话拥有者或管理员可以授权")
        member = await db.get(SessionMember, (session_id, body.principal))
        if member is None:
            member = SessionMember(session_id=session_id, principal=body.principal)
            db.add(member)
        member.permission = body.permission
        db.add(
            Event(
                session_id=session_id, type="access.granted", actor=actor.id, data=body.model_dump()
            )
        )
    return {"ok": True}


@router.get("/sessions/{session_id}/members")
async def members(session_id: str, request: Request, actor: Actor):
    async with request.app.state.db.transaction() as db:
        session = await authorize_session(db, session_id, actor)
        if actor.role != "admin" and session.owner != actor.id:
            raise HTTPException(403, "只有会话拥有者或管理员可以管理授权")
        rows = await db.scalars(select(SessionMember).where(SessionMember.session_id == session_id))
        return [public(row) for row in rows]


@router.delete("/sessions/{session_id}/members/{member_id}")
async def revoke_member(session_id: str, member_id: str, request: Request, actor: Actor):
    require_operator(actor)
    async with request.app.state.db.transaction() as db:
        session = await authorize_session(db, session_id, actor, write=True)
        if actor.role != "admin" and session.owner != actor.id:
            raise HTTPException(403, "只有会话拥有者或管理员可以管理授权")
        member = await db.get(SessionMember, (session_id, member_id))
        if member:
            await db.delete(member)
            db.add(
                Event(
                    session_id=session_id,
                    type="access.revoked",
                    actor=actor.id,
                    data={"principal": member_id},
                )
            )
    return {"ok": True}


@router.get("/logs")
async def logs(
    request: Request,
    actor: Actor,
    after: int = Query(0, ge=0),
    session_id: str | None = None,
    limit: int = Query(300, ge=1, le=500),
):
    """Return a bounded event log snapshot for the realtime operations console."""
    async with request.app.state.db.transaction() as db:
        query = select(Event).join(Session).where(accessible(actor), Event.id > after)
        if session_id:
            await authorize_session(db, session_id, actor)
            query = query.where(Event.session_id == session_id)
        rows = (await db.scalars(query.order_by(Event.id.desc()).limit(limit))).all()
        return [public(row) for row in reversed(rows)]


@router.get("/events")
async def events(
    request: Request,
    actor: Actor,
    after: int = Query(0, ge=0),
    session_id: str | None = None,
    follow: bool = True,
):
    try:
        header_cursor = int(request.headers.get("last-event-id", "0"))
        if header_cursor < 0:
            raise ValueError
        after = max(after, header_cursor)
    except ValueError as exc:
        raise HTTPException(400, "事件游标必须为非负整数") from exc
    if session_id:
        async with request.app.state.db.transaction() as db:
            await authorize_session(db, session_id, actor)

    async def stream():
        cursor = after
        while not await request.is_disconnected():
            async with request.app.state.db.transaction() as db:
                # Re-check membership while connected: an already open stream must
                # stop revealing a session as soon as its grant is revoked.
                if session_id:
                    try:
                        await authorize_session(db, session_id, actor)
                    except HTTPException:
                        return
                query = select(Event).join(Session).where(accessible(actor), Event.id > cursor)
                if session_id:
                    query = query.where(Event.session_id == session_id)
                batch = list(await db.scalars(query.order_by(Event.id).limit(200)))
            for row in batch:
                cursor = row.id
                payload = json.dumps(public(row), ensure_ascii=False)
                yield f"id: {row.id}\nevent: relay\ndata: {payload}\n\n"
            if batch:
                continue
            if not follow:
                return
            yield ": ping\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
