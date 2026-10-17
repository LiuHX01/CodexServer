import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select, update

from ..models import Agent, Event, Run, Session, now
from ..services.events import activity, ingest, upsert_request, upsert_run

logger = logging.getLogger(__name__)


class Observer:
    def __init__(self, database, adapters, owner):
        self.database, self.adapters, self.owner = database, adapters, owner
        self.tasks: dict[str, asyncio.Task] = {}

    async def maintain(self):
        for session_id, task in tuple(self.tasks.items()):
            if task.done():
                if not task.cancelled() and task.exception():
                    logger.warning("Session observation will retry: %s", session_id)
                self.tasks.pop(session_id)
        claimed = []
        async with self.database.transaction() as db:
            invalid = list(
                await db.scalars(
                    select(Session.id)
                    .join(Agent)
                    .where(
                        Session.id.in_(self.tasks),
                        (Session.source_instance_id != Agent.instance_id) | Session.archived,
                    )
                )
            )
            for session_id in invalid:
                self.tasks[session_id].cancel()
            # Renew only watchers we still own, never steal another worker's lease.
            if self.tasks:
                await db.execute(
                    update(Session)
                    .where(
                        Session.id.in_(self.tasks),
                        Session.observer_owner == self.owner,
                    )
                    .values(observer_until=now() + timedelta(seconds=20))
                )
            rows = (
                await db.scalars(
                    select(Session)
                    .where(
                        Session.provider_thread_id.is_not(None),
                        Session.archived.is_(False),
                        (Session.observer_until.is_(None)) | (Session.observer_until < now()),
                    )
                    .with_for_update(skip_locked=True)
                    .limit(100)
                )
            ).all()
            for row in rows:
                agent = await db.get(Agent, row.agent_id)
                if agent.instance_id != row.source_instance_id:
                    continue
                row.observer_owner = self.owner
                row.observer_until = now() + timedelta(seconds=20)
                claimed.append(row.id)
        # A watcher must see its committed lease before it starts remote I/O.
        for session_id in claimed:
            self.tasks[session_id] = asyncio.create_task(
                self.watch(session_id), name=f"observe-{session_id}"
            )

    async def reconcile(self, adapter, session_id):
        async with self.database.transaction() as db:
            session = await db.get(Session, session_id)
            runs = (
                await db.scalars(
                    select(Run).where(
                        Run.session_id == session_id,
                        Run.status.in_(["queued", "running"]),
                    )
                )
            ).all()
        thread = await adapter.get(f"/v1/threads/{session.provider_thread_id}")
        snapshots = [await adapter.get(f"/v1/runs/{run.provider_run_id}") for run in runs]
        requests = await adapter.get(
            f"/v1/threads/{session.provider_thread_id}/requests?pending_only=false"
        )
        async with self.database.transaction() as db:
            row = await db.scalar(select(Session).where(Session.id == session_id).with_for_update())
            agent = await db.get(Agent, row.agent_id)
            if row.observer_owner != self.owner or row.source_instance_id != agent.instance_id:
                return
            row.cwd = thread.get("cwd")
            row.observed_at, row.error = now(), None
            for data in snapshots:
                await upsert_run(db, row, data)
            for data in requests:
                await upsert_request(db, row, data)
            await activity(db, row)

    async def watch(self, session_id):
        adapter = None
        try:
            async with self.database.transaction() as db:
                session = await db.get(Session, session_id)
                agent = await db.get(Agent, session.agent_id)
            adapter = self.adapters.open(agent)
            async with self.database.transaction() as db:
                db.add(
                    Event(
                        session_id=session_id,
                        type="observer.connected",
                        data={"cursor": session.event_cursor, "instance_id": agent.instance_id},
                    )
                )
            await self.reconcile(adapter, session_id)
            async for event in adapter.watch(session.provider_thread_id, session.event_cursor):
                if not await ingest(self.database, session_id, event, observer=self.owner):
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            async with self.database.transaction() as db:
                session = await db.get(Session, session_id)
                if session.observer_owner == self.owner:
                    session.state = "unknown"
                    session.error = "事件连接中断，保留最后事实，正在重新核对"
                    db.add(
                        Event(
                            session_id=session_id,
                            type="observer.error",
                            data={"error": str(exc)[:500]},
                        )
                    )
            raise
        finally:
            if adapter:
                await adapter.close()
            async with self.database.transaction() as db:
                db.add(Event(session_id=session_id, type="observer.disconnected", data={}))
                await db.execute(
                    update(Session)
                    .where(
                        Session.id == session_id,
                        Session.observer_owner == self.owner,
                    )
                    .values(observer_owner=None, observer_until=None)
                )

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
