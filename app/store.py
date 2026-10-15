import asyncio
import json
import os
from contextlib import suppress
from pathlib import Path

import aiosqlite

from .models import Event, Run, now


class Store:
    """One owner per data directory; atomic run status + replayable event writes."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.lock = asyncio.Lock()
        self.db: aiosqlite.Connection | None = None
        self.owner = None

    async def open(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.owner = (self.directory / "server.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self.owner.seek(0)
                msvcrt.locking(self.owner.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.owner.close()
            raise RuntimeError("DATA_DIR already in use; start uvicorn with --workers 1") from exc
        try:
            self.db = await aiosqlite.connect(self.directory / "server.sqlite3")
            self.db.row_factory = aiosqlite.Row
            await self.db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, thread_id TEXT NOT NULL,
                    status TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS runs_thread ON runs(thread_id, created_at);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    event TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_run ON events(run_id, id);
            """)
            # A restarted process cannot claim that its previous turns are still live.
            async with self.db.execute(
                "SELECT payload FROM runs WHERE status IN ('queued', 'running')"
            ) as cursor:
                old_runs = await cursor.fetchall()
            for row in old_runs:
                run = Run.model_validate_json(row["payload"])
                run.status = "interrupted"
                run.error = "Service restarted; resume the thread and submit a new message"
                await self.save(run, "run.finished", run.model_dump(mode="json"))
        except BaseException:
            await self.close()
            raise

    async def close(self):
        if self.db:
            await self.db.close()
        if self.owner and not self.owner.closed:
            # Closing the descriptor releases the OS lock.
            self.owner.close()

    async def save(self, run: Run, event: str, data: dict) -> None:
        run.updated_at = now()
        async with self.lock:
            try:
                await self.db.execute(
                    "INSERT INTO runs VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "status=excluded.status, payload=excluded.payload",
                    (run.id, run.thread_id, run.status, run.model_dump_json(), run.created_at),
                )
                await self.db.execute(
                    "INSERT INTO events(run_id, event, data) VALUES (?, ?, ?)",
                    (run.id, event, json.dumps(data, ensure_ascii=False)),
                )
                await self.db.commit()
            except BaseException:
                with suppress(Exception):
                    await self.db.rollback()
                raise

    async def get(self, run_id: str) -> Run | None:
        async with (
            self.lock,
            self.db.execute("SELECT payload FROM runs WHERE id = ?", (run_id,)) as cursor,
        ):
            row = await cursor.fetchone()
        return Run.model_validate_json(row["payload"]) if row else None

    async def events(self, run_id: str, after: int, limit: int = 200) -> list[Event]:
        async with (
            self.lock,
            self.db.execute(
                "SELECT id, event, data FROM events "
                "WHERE run_id = ? AND id > ? ORDER BY id LIMIT ?",
                (run_id, after, limit),
            ) as cursor,
        ):
            rows = await cursor.fetchall()
        return [
            Event(id=row["id"], event=row["event"], data=json.loads(row["data"])) for row in rows
        ]
