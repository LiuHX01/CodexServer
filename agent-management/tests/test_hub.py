import asyncio
from uuid import uuid4

from sqlalchemy import func, select

from app.models import Agent, Event, Session
from app.services.events import ingest
from tests.conftest import create_agent, create_session


def key():
    return {"Idempotency-Key": uuid4().hex}


async def start_run(service, session_id):
    client, worker, adapter, db = service
    command = (
        await client.post(
            f"/v1/sessions/{session_id}/messages", headers=key(), json={"prompt": "hello"}
        )
    ).json()
    await worker.dispatcher.dispatch_one()
    detail = (await client.get(f"/v1/sessions/{session_id}")).json()
    return detail["runs"][-1], command


async def test_login_permissions_and_credentials_are_not_exposed(service):
    client, worker, adapter, db = service
    assert (await client.get("/v1/agents", headers={"Authorization": ""})).status_code == 401
    response = await client.post(
        "/auth/login", json={"token": "relay-test-admin-key-with-at-least-24-characters"}
    )
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert (
        await client.post(
            "/auth/logout", headers={"Authorization": "", "Origin": "https://foreign.test"}
        )
    ).status_code == 403
    agent = await create_agent(client)
    assert "credential" not in agent
    async with db.transaction() as tx:
        row = await tx.get(Agent, agent["id"])
        assert "provider-secret" not in row.credential
    bob = {"Authorization": "Bearer bob-key-with-at-least-24-characters"}
    assert (
        await client.post(
            "/v1/agents",
            headers=bob,
            json={"name": "x", "base_url": "http://x.test", "token": "x" * 24},
        )
    ).status_code == 403


async def test_creation_is_atomic_idempotent_and_dispatches_once(service):
    client, worker, adapter, db = service
    agent = await create_agent(client)
    path = f"/v1/agents/{agent['id']}/sessions"
    headers = key()
    responses = await asyncio.gather(
        *(client.post(path, headers=headers, json={"name": "same"}) for _ in range(2))
    )
    assert [r.status_code for r in responses] == [202, 202]
    assert responses[0].json()["id"] == responses[1].json()["id"]
    assert (await client.post(path, headers=headers, json={"name": "different"})).status_code == 409
    async with db.transaction() as tx:
        assert await tx.scalar(select(func.count()).select_from(Session)) == 1
    assert await worker.dispatcher.dispatch_one()
    assert not await worker.dispatcher.dispatch_one()
    assert len(adapter.calls) == 1


async def test_full_run_controls_and_new_assignment_policy(service):
    client, worker, adapter, db = service
    session_id, agent_id = await create_session(service)
    run, command = await start_run(service, session_id)
    assert (
        await client.post(
            f"/v1/sessions/{session_id}/messages", headers=key(), json={"prompt": "duplicate"}
        )
    ).status_code == 409
    await client.patch(f"/v1/agents/{agent_id}", json={"enabled": False})
    steering = await client.post(
        f"/v1/runs/{run['id']}/input", headers=key(), json={"prompt": "more context"}
    )
    assert steering.status_code == 202
    await worker.dispatcher.dispatch_one()
    assert adapter.calls[-1][1] == {"prompt": "more context"}
    assert (await client.post(f"/v1/runs/{run['id']}/interrupt", headers=key())).status_code == 202
    await worker.dispatcher.dispatch_one()
    detail = (await client.get(f"/v1/sessions/{session_id}")).json()
    assert detail["runs"][-1]["cancel_requested"]
    assert detail["runs"][-1]["status"] == "running"
    assert (
        await client.post(
            f"/v1/agents/{agent_id}/sessions", headers=key(), json={"name": "disabled"}
        )
    ).status_code == 409


async def test_lost_response_reconciles_without_duplicate_mutation(service):
    client, worker, adapter, db = service
    session_id, _ = await create_session(service)
    adapter.ambiguous = True
    response = await client.post(
        f"/v1/sessions/{session_id}/messages", headers=key(), json={"prompt": "hello"}
    )
    command_id = response.json()["id"]
    await worker.dispatcher.dispatch_one()
    assert (await client.get(f"/v1/commands/{command_id}")).json()["status"] == "unknown"
    assert not await worker.dispatcher.dispatch_one()
    calls = len(adapter.calls)
    response = await client.post(f"/v1/commands/{command_id}/reconcile")
    assert response.json()["status"] == "accepted"
    assert len(adapter.calls) == calls
    assert len((await client.get(f"/v1/sessions/{session_id}")).json()["runs"]) == 1


