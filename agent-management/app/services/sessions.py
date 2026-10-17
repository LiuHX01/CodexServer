from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..models import Agent, Command, Event, Session, identifier
from .commands import fingerprint, match


async def create_session(database, actor, agent_id, payload, key):
    if not key or len(key) > 200:
        raise HTTPException(400, "创建会话需要 Idempotency-Key")
    digest = fingerprint("create_session", agent_id, payload)
    try:
        async with database.transaction() as db:
            agent = await db.get(Agent, agent_id)
            if agent is None:
                raise HTTPException(404, "执行端不存在")
            old = await db.scalar(
                select(Command).where(
                    Command.actor == actor.id,
                    Command.idempotency_key == key,
                )
            )
            if old:
                return match(old, digest)
            if not agent.enabled:
                raise HTTPException(409, "执行端已禁止新分派")
            row = Session(id=identifier(), agent_id=agent_id, owner=actor.id, name=payload["name"])
            db.add(row)
            await db.flush()
            command = Command(
                id=identifier(),
                session_id=row.id,
                actor=actor.id,
                kind="create_session",
                idempotency_key=key,
                fingerprint=digest,
                payload=payload,
            )
            db.add(command)
            db.add(
                Event(
                    session_id=row.id,
                    type="command.queued",
                    actor=actor.id,
                    data={"command_id": command.id, "kind": command.kind},
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
