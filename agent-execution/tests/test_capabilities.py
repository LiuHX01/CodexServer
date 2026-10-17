import asyncio
import sqlite3
from unittest.mock import AsyncMock

import pytest

from app.models import AgentRequest, Run
from app.store import Store
from app.streams import event_response
from tests.conftest import create_thread, submit, wait_run
from tests.test_message_stream import sse_events

QUESTION = {"id": "strategy", "header": "选择", "question": "采用哪个方案？"}
ANSWER = {"answers": {"strategy": {"answers": ["A"]}}}


async def pending(client, thread_id):
    response = await client.get(f"/v1/threads/{thread_id}/requests")
    assert response.status_code == 200
    requests = response.json()
    assert len(requests) == 1
    return requests[0]


async def running(client):
    thread = await create_thread(client)
    run_id = await submit(client, thread)
    return await wait_run(client, run_id)


async def test_steering_targets_existing_turn_and_rejects_queued_or_finished(service):
    client, backend, manager = service
    run = await running(client)
    response = await client.post(f"/v1/runs/{run['id']}/input", json={"prompt": "兼容旧接口"})
    assert response.status_code == 200
    assert response.json() == {"turnId": run["turn_id"]}
    assert backend.steered == [(run["thread_id"], run["turn_id"], "兼容旧接口")]
    assert len(backend.started) == 1
    assert (await manager.store.events(run["id"], 0))[-1].event == "run.input_accepted"
    await backend.finish(run["turn_id"])
    await wait_run(client, run["id"], lambda r: r["status"] == "completed")
    assert (
        await client.post(f"/v1/runs/{run['id']}/input", json={"prompt": "too late"})
    ).status_code == 409
    assert (
        await client.post("/v1/runs/missing/input", json={"prompt": "hello"})
    ).status_code == 404
    runs = [await running(client) for _ in range(2)]
    queued = await submit(client, await create_thread(client))
    assert (
        await client.post(f"/v1/runs/{queued}/input", json={"prompt": "too early"})
    ).status_code == 409
    assert len(backend.started) == 1 + len(runs)


async def test_waiting_for_answer_does_not_block_other_runs_or_controls(service):
    client, backend, _ = service
    first = await running(client)
    await backend.ask(first, questions=[QUESTION])
    request = await pending(client, first["thread_id"])
    second = await running(client)
    await backend.finish(second["turn_id"])
    await wait_run(client, second["id"], lambda r: r["status"] == "completed")
    assert (
        await client.post(f"/v1/runs/{first['id']}/input", json={"prompt": "更多上下文"})
    ).status_code == 200
    reply = await client.post(f"/v1/requests/{request['id']}/response", json={"result": ANSWER})
    assert reply.status_code == 200
    assert reply.json()["status"] == "responded"
    assert backend.responses[-1] == {"id": 1, "result": ANSWER, "error": None}
    await backend.on_control(
        {
            "method": "serverRequest/resolved",
            "params": {"threadId": first["thread_id"], "requestId": 1},
        }
    )
    records = (
        await client.get(f"/v1/threads/{first['thread_id']}/requests?pending_only=false")
    ).json()
    assert records[0]["status"] == "resolved"


@pytest.mark.parametrize(
    "method,result",
    [
        ("item/tool/requestUserInput", ANSWER),
        ("item/commandExecution/requestApproval", {"decision": "decline"}),
        ("item/fileChange/requestApproval", {"decision": "accept"}),
        ("item/permissions/requestApproval", {"permissions": {}, "scope": "turn"}),
        ("mcpServer/elicitation/request", {"action": "decline", "content": None}),
    ],
)
async def test_typed_responses_are_validated_and_only_sent_once(service, method, result):
    client, backend, _ = service
    run = await running(client)
    await backend.ask(run, method=method, questions=[QUESTION])
    request = await pending(client, run["thread_id"])
    path = f"/v1/requests/{request['id']}/response"
    assert (await client.post(path, json={"result": {"wrong": True}})).status_code == 422
    assert not backend.responses
    replies = await asyncio.gather(*(client.post(path, json={"result": result}) for _ in range(2)))
    assert sorted(reply.status_code for reply in replies) == [200, 409]
    assert len(backend.responses) == 1


