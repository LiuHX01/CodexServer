import logging
from datetime import timedelta

from sqlalchemy import exists, select, update
from sqlalchemy.orm import aliased

from ..adapters.base import DeliveryUnknown, ProviderError
from ..models import Agent, Command, Event, HumanRequest, Run, Session, now
from ..services.agents import inspect_agent
from ..services.events import activity, upsert_request, upsert_run

logger = logging.getLogger(__name__)


class Dispatcher:
    def __init__(self, database, adapters):
        self.database, self.adapters = database, adapters

    async def claim(self):
        async with self.database.transaction() as db:
            await db.execute(
                update(Command)
                .where(
                    Command.status == "dispatching",
                    Command.lease_until < now(),
                )
                .values(status="unknown", error="投递进程退出或租约过期，远端结果待核对")
            )
            busy = aliased(Command)
            row = await db.scalar(
                select(Command)
                .where(
                    Command.status == "queued",
                    ~exists(
                        select(busy.id).where(
                            busy.session_id == Command.session_id,
                            busy.status == "dispatching",
                        )
                    ),
                )
                .order_by(Command.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return None
            # Locking the conversation prevents two workers claiming adjacent commands.
            await db.scalar(select(Session).where(Session.id == row.session_id).with_for_update())
            other = await db.scalar(
                select(Command.id)
                .where(
                    Command.session_id == row.session_id,
                    Command.status == "dispatching",
                )
                .limit(1)
            )
            if other:
                return None
            row.status = "dispatching"
            row.lease_until = now() + timedelta(seconds=60)
            db.add(
                Event(
                    session_id=row.session_id,
                    type="command.dispatching",
                    actor=row.actor,
                    data={
                        "command_id": row.id,
                        "kind": row.kind,
                        "lease_seconds": 60,
                    },
                )
            )
            return row.id

    async def dispatch_one(self):
        command_id = await self.claim()
        if command_id is None:
            return False
        adapter = None
        try:
            async with self.database.transaction() as db:
                command = await db.get(Command, command_id)
                session = await db.get(Session, command.session_id)
                agent = await db.get(Agent, session.agent_id)
            if not agent.instance_id:
                await inspect_agent(self.database, self.adapters, agent.id)
            async with self.database.transaction() as db:
                command = await db.get(Command, command_id)
                session = await db.get(Session, command.session_id)
                agent = await db.get(Agent, session.agent_id)
                if not agent.instance_id:
                    raise ProviderError(409, "执行端尚未通过连接检查，未投递操作")
                if command.kind in {"create_session", "start"} and not agent.enabled:
                    raise ProviderError(409, "执行端已禁止新分派")
                if session.source_instance_id and session.source_instance_id != agent.instance_id:
                    raise ProviderError(409, "执行端实例已变化，已停止投递")
                path, body = await self.remote_operation(db, command, session)
                db.add(
                    Event(
                        session_id=session.id,
                        type="command.sending",
                        actor=command.actor,
                        data={
                            "command_id": command.id,
                            "kind": command.kind,
                            "method": "POST",
                            "path": path,
                            "has_body": body is not None,
                        },
                    )
                )
            adapter = self.adapters.open(agent)
            result = await adapter.mutate(path, body, command.id)
            await self.accept(command_id, result)
        except ProviderError as exc:
            await self.fail(command_id, "rejected", exc.message)
        except DeliveryUnknown as exc:
            await self.fail(command_id, "unknown", str(exc))
        except Exception:
            logger.exception("Command delivery requires reconciliation: %s", command_id)
            await self.fail(command_id, "unknown", "操作结果待核对，请勿重复提交")
        finally:
            if adapter:
                await adapter.close()
        return True

    @staticmethod
    async def remote_operation(db, command, session):
        payload = command.payload
        thread = session.provider_thread_id
        if command.kind == "create_session":
            return "/v1/threads", payload
        if command.kind == "start":
            return f"/v1/threads/{thread}/messages", {**payload, "stream": False}
        if command.kind in {"steer", "interrupt"}:
            run = await db.get(Run, payload["run_id"])
            if run is None or run.session_id != session.id:
                raise ProviderError(404, "运行不存在")
            if run.status not in {"queued", "running"}:
                raise ProviderError(409, "运行已经结束")
            if command.kind == "steer":
                return f"/v1/runs/{run.provider_run_id}/input", {"prompt": payload["prompt"]}
            return f"/v1/runs/{run.provider_run_id}/cancel", None
        if command.kind == "reply":
            request = await db.get(HumanRequest, payload["request_id"])
            if request is None or request.session_id != session.id or request.status != "pending":
                raise ProviderError(409, "人工请求已经失效或被回答")
            return f"/v1/requests/{request.provider_request_id}/response", {
                "result": payload["result"]
            }
        if command.kind == "archive":
            return f"/v1/threads/{thread}/archive", None
        raise ProviderError(400, "不支持的操作")

    async def accept(self, command_id, result):
        async with self.database.transaction() as db:
            command = await db.scalar(
                select(Command).where(Command.id == command_id).with_for_update()
            )
            if command.status == "accepted":
                return
            session = await db.scalar(
                select(Session)
                .where(
                    Session.id == command.session_id,
                )
                .with_for_update()
            )
            if command.kind == "create_session":
                agent = await db.get(Agent, session.agent_id)
                session.provider_thread_id = result["id"]
                session.source_instance_id = agent.instance_id or "legacy"
                session.cwd = result.get("cwd")
                session.state = "idle"
            elif command.kind in {"start", "interrupt"}:
                await upsert_run(db, session, result, acknowledgement=True)
                await activity(db, session)
            elif command.kind == "reply":
                request = await upsert_request(db, session, result, acknowledgement=True)
                request.answered_by = command.actor
            elif command.kind == "archive":
                session.archived = True
            command.status, command.result, command.error = "accepted", result, None
            session.version += 1
            db.add(
                Event(
                    session_id=session.id,
                    type="command.accepted",
                    actor=command.actor,
                    data={"command_id": command.id, "kind": command.kind},
                )
            )

    async def fail(self, command_id, status, reason):
        async with self.database.transaction() as db:
            command = await db.get(Command, command_id)
            command.status, command.error = status, reason
            db.add(
                Event(
                    session_id=command.session_id,
                    type=f"command.{status}",
                    data={"command_id": command.id, "kind": command.kind, "error": reason},
                )
            )
            if command.kind == "create_session":
                session = await db.get(Session, command.session_id)
                session.state, session.error = status, reason

    async def reconcile(self, command_id):
        async with self.database.transaction() as db:
            command = await db.get(Command, command_id)
            if command is None or command.status not in {"unknown", "dispatching"}:
                return
            session = await db.get(Session, command.session_id)
            agent = await db.get(Agent, session.agent_id)
        adapter = self.adapters.open(agent)
        if session.source_instance_id and session.source_instance_id != agent.instance_id:
            await adapter.close()
            return
        try:
            async with self.database.transaction() as db:
                db.add(
                    Event(
                        session_id=session.id,
                        type="command.reconcile.started",
                        actor=command.actor,
                        data={"command_id": command.id, "kind": command.kind},
                    )
                )
            receipt = await adapter.get(f"/v1/operations/{command.id}")
            if receipt["status"] == "completed":
                if receipt["status_code"] < 400:
                    await self.accept(command_id, receipt["response"])
                else:
                    await self.fail(command_id, "rejected", "执行端已明确拒绝原操作")
            else:
                async with self.database.transaction() as db:
                    db.add(
                        Event(
                            session_id=session.id,
                            type="command.reconcile.pending",
                            actor=command.actor,
                            data={"command_id": command.id, "remote_status": receipt.get("status")},
                        )
                    )
        except ProviderError:
            # Missing receipts do not prove that a mutation never took place.
            pass
        finally:
            await adapter.close()
