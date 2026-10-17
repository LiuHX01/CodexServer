"""Durable command acceptance. HTTP success is not remote execution completion."""

import hashlib
import json

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..models import Agent, Command, Event, identifier
from ..security import authorize_session


def fingerprint(kind, session_id, payload):
    body = json.dumps([kind, session_id, payload], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


async def enqueue(database, actor, session_id, kind, payload, key):
    if not key or len(key) > 200:
        raise HTTPException(400, "操作需要不超过 200 字符的 Idempotency-Key")
    digest = fingerprint(kind, session_id, payload)
    try:
        async with database.transaction() as db:
            session = await authorize_session(db, session_id, actor, write=True)
            old = await db.scalar(
                select(Command).where(
                    Command.actor == actor.id,
                    Command.idempotency_key == key,
                )
            )
            if old:
                return match(old, digest)
            await db.refresh(session, with_for_update=True)
            agent = await db.get(Agent, session.agent_id)
            if kind in {"create_session", "start"} and not agent.enabled:
                raise HTTPException(409, "执行端已禁止新分派")
            if kind != "create_session" and not session.provider_thread_id:
                raise HTTPException(409, "会话尚未在执行端准备完成")
            if session.source_instance_id and agent.instance_id != session.source_instance_id:
                raise HTTPException(409, "执行端数据实例已变化，原会话需要人工核对")
            if session.archived:
                raise HTTPException(409, "会话已归档")
            if kind in {"steer", "interrupt"}:
                from ..models import Run

                run = await db.get(Run, payload["run_id"])
                if run is None or run.session_id != session.id:
                    raise HTTPException(404, "运行不存在")
                if run.status not in {"queued", "running"}:
                    raise HTTPException(409, "运行已经结束")
                if kind == "steer" and run.status != "running":
                    raise HTTPException(409, "运行尚未开始，不能追加当前轮输入")
            if kind == "reply":
                from ..models import HumanRequest

                pending = await db.get(HumanRequest, payload["request_id"])
                if (
                    pending is None
                    or pending.session_id != session.id
                    or pending.status != "pending"
                ):
                    raise HTTPException(409, "人工请求已经失效或被回答")
            capability = {"steer": "steering", "reply": "human_requests"}.get(kind)
            if capability and not agent.capabilities.get("capabilities", {}).get(capability):
                raise HTTPException(409, "执行端未声明支持此项能力")
            if kind == "start":
                from ..models import Run

                active = await db.scalar(
                    select(Run.id)
                    .where(
                        Run.session_id == session.id,
                        Run.status.in_(["queued", "running"]),
                    )
                    .limit(1)
                )
                queued = await db.scalar(
                    select(Command.id)
                    .where(
                        Command.session_id == session.id,
                        Command.kind == "start",
                        Command.status.in_(["queued", "dispatching", "unknown"]),
                    )
                    .limit(1)
                )
                if active or queued:
                    raise HTTPException(409, "会话已有运行或结果待核对，请追加输入或先中断")
            command = Command(
                id=identifier(),
                session_id=session.id,
                actor=actor.id,
                kind=kind,
                idempotency_key=key,
                fingerprint=digest,
                payload=payload,
            )
            db.add(command)
            db.add(
                Event(
                    session_id=session.id,
                    type="command.queued",
                    actor=actor.id,
                    data={"command_id": command.id, "kind": kind, "payload": payload},
                )
            )
            await db.flush()
            return command
    except IntegrityError:
        async with database.transaction() as db:
            old = await db.scalar(
                select(Command).where(
                    Command.actor == actor.id,
                    Command.idempotency_key == key,
                )
            )
            if old is None:
                raise
            return match(old, digest)


def match(command, digest):
    if command.fingerprint != digest:
        raise HTTPException(409, "此幂等键已用于不同操作")
    return command
