import asyncio
import logging
import time
from uuid import uuid4

from sqlalchemy import select

from ..models import Agent, Command
from ..services.agents import inspect_agent
from .dispatch import Dispatcher
from .observe import Observer

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, database, adapters, settings):
        self.database, self.adapters, self.settings = database, adapters, settings
        self.dispatcher = Dispatcher(database, adapters)
        self.observer = Observer(database, adapters, uuid4().hex)
        self.tasks = []
        self.stopping = asyncio.Event()

    async def start(self):
        self.tasks = [
            asyncio.create_task(self.dispatch_loop()),
            asyncio.create_task(self.monitor_loop()),
        ]

    async def pause(self, seconds):
        try:
            await asyncio.wait_for(self.stopping.wait(), seconds)
        except TimeoutError:
            pass

    async def dispatch_loop(self):
        while not self.stopping.is_set():
            try:
                if await self.dispatcher.dispatch_one():
                    continue
            except Exception:
                logger.exception("Command worker iteration failed")
            await self.pause(0.4)

    async def monitor_loop(self):
        last_check = 0
        while not self.stopping.is_set():
            try:
                if time.monotonic() - last_check >= self.settings.reconcile_seconds:
                    async with self.database.transaction() as db:
                        agent_ids = list(await db.scalars(select(Agent.id)))
                        unknown = list(
                            await db.scalars(select(Command.id).where(Command.status == "unknown"))
                        )
                    for agent_id in agent_ids:
                        await inspect_agent(self.database, self.adapters, agent_id)
                    for command_id in unknown:
                        await self.dispatcher.reconcile(command_id)
                    last_check = time.monotonic()
                await self.observer.maintain()
            except Exception:
                logger.exception("Observation worker iteration failed")
            await self.pause(self.settings.poll_seconds)

    async def close(self):
        self.stopping.set()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.observer.close()
