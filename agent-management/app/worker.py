"""Standalone worker: uv run python -m app.worker."""

import asyncio
import signal

from .adapters.codex import AdapterRegistry
from .config import Settings
from .database import Database
from .security import Security
from .workers.runtime import Worker


async def main():
    settings = Settings()
    database = Database(settings.database_url)
    worker = Worker(database, AdapterRegistry(Security(settings)), settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await worker.start()
    try:
        await stop.wait()
    finally:
        await worker.close()
        await database.close()


if __name__ == "__main__":
    asyncio.run(main())