async def test_replay_updates_state_and_cursor_in_one_transaction(service):
    client, worker, adapter, db = service
    session_id, _ = await create_session(service)
    run, _ = await start_run(service, session_id)
    event = {
        "id": 12,
        "event": "run.finished",
        "data": {"id": run["provider_run_id"], "status": "completed", "final_response": "done"},
    }
    await ingest(db, session_id, event)
    await ingest(db, session_id, event)
    async with db.transaction() as tx:
        assert (await tx.get(Session, session_id)).event_cursor == 12
        assert (
            await tx.scalar(
                select(func.count()).select_from(Event).where(Event.provider_event_id == 12)
            )
            == 1
        )
    detail = (await client.get(f"/v1/sessions/{session_id}")).json()
    assert detail["state"] == "idle"
    assert detail["runs"][0]["final_response"] == "done"
    events = await client.get(f"/v1/events?session_id={session_id}&follow=false")
    assert "run.finished" in events.text
    assert (
        await client.get("/v1/events?follow=false", headers={"Last-Event-ID": "invalid"})
    ).status_code == 400


async def test_session_visibility_and_grant_are_enforced(service):
    client, worker, adapter, db = service
    session_id, _ = await create_session(service)
    bob = {"Authorization": "Bearer bob-key-with-at-least-24-characters"}
    assert (await client.get(f"/v1/sessions/{session_id}", headers=bob)).status_code == 403
    assert (await client.get("/v1/sessions", headers=bob)).json() == []
    assert (
        await client.put(
            f"/v1/sessions/{session_id}/members", json={"principal": "bob", "permission": "view"}
        )
    ).status_code == 200
    assert (await client.get(f"/v1/sessions/{session_id}", headers=bob)).status_code == 200
    assert (
        await client.post(
            f"/v1/sessions/{session_id}/messages", headers={**bob, **key()}, json={"prompt": "no"}
        )
    ).status_code == 403


async def test_human_request_reply_preserves_provider_binding(service):
    client, worker, adapter, db = service
    session_id, _ = await create_session(service)
    await ingest(
        db,
        session_id,
        {
            "id": 1,
            "event": "request.pending",
            "data": {
                "id": "provider-request",
                "method": "item/tool/requestUserInput",
                "status": "pending",
                "params": {"questions": [{"id": "q", "question": "Which?"}]},
            },
        },
    )
    row = (await client.get("/v1/requests")).json()[0]
    response = await client.post(
        f"/v1/requests/{row['id']}/response",
        headers=key(),
        json={"result": {"answers": {"q": {"answers": ["A"]}}}},
    )
    assert response.status_code == 202
    await worker.dispatcher.dispatch_one()
    assert adapter.calls[-1][0] == "/v1/requests/provider-request/response"
    assert (await client.get("/v1/requests")).json() == []
    assert (
        await client.post(f"/v1/requests/{row['id']}/response", headers=key(), json={"result": {}})
    ).status_code == 409


async def test_frontend_is_local_and_all_primary_views_are_shipped(service):
    client, *_ = service
    response = await client.get("/")
    assert response.status_code == 200
    assert "AgentRelay" in response.text
    for path in ["app.js", "views.js", "api.js", "style.css", "mark.svg"]:
        assert (await client.get("/assets/" + path)).status_code == 200
    assert (await client.get("/healthz")).json() == {"status": "ok"}


async def test_same_reply_key_replays_after_request_has_been_answered(service):
    client, worker, adapter, db = service
    sid, _ = await create_session(service)
    await ingest(
        db,
        sid,
        {
            "id": 1,
            "event": "request.pending",
            "data": {
                "id": "question",
                "method": "item/tool/requestUserInput",
                "status": "pending",
                "params": {},
            },
        },
    )
    row = (await client.get("/v1/requests")).json()[0]
    path = f"/v1/requests/{row['id']}/response"
    headers = key()
    body = {"result": {"answers": {}}}
    first = await client.post(path, headers=headers, json=body)
    await worker.dispatcher.dispatch_one()
    replay = await client.post(path, headers=headers, json=body)
    assert replay.status_code == 202
    assert replay.json()["id"] == first.json()["id"]
    assert not await worker.dispatcher.dispatch_one()


