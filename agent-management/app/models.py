"""Persistence records. Runs and requests mirror provider facts, not business tasks."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now():
    return datetime.now(UTC)


def identifier():
    return uuid4().hex


class Base(DeclarativeBase):
    pass


class Record:
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Agent(Record, Base):
    __tablename__ = "agents"
    name: Mapped[str] = mapped_column(String(200))
    provider: Mapped[str] = mapped_column(String(40), default="codex")
    base_url: Mapped[str] = mapped_column(String(2000))
    credential: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    connection: Mapped[str] = mapped_column(String(30), default="unknown")
    instance_id: Mapped[str | None] = mapped_column(String(100))
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)
    remote_status: Mapped[dict] = mapped_column(JSON, default=dict)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class Session(Record, Base):
    __tablename__ = "sessions"
    __table_args__ = (UniqueConstraint("agent_id", "provider_thread_id"),)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    provider_thread_id: Mapped[str | None] = mapped_column(String(100))
    source_instance_id: Mapped[str | None] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(200))
    owner: Mapped[str] = mapped_column(String(100))
    cwd: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(40), default="provisioning")
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observer_owner: Mapped[str | None] = mapped_column(String(32))
    observer_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    event_cursor: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    error: Mapped[str | None] = mapped_column(Text)


class SessionMember(Base):
    __tablename__ = "session_members"
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), primary_key=True)
    principal: Mapped[str] = mapped_column(String(100), primary_key=True)
    permission: Mapped[str] = mapped_column(String(20))


class Run(Record, Base):
    __tablename__ = "runs"
    __table_args__ = (UniqueConstraint("session_id", "provider_run_id"),)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    provider_run_id: Mapped[str] = mapped_column(String(100))
    provider_turn_id: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(30))
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    final_response: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)


class HumanRequest(Record, Base):
    __tablename__ = "requests"
    __table_args__ = (UniqueConstraint("session_id", "provider_request_id"),)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    provider_request_id: Mapped[str] = mapped_column(String(100))
    provider_run_id: Mapped[str | None] = mapped_column(String(100))
    method: Mapped[str] = mapped_column(String(150))
    status: Mapped[str] = mapped_column(String(30))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    response: Mapped[dict | None] = mapped_column(JSON)
    reason: Mapped[str | None] = mapped_column(Text)
    answered_by: Mapped[str | None] = mapped_column(String(100))


class Command(Record, Base):
    __tablename__ = "commands"
    __table_args__ = (
        UniqueConstraint("actor", "idempotency_key"),
        Index("commands_queue", "status", "created_at"),
    )
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    actor: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(40))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    fingerprint: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30), default="queued")
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("session_id", "source_instance_id", "provider_event_id"),
        Index("events_session_cursor", "session_id", "id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"))
    source_instance_id: Mapped[str | None] = mapped_column(String(100))
    provider_event_id: Mapped[int | None] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(160))
    data: Mapped[dict] = mapped_column(JSON)
    actor: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
