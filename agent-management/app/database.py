from contextlib import asynccontextmanager

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session as OrmSession

from .models import Event


@event.listens_for(OrmSession, "before_flush")
def serialize_event_publication(session, flush_context, instances):
    # PostgreSQL sequences allocate before commit. Serialize publishers so a
    # subscriber never advances past a lower event ID in an uncommitted transaction.
    # The lock covers event publication only; remote I/O never holds it.
    if any(isinstance(row, Event) for row in session.new):
        session.connection().execute(text("SELECT pg_advisory_xact_lock(712084901)"))


class Database:
    def __init__(self, url: str, **engine_options):
        self.engine = create_async_engine(url, pool_pre_ping=True, **engine_options)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def transaction(self):
        async with self.sessions() as db, db.begin():
            yield db

    async def close(self):
        await self.engine.dispose()