async def test_obsolete_observer_cannot_advance_cursor(service):
    from datetime import timedelta

    from app.models import now

    client, worker, adapter, db = service
    sid, agent_id = await create_session(service)
    async with db.transaction() as tx:
        session = await tx.get(Session, sid)
        session.observer_owner = "new-owner"
        session.observer_until = now() + timedelta(seconds=30)
    event = {"id": 1, "event": "test", "data": {}}
    assert not await ingest(db, sid, event, observer="old-owner")
    assert await ingest(db, sid, event, observer="new-owner")
    async with db.transaction() as tx:
        (await tx.get(Agent, agent_id)).instance_id = "different-source"
    assert not await ingest(db, sid, {**event, "id": 2}, observer="new-owner")
    assert (await client.get(f"/v1/sessions/{sid}")).json()["event_cursor"] == 1
    assert (
        await client.get("/v1/events?follow=false", headers={"Last-Event-ID": "-1"})
    ).status_code == 400


async def test_event_publication_cannot_skip_an_uncommitted_lower_id(service):
    client, worker, adapter, db = service
    first, _ = await create_session(service)
    second, _ = await create_session(service)
    entered, release = asyncio.Event(), asyncio.Event()

    async def publish(sid, hold=False):
        async with db.transaction() as tx:
            row = Event(session_id=sid, type="publication-probe", data={})
            tx.add(row)
            await tx.flush()
            if hold:
                entered.set()
                await release.wait()
            return row.id

    slow = asyncio.create_task(publish(first, True))
    await asyncio.wait_for(entered.wait(), 3)
    fast = asyncio.create_task(publish(second))
    try:
        await asyncio.sleep(0.03)
        # The second publisher cannot commit an ID ahead of the held transaction.
        assert not fast.done()
    finally:
        release.set()
    ids = await asyncio.gather(slow, fast)
    assert ids[0] < ids[1]


async def test_late_command_acknowledgement_does_not_regress_completed_mirror(service):
    from app.services.events import upsert_request, upsert_run

    client, worker, adapter, db = service
    sid, _ = await create_session(service)
    async with db.transaction() as tx:
        session = await tx.get(Session, sid)
        await upsert_run(tx, session, {"id": "r", "status": "completed", "final_response": "done"})
        row = await upsert_run(tx, session, {"id": "r", "status": "queued"}, acknowledgement=True)
        assert row.status == "completed" and row.final_response == "done"
        data = {
            "id": "q",
            "method": "item/tool/requestUserInput",
            "status": "resolved",
            "params": {},
        }
        await upsert_request(tx, session, data)
        row = await upsert_request(
            tx, session, {**data, "status": "responded"}, acknowledgement=True
        )
        assert row.status == "resolved"


async def test_revoking_session_grant_removes_detail_history_and_events_access(service):
    client, worker, adapter, db = service
    sid, _ = await create_session(service)
    await client.put(f"/v1/sessions/{sid}/members", json={"principal": "bob", "permission": "view"})
    assert (await client.get(f"/v1/sessions/{sid}/members")).json()[0]["principal"] == "bob"
    bob = {"Authorization": "Bearer bob-key-with-at-least-24-characters"}
    assert (await client.delete(f"/v1/sessions/{sid}/members/bob", headers=bob)).status_code == 403
    assert (await client.delete(f"/v1/sessions/{sid}/members/bob")).status_code == 200
    assert (await client.get(f"/v1/sessions/{sid}", headers=bob)).status_code == 403
    assert (await client.get(f"/v1/sessions/{sid}/history", headers=bob)).status_code == 403
    assert (
        await client.get(f"/v1/events?session_id={sid}&follow=false", headers=bob)
    ).status_code == 403
    assert "access.revoked" not in (await client.get("/v1/events?follow=false", headers=bob)).text
