import asyncio

from app.store import Store
from tests.conftest import create_thread


async def test_native_mutations_replay_receipts_and_reject_key_reuse(service):
    client, backend, manager = service
    headers = {"Idempotency-Key": "create-once"}
    first = await client.post("/v1/threads", headers=headers, json={"name": "original"})
    second = await client.post("/v1/threads", headers=headers, json={"name": "original"})
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert second.headers["Idempotency-Replayed"] == "true"
    assert len(backend.threads) == 1
    assert (
        await client.post("/v1/threads", headers=headers, json={"name": "changed"})
    ).status_code == 409
    receipt = (await client.get("/v1/operations/create-once")).json()
    assert receipt["status"] == "completed" and receipt["response"] == first.json()
    assert "fingerprint" not in receipt
    assert (
        await client.get("/v1/operations/create-once", headers={"Authorization": ""})
    ).status_code == 401


async def test_unauthenticated_requests_cannot_reserve_a_key(service):
    client, backend, manager = service
    headers = {"Idempotency-Key": "not-reserved", "Authorization": ""}
    assert (await client.post("/v1/threads", json={}, headers=headers)).status_code == 401
    assert await manager.store.operation("not-reserved") is None
    thread = await create_thread(client)
    assert (
        await client.post(
            f"/v1/threads/{thread}/messages",
            headers={"Idempotency-Key": "stream"},
            json={"prompt": "hello", "stream": True},
        )
    ).status_code == 422
    assert await manager.store.operation("stream") is None


async def test_inflight_receipt_survives_restart_as_unknown(tmp_path):
    store = Store(tmp_path)
    await store.open()
    first_id = store.instance_id
    results = await asyncio.gather(*(store.reserve_operation("same", "digest") for _ in range(2)))
    assert sorted(results) == [False, True]
    await store.close()
    await store.open()
    try:
        assert store.instance_id == first_id
        assert (await store.operation("same"))["status"] == "unknown"
        assert not await store.reserve_operation("same", "digest")
    finally:
        await store.close()


async def test_instance_condition_prevents_mutating_replacement_server(service):
    client, backend, manager = service
    response = await client.post(
        "/v1/threads",
        json={},
        headers={
            "Idempotency-Key": "conditional",
            "X-Agent-Instance": "previous-instance",
        },
    )
    assert response.status_code == 409
    assert not backend.threads
    assert await manager.store.operation("conditional") is None
    profile = (await client.get("/v1/capabilities")).json()
    assert profile["instance_id"] == manager.store.instance_id
