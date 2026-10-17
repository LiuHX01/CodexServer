from datetime import datetime

from sqlalchemy import select

from ..models import Agent, Event, HumanRequest, Run, Session, now

ACTIVE = {"queued", "running"}


async def activity(db, session):
    active = await db.scalar(
        select(Run.id)
        .where(
            Run.session_id == session.id,
            Run.status.in_(ACTIVE),
        )
        .limit(1)
    )
    session.state = "running" if active else "idle"


async def upsert_run(db, session, data, *, acknowledgement=False):
    native_id = data.get("id") or data.get("run_id")
    if not native_id:
        return
    row = await db.scalar(
        select(Run).where(
            Run.session_id == session.id,
            Run.provider_run_id == native_id,
        )
    )
    if row is None:
        row = Run(session_id=session.id, provider_run_id=native_id, status="queued")
        created = data.get("created_at")
        if created:
            row.created_at = datetime.fromisoformat(created)
        db.add(row)
    for local, remote in [
        ("provider_turn_id", "turn_id"),
        ("status", "status"),
        ("options", "options"),
        ("final_response", "final_response"),
        ("error", "error"),
        ("cancel_requested", "cancel_requested"),
    ]:
        if remote in data and data[remote] is not None:
            if acknowledgement and local == "status":
                # An HTTP acknowledgement may arrive after the completion event.
                # It describes acceptance at an earlier instant, not current state.
                if row.status not in ACTIVE or (
                    row.status == "running" and data[remote] == "queued"
                ):
                    continue
            setattr(row, local, data[remote])
    await db.flush()
    return row


async def upsert_request(db, session, data, *, acknowledgement=False):
    row = await db.scalar(
        select(HumanRequest).where(
            HumanRequest.session_id == session.id,
            HumanRequest.provider_request_id == data["id"],
        )
    )
    if row is None:
        row = HumanRequest(
            session_id=session.id,
            provider_request_id=data["id"],
            method=data["method"],
            status=data["status"],
        )
        db.add(row)
    if not acknowledgement or row.status not in {"resolved", "expired"}:
        row.status = data["status"]
    row.params = data["params"]
    row.provider_run_id = data.get("run_id")
    row.response = data.get("response")
    row.reason = data.get("reason")
    await db.flush()
    return row


async def ingest(database, session_id, envelope, *, observer=None):
    """Commit the source cursor, mirror updates and event together; replay is harmless."""
    async with database.transaction() as db:
        session = await db.scalar(select(Session).where(Session.id == session_id).with_for_update())
        if observer is not None:
            agent = await db.get(Agent, session.agent_id)
            if (
                session.observer_owner != observer
                or session.observer_until is None
                or session.observer_until < now()
                or session.source_instance_id != agent.instance_id
            ):
                return False
        if envelope["id"] <= session.event_cursor:
            return True
        kind, data = envelope["event"], envelope["data"]
        if kind in {"run.queued", "run.running", "run.started", "run.finished"}:
            await upsert_run(db, session, data)
        elif kind.startswith("request.") and "method" in data:
            await upsert_request(db, session, data)
        db.add(
            Event(
                session_id=session.id,
                source_instance_id=session.source_instance_id,
                provider_event_id=envelope["id"],
                type=kind,
                data=envelope,
            )
        )
        session.event_cursor = envelope["id"]
        session.observed_at = now()
        session.error = None
        await activity(db, session)
        return True