async def test_invalid_question_and_unavailable_approval_decision_are_not_forwarded(service):
    client, backend, _ = service
    run = await running(client)
    await backend.ask(run, questions=[QUESTION])
    request = await pending(client, run["thread_id"])
    assert (
        await client.post(
            f"/v1/requests/{request['id']}/response",
            json={"result": {"answers": {"unknown": {"answers": ["A"]}}}},
        )
    ).status_code == 422
    await backend.ask(
        run,
        wire_id=2,
        method="item/commandExecution/requestApproval",
        availableDecisions=["decline", "cancel"],
    )
    requests = (await client.get(f"/v1/threads/{run['thread_id']}/requests")).json()
    assert (
        await client.post(
            f"/v1/requests/{requests[-1]['id']}/response", json={"result": {"decision": "accept"}}
        )
    ).status_code == 422
    assert not backend.responses


@pytest.mark.parametrize("ending", ["cancel", "complete", "resolve", "disconnect"])
async def test_stale_requests_cannot_be_answered(service, ending):
    client, backend, manager = service
    run = await running(client)
    await backend.ask(run, questions=[QUESTION])
    request = await pending(client, run["thread_id"])
    if ending == "cancel":
        await client.post(f"/v1/runs/{run['id']}/cancel")
    elif ending == "complete":
        await backend.finish(run["turn_id"])
        await wait_run(client, run["id"], lambda r: r["status"] == "completed")
    elif ending == "resolve":
        await backend.on_control(
            {
                "method": "serverRequest/resolved",
                "params": {"threadId": run["thread_id"], "requestId": 1},
            }
        )
    else:
        await manager.disconnected()
    assert (await client.get(f"/v1/threads/{run['thread_id']}/requests")).json() == []
    assert (
        await client.post(f"/v1/requests/{request['id']}/response", json={"result": ANSWER})
    ).status_code == 409
    assert not backend.responses


async def test_thread_events_span_turns_preserve_metadata_and_replay_without_leaking(service):
    client, backend, _ = service
    first = await running(client)
    thread = first["thread_id"]
    other = await running(client)
    await backend.on_notification(
        "thread/status/changed", {"threadId": thread, "status": {"type": "active"}}
    )
    await backend.ask(first, questions=[QUESTION])
    await backend.finish(first["turn_id"])
    await wait_run(client, first["id"], lambda r: r["status"] == "completed")
    next_id = await submit(client, thread, "next")
    second = await wait_run(client, next_id)
    await backend.finish(second["turn_id"])
    await wait_run(client, next_id, lambda r: r["status"] == "completed")
    path = f"/v1/threads/{thread}/events?follow=false"
    events = sse_events((await client.get(path)).text)
    assert all(data["thread_id"] == thread and data["version"] == 1 for _, data in events)
    assert {data["run_id"] for _, data in events} == {None, first["id"], next_id}
    assert other["id"] not in str(events)
    assert sum(name == "run.finished" for name, _ in events) == 2
    assert any(name == "thread/status/changed" for name, _ in events)
    assert any(name == "request.pending" for name, _ in events)
    cursor = events[-2][1]["id"]
    replay = sse_events((await client.get(path, headers={"Last-Event-ID": str(cursor)})).text)
    assert replay == events[-1:]
    assert (await client.get(path, headers={"Last-Event-ID": "-1"})).status_code == 400


async def test_execution_options_are_per_run_and_bounded_by_deployment(service):
    client, backend, manager = service
    manager.settings.allowed_models = ["alternate-model"]
    thread = await create_thread(client)
    options = {
        "model": "alternate-model",
        "reasoning_effort": "high",
        "approval_policy": "on-request",
        "collaboration_mode": "default",
    }
    response = await client.post(
        f"/v1/threads/{thread}/messages", json={"prompt": "hello", "options": options}
    )
    run = await wait_run(client, response.json()["id"])
    assert backend.executions[run["turn_id"]].model_dump() == options
    await backend.finish(run["turn_id"])
    await wait_run(client, run["id"], lambda r: r["status"] == "completed")
    default = await wait_run(client, await submit(client, thread))
    assert default["options"]["model"] == manager.settings.codex_model
    assert default["options"]["approval_policy"] == "never"
    another = await create_thread(client)
    for overrides in [{"model": "forbidden"}, {"approval_policy": "untrusted"}]:
        manager.settings.allowed_approval_policies = ["never"]
        assert (
            await client.post(
                f"/v1/threads/{another}/messages", json={"prompt": "hello", "options": overrides}
            )
        ).status_code == 403


