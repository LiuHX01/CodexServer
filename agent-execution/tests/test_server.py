import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.models import Run
from app.store import Store
from tests.conftest import create_thread, submit, wait_run


async def test_auth_and_openapi(service):
    client, _, _ = service
    assert (await client.get("/healthz", headers={"Authorization": ""})).status_code == 200
    assert (await client.get("/v1/threads", headers={"Authorization": ""})).status_code == 401
    assert (
        await client.get("/v1/status", headers={"Authorization": "Bearer wrong"})
    ).status_code == 401
    assert (await client.get("/v1/status")).status_code == 200
    schema = (await client.get("/openapi.json")).json()
    assert "HTTPBearer" in schema["components"]["securitySchemes"]
    assert "m" not in str(schema["paths"]["/v1/threads"]["post"].get("parameters", []))


async def test_parallel_runs_do_not_mix_events_and_respect_capacity(service):
    client, backend, _ = service
    threads = await asyncio.gather(*(create_thread(client) for _ in range(3)))
    ids = await asyncio.gather(*(submit(client, t, f"message-{i}") for i, t in enumerate(threads)))
    async with asyncio.timeout(5):
        while True:
            current = [
                response.json()
                for response in await asyncio.gather(
                    *(client.get(f"/v1/runs/{run_id}") for run_id in ids)
                )
            ]
            running = [r for r in current if r["turn_id"] is not None]
            if len(running) == 2:
                break
            await asyncio.sleep(0.005)
    first, second = running
    queued = next(r for r in current if r["status"] == "queued")
    assert backend.active == 2
    assert (
        await client.post(f"/v1/threads/{threads[0]}/messages", json={"prompt": "collision"})
    ).status_code == 409
    assert (await client.post(f"/v1/threads/{threads[0]}/archive")).status_code == 409
    await backend.finish(first["turn_id"])
    third = await wait_run(client, queued["id"])
    await asyncio.gather(backend.finish(second["turn_id"]), backend.finish(third["turn_id"]))
    results = await asyncio.gather(
        *(wait_run(client, r, lambda x: x["status"] == "completed") for r in ids)
    )
    assert [r["final_response"] for r in results] == ["message-0", "message-1", "message-2"]
    assert backend.peak_active == 2
    for i, run_id in enumerate(ids):
        stream = (await client.get(f"/v1/runs/{run_id}/events")).text
        assert f"message-{i}" in stream
        assert all(f"message-{j}" not in stream for j in range(3) if j != i)
        assert "event: run.finished" in stream


async def test_cancel_running_and_queued_and_release_thread(service):
    client, backend, _ = service
    threads = [await create_thread(client) for _ in range(3)]
    ids = [await submit(client, thread_id) for thread_id in threads]
    await wait_run(client, ids[0])
    await wait_run(client, ids[1])
    cancelled = await client.post(f"/v1/runs/{ids[2]}/cancel")
    assert cancelled.json()["status"] == "interrupted"
    assert len(backend.started) == 2
    await client.post(f"/v1/runs/{ids[0]}/cancel")
    await wait_run(client, ids[0], lambda r: r["status"] == "interrupted")
    new_id = await submit(client, threads[0], "next turn")
    new_run = await wait_run(client, new_id)
    await backend.finish(new_run["turn_id"])
    assert (await wait_run(client, new_id, lambda r: r["status"] == "completed"))[
        "final_response"
    ] == "next turn"


async def test_management_and_workspace_isolation(service, settings):
    client, backend, _ = service
    a, b = await create_thread(client, name="A"), await create_thread(client)
    assert backend.threads[a]["cwd"] != backend.threads[b]["cwd"]
    assert Path(backend.threads[a]["cwd"]).is_relative_to(settings.workspace_root)
    assert (await client.post(f"/v1/threads/{a}/resume")).status_code == 200
    assert (await client.get(f"/v1/threads/{a}/history")).status_code == 200
    assert (await client.post(f"/v1/threads/{a}/archive")).status_code == 200
    assert a not in [x["id"] for x in (await client.get("/v1/threads")).json()["data"]]
    archived = (await client.get("/v1/threads?archived=true")).json()["data"]
    assert a in [x["id"] for x in archived]
    assert (await client.post("/v1/threads", json={"cwd": "../.."})).status_code == 422
    assert (
        await client.post("/v1/threads", json={"sandbox": "danger-full-access"})
    ).status_code == 422
    backend.threads[a]["cwd"] = str(settings.workspace_root.parent)
    assert (
        await client.post(f"/v1/threads/{a}/messages", json={"prompt": "hello"})
    ).status_code == 403


