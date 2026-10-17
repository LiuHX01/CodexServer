import asyncio
import json
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from uuid import uuid4

import aiosqlite

from .models import AgentRequest, Event, Run, now


class Store:
    """One owner per data directory; atomic run status + replayable event writes."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.lock = asyncio.Lock()
        self.db: aiosqlite.Connection | None = None
        self.owner = None
        self.changed = asyncio.Event()
        self.instance_id: str | None = None

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
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT,
                    thread_id TEXT, turn_id TEXT, event TEXT NOT NULL,
                    data TEXT NOT NULL, created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS agent_requests (
                    id TEXT PRIMARY KEY, thread_id TEXT NOT NULL,
                    status TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS requests_thread ON agent_requests(thread_id, status);
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS operations (
                    key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL,
                    status_code INTEGER, response TEXT
                );
            """)
            await self.db.execute(
                "INSERT OR IGNORE INTO metadata VALUES ('instance_id', ?)", (uuid4().hex,)
            )
            async with self.db.execute(
                "SELECT value FROM metadata WHERE key='instance_id'"
            ) as cursor:
                self.instance_id = (await cursor.fetchone())["value"]
            await self.db.execute(
                "UPDATE operations SET status='unknown' WHERE status='dispatching'"
            )
            await self.db.commit()
            await self._migrate_events()
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
            async with self.db.execute(
                "SELECT payload FROM agent_requests WHERE status IN ('pending', 'responded')"
            ) as cursor:
                old_requests = await cursor.fetchall()
            for row in old_requests:
                request = AgentRequest.model_validate_json(row["payload"])
                request.status = "expired"
                request.reason = "Service restarted; the original runtime request is no longer live"
                await self.save_request(request, "request.expired")
        except BaseException:
            await self.close()
            raise

    async def _migrate_events(self):
        """Keep existing event IDs so deployed clients can retain their replay cursors."""
        async with self.db.execute("PRAGMA table_info(events)") as cursor:
            columns = {row["name"] for row in await cursor.fetchall()}
        if "thread_id" not in columns:
            await self.db.executescript("""
                BEGIN IMMEDIATE;
                ALTER TABLE events RENAME TO legacy_events;
                CREATE TABLE events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT,
                    thread_id TEXT, turn_id TEXT, event TEXT NOT NULL,
                    data TEXT NOT NULL, created_at TEXT
                );
                INSERT INTO events(id, run_id, thread_id, turn_id, event, data, created_at)
                    SELECT e.id, e.run_id, r.thread_id, json_extract(r.payload, '$.turn_id'),
                           e.event, e.data, r.created_at
                    FROM legacy_events e LEFT JOIN runs r ON r.id = e.run_id;
                DROP TABLE legacy_events;
                COMMIT;
            """)
        await self.db.executescript("""
            CREATE INDEX IF NOT EXISTS events_run ON events(run_id, id);
            CREATE INDEX IF NOT EXISTS events_thread ON events(thread_id, id);
        """)

    async def close(self):
        if self.db:
            await self.db.close()
        if self.owner and not self.owner.closed:
            # Closing the descriptor releases the OS lock.
            self.owner.close()

    @asynccontextmanager
    async def _write(self):
        """Publish changes only after their state and events have committed together."""
        async with self.lock:
            try:
                yield
                await self.db.commit()
            except BaseException:
                with suppress(Exception):
                    await self.db.rollback()
                raise
        self.changed.set()

    async def _insert_event(self, event, data, thread_id, turn_id, run_id):
        await self.db.execute(
            "INSERT INTO events(run_id, thread_id, turn_id, event, data, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, thread_id, turn_id, event, json.dumps(data, ensure_ascii=False), now()),
        )

    async def save(self, run: Run, event: str, data: dict) -> None:
        run.updated_at = now()
        async with self._write():
            await self.db.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET status=excluded.status, payload=excluded.payload",
                (run.id, run.thread_id, run.status, run.model_dump_json(), run.created_at),
            )
            await self._insert_event(event, data, run.thread_id, run.turn_id, run.id)

    async def publish(
        self, event: str, data: dict, *, thread_id: str, turn_id: str | None = None
    ) -> None:
        async with self._write():
            await self._insert_event(event, data, thread_id, turn_id, None)

    async def save_request(self, request: AgentRequest, event: str) -> None:
        request.updated_at = now()
        async with self._write():
            await self.db.execute(
                "INSERT INTO agent_requests VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "status=excluded.status, payload=excluded.payload",
                (request.id, request.thread_id, request.status, request.model_dump_json()),
            )
            await self._insert_event(
                event,
                request.model_dump(mode="json"),
                request.thread_id,
                request.turn_id,
                request.run_id,
            )

    async def get_request(self, request_id: str) -> AgentRequest | None:
        async with (
            self.lock,
            self.db.execute(
                "SELECT payload FROM agent_requests WHERE id = ?", (request_id,)
            ) as cursor,
        ):
            row = await cursor.fetchone()
        return AgentRequest.model_validate_json(row["payload"]) if row else None

    async def requests(self, thread_id: str, *, pending_only: bool = True) -> list[AgentRequest]:
        query = "SELECT payload FROM agent_requests WHERE thread_id = ?"
        if pending_only:
            query += " AND status = 'pending'"
        query += " ORDER BY rowid"
        async with self.lock, self.db.execute(query, (thread_id,)) as cursor:
            return [
                AgentRequest.model_validate_json(row["payload"]) for row in await cursor.fetchall()
            ]

    async def get(self, run_id: str) -> Run | None:
        async with (
            self.lock,
            self.db.execute("SELECT payload FROM runs WHERE id = ?", (run_id,)) as cursor,
        ):
            row = await cursor.fetchone()
        return Run.model_validate_json(row["payload"]) if row else None

    async def operation(self, key: str) -> dict | None:
        async with (
            self.lock,
            self.db.execute("SELECT * FROM operations WHERE key=?", (key,)) as cursor,
        ):
            row = await cursor.fetchone()
        if row is None:
            return None
        return {**dict(row), "response": json.loads(row["response"]) if row["response"] else None}

    async def reserve_operation(self, key: str, fingerprint: str) -> bool:
        async with self._write():
            cursor = await self.db.execute(
                "INSERT OR IGNORE INTO operations(key, fingerprint, status) "
                "VALUES (?, ?, 'dispatching')",
                (key, fingerprint),
            )
            return cursor.rowcount == 1

    async def finish_operation(self, key: str, status_code: int, response: dict):
        async with self._write():
            await self.db.execute(
                "UPDATE operations SET status='completed', status_code=?, response=? WHERE key=?",
                (status_code, json.dumps(response, ensure_ascii=False), key),
            )

    async def uncertain_operation(self, key: str):
        async with self._write():
            await self.db.execute("UPDATE operations SET status='unknown' WHERE key=?", (key,))

    async def events(
        self,
        run_id: str | None,
        after: int,
        limit: int = 200,
        *,
        thread_id: str | None = None,
    ) -> list[Event]:
        if (run_id is None) == (thread_id is None):
            raise ValueError("Select either a run or a thread event stream")
        scope = "run_id" if run_id is not None else "thread_id"
        async with (
            self.lock,
            self.db.execute(
                f"SELECT * FROM events WHERE {scope} = ? AND id > ? ORDER BY id LIMIT ?",
                (run_id if run_id is not None else thread_id, after, limit),
            ) as cursor,
        ):
            rows = await cursor.fetchall()
        return [Event(**{**dict(row), "data": json.loads(row["data"])}) for row in rows]