async def test_new_endpoints_require_service_auth(service):
    client, _, _ = service
    for method, path, body in [
        ("POST", "/v1/runs/r/input", {"prompt": "hi"}),
        ("POST", "/v1/requests/r/response", {"result": {}}),
        ("GET", "/v1/threads/t/events", None),
        ("GET", "/v1/threads/t/requests", None),
    ]:
        assert (
            await client.request(method, path, json=body, headers={"Authorization": ""})
        ).status_code == 401


async def test_thread_subscription_remains_open_across_runs(service):
    client, backend, manager = service
    thread = await create_thread(client)
    request = AsyncMock()
    request.is_disconnected.return_value = False
    response_request = event_response(request, manager, thread_id=thread).body_iterator
    try:
        for prompt in ("first", "second"):
            run = await wait_run(client, await submit(client, thread, prompt))
            await backend.finish(run["turn_id"])
            async with asyncio.timeout(5):
                while True:
                    event, data = sse_events(await anext(response_request))[0]
                    assert data["thread_id"] == thread
                    if event == "run.finished":
                        assert data["data"]["final_response"] == prompt
                        break
            await wait_run(client, run["id"], lambda r: r["status"] == "completed")
    finally:
        await response_request.aclose()


async def test_disconnected_responder_still_finishes_exactly_one_reply(service):
    client, backend, manager = service
    run = await running(client)
    await backend.ask(run, questions=[QUESTION])
    request = await pending(client, run["thread_id"])
    entered, release = asyncio.Event(), asyncio.Event()
    original_respond = backend.respond

    async def slow_respond(*args, **kwargs):
        entered.set()
        await release.wait()
        await original_respond(*args, **kwargs)

    backend.respond = slow_respond
    reply = asyncio.create_task(manager.interactions.respond(request["id"], ANSWER))
    await asyncio.wait_for(entered.wait(), 5)
    reply.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reply
    release.set()
    async with asyncio.timeout(5):
        while True:
            manager.store.changed.clear()
            if (await manager.store.get_request(request["id"])).status != "pending":
                break
            await manager.store.changed.wait()
    assert len(backend.responses) == 1


async def test_unknown_request_is_rejected_and_late_request_is_expired(service):
    client, backend, manager = service
    run = await running(client)
    await backend.ask(run, method="unsupported/method")
    assert backend.responses[-1]["error"]["code"] == -32601
    await backend.finish(run["turn_id"])
    await wait_run(client, run["id"], lambda r: r["status"] == "completed")
    await backend.ask(run, questions=[QUESTION])
    assert await manager.store.requests(run["thread_id"]) == []
    records = await manager.store.requests(run["thread_id"], pending_only=False)
    assert records[-1].status == "expired"
    assert backend.responses[-1]["error"]["code"] == -32600


async def test_legacy_database_migration_preserves_ids_and_expires_requests(tmp_path):
    with sqlite3.connect(tmp_path / "server.sqlite3") as db:
        db.executescript("""
            CREATE TABLE runs (id TEXT PRIMARY KEY, thread_id TEXT NOT NULL,
                status TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                event TEXT NOT NULL, data TEXT NOT NULL);
            CREATE INDEX events_run ON events(run_id, id);
        """)
        run = Run(id="r", thread_id="t", turn_id="turn", status="completed")
        db.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?)",
            (run.id, run.thread_id, run.status, run.model_dump_json(), run.created_at),
        )
        db.execute("INSERT INTO events VALUES (42, 'r', 'run.finished', '{}')")
    store = Store(tmp_path)
    await store.open()
    request = AgentRequest(id="old", method="item/tool/requestUserInput", thread_id="t", params={})
    await store.save_request(request, "request.pending")
    await store.close()
    await store.open()
    try:
        assert (await store.events("r", 0))[0].id == 42
        event = (await store.events(None, 0, thread_id="t"))[0]
        assert (event.thread_id, event.turn_id) == ("t", "turn")
        assert (await store.get_request("old")).status == "expired"
        assert await store.requests("t") == []
        assert (await store.events(None, 42, thread_id="t"))[-1].event == "request.expired"
    finally:
        await store.close()