async def test_failure_timeout_and_sse_replay(service):
    client, backend, manager = service
    thread_id = await create_thread(client)
    backend.fail_next = True
    bad_id = await submit(client, thread_id)
    assert (await wait_run(client, bad_id, lambda r: r["status"] == "failed"))["error"]
    manager.settings.run_timeout_seconds = 0.05
    timeout_id = await submit(client, thread_id)
    timed_out = await wait_run(client, timeout_id, lambda r: r["status"] == "failed")
    assert "RUN_TIMEOUT_SECONDS" in timed_out["error"]
    events = await manager.store.events(timeout_id, 0)
    last = events[-1].id
    stream = await client.get(
        f"/v1/runs/{timeout_id}/events", headers={"Last-Event-ID": str(last - 1)}
    )
    assert "event: run.finished" in stream.text
    assert "event: run.running" not in stream.text
    assert (await client.get(f"/v1/runs/{timeout_id}/events?after={last}")).text == ""
    assert (
        await client.get(f"/v1/runs/{timeout_id}/events", headers={"Last-Event-ID": "invalid"})
    ).status_code == 400
    assert (await client.get("/v1/runs/missing")).status_code == 404


async def test_queue_limit_and_validation(service):
    client, _, manager = service
    manager.settings.max_pending_runs = 1
    a, b = await create_thread(client), await create_thread(client)
    await submit(client, a)
    assert (await client.post(f"/v1/threads/{b}/messages", json={"prompt": "x"})).status_code == 429
    assert (
        await client.post(f"/v1/threads/{b}/messages", json={"prompt": "  "})
    ).status_code == 422
    assert (await client.get("/v1/threads?limit=0")).status_code == 422


async def test_simplified_api_surface(service):
    client, _, _ = service
    schema = (await client.get("/openapi.json")).json()
    assert sum(len(methods) for methods in schema["paths"].values()) == 17
    assert set(schema["components"]["schemas"]["CreateThread"]["properties"]) == {"name", "cwd"}
    assert set(schema["components"]["schemas"]["Message"]["properties"]) == {
        "prompt",
        "stream",
        "options",
    }
    assert "requestBody" not in schema["paths"]["/v1/threads/{thread_id}/resume"]["post"]
    assert "/v1/models" not in schema["paths"]
    assert "/v1/runs" not in schema["paths"]
    assert "/v1/threads/{thread_id}/fork" not in schema["paths"]


async def test_simultaneous_admission_respects_queue_limit(service):
    client, _, manager = service
    manager.settings.max_pending_runs = 1
    threads = [await create_thread(client) for _ in range(8)]
    results = await asyncio.gather(
        *(
            client.post(f"/v1/threads/{thread_id}/messages", json={"prompt": "hello"})
            for thread_id in threads
        )
    )
    assert [r.status_code for r in results].count(202) == 1
    assert [r.status_code for r in results].count(429) == 7


async def test_restart_recovers_runs_and_preserves_events(tmp_path):
    store = Store(tmp_path)
    await store.open()
    try:
        await store.save(Run(id="r", thread_id="t", status="running"), "run.running", {})
        with pytest.raises(RuntimeError, match="already in use"):
            await Store(tmp_path).open()
    finally:
        await store.close()
    recovered = Store(tmp_path)
    await recovered.open()
    try:
        assert (await recovered.get("r")).status == "interrupted"
        assert [event.event for event in await recovered.events("r", 0)] == [
            "run.running",
            "run.finished",
        ]
    finally:
        await recovered.close()


def test_example_token_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, api_token="replace-this-with-a-random-secret")


async def test_default_workspace_and_explicit_override(service, settings):
    client, backend, _ = service
    default = settings.workspace_root / "default-project"
    override = settings.workspace_root / "other-project"
    default.mkdir(parents=True)
    override.mkdir()
    settings.default_cwd = "default-project"
    thread_id = await create_thread(client)
    assert backend.threads[thread_id]["cwd"] == str(default.resolve())
    thread_id = await create_thread(client, cwd=str(override))
    assert backend.threads[thread_id]["cwd"] == str(override.resolve())
    settings.default_cwd = "missing-project"
    assert (await client.post("/v1/threads", json={})).status_code == 422
    settings.default_cwd = str(settings.workspace_root.parent)
    assert (await client.post("/v1/threads", json={})).status_code == 422
