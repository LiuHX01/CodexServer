import asyncio
import json
import socket

import httpx
import pytest
import uvicorn

from tests.conftest import create_thread, wait_run


def sse_events(text):
    events = []
    for block in text.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in fields:
            events.append((fields["event"], json.loads(fields["data"])))
    return events


async def started(manager, thread_id):
    async with asyncio.timeout(5):
        while True:
            for live in list(manager.live.values()):
                if live.run.thread_id == thread_id and live.run.turn_id:
                    return live.run.model_copy()
            await asyncio.sleep(0.005)


async def test_merged_stream_returns_events_and_persisted_result(service):
    client, backend, manager = service
    thread = await create_thread(client)
    request = asyncio.create_task(
        client.post(
            f"/v1/threads/{thread}/messages", json={"prompt": "streamed answer", "stream": True}
        )
    )
    run = await started(manager, thread)
    assert not request.done()
    await backend.finish(run.turn_id)
    response = await asyncio.wait_for(request, 5)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-run-id"] == run.id
    events = sse_events(response.text)
    assert events[0][0] == "run.queued"
    assert events[0][1]["thread_id"] == thread
    assert events[-1][0] == "run.finished"
    assert events[-1][1]["status"] == "completed"
    assert events[-1][1]["final_response"] == "streamed answer"
    assert any(event == "item/agentMessage/delta" for event, _ in events)
    assert (await client.get(f"/v1/runs/{run.id}")).json()["status"] == "completed"


async def test_explicit_false_keeps_async_json_response(service):
    client, backend, manager = service
    thread = await create_thread(client)
    response = await client.post(
        f"/v1/threads/{thread}/messages", json={"prompt": "async answer", "stream": False}
    )
    assert response.status_code == 202
    assert response.headers["content-type"].startswith("application/json")
    run = await started(manager, thread)
    await backend.finish(run.turn_id)
    await wait_run(client, run.id, lambda r: r["status"] == "completed")


async def test_merged_stream_reports_model_failure_in_terminal_event(service):
    client, backend, _ = service
    thread = await create_thread(client)
    backend.fail_next = True
    response = await asyncio.wait_for(
        client.post(f"/v1/threads/{thread}/messages", json={"prompt": "hello", "stream": True}), 5
    )
    assert response.status_code == 200
    events = sse_events(response.text)
    assert events[-1][0] == "run.finished"
    assert events[-1][1]["status"] == "failed"
    assert events[-1][1]["error"]


async def test_merged_stream_can_be_cancelled_and_rejects_conflicting_message(service):
    client, _, manager = service
    thread = await create_thread(client)
    request = asyncio.create_task(
        client.post(f"/v1/threads/{thread}/messages", json={"prompt": "wait", "stream": True})
    )
    run = await started(manager, thread)
    conflict = await client.post(
        f"/v1/threads/{thread}/messages", json={"prompt": "conflict", "stream": True}
    )
    assert conflict.status_code == 409
    await client.post(f"/v1/runs/{run.id}/cancel")
    response = await asyncio.wait_for(request, 5)
    assert sse_events(response.text)[-1][1]["status"] == "interrupted"


async def test_parallel_merged_streams_are_isolated(service):
    client, backend, manager = service
    threads = [await create_thread(client) for _ in range(2)]
    requests = [
        asyncio.create_task(
            client.post(f"/v1/threads/{thread}/messages", json={"prompt": text, "stream": True})
        )
        for thread, text in zip(threads, ["alpha-answer", "beta-answer"], strict=True)
    ]
    runs = await asyncio.gather(*(started(manager, thread) for thread in threads))
    assert backend.active == 2
    await asyncio.gather(*(backend.finish(run.turn_id) for run in runs))
    responses = await asyncio.wait_for(asyncio.gather(*requests), 5)
    for response, expected, other in zip(
        responses, ["alpha-answer", "beta-answer"], ["beta-answer", "alpha-answer"], strict=True
    ):
        assert expected in response.text
        assert other not in response.text


@pytest.mark.parametrize("invalid", ["true", "false", 1, None])
async def test_stream_requires_json_boolean(service, invalid):
    client, backend, _ = service
    thread = await create_thread(client)
    response = await client.post(
        f"/v1/threads/{thread}/messages", json={"prompt": "hello", "stream": invalid}
    )
    assert response.status_code == 422
    assert not backend.started


async def test_docs_describe_both_message_response_modes(service):
    client, _, _ = service
    schema = (await client.get("/openapi.json")).json()
    responses = schema["paths"]["/v1/threads/{thread_id}/messages"]["post"]["responses"]
    assert "text/event-stream" in responses["200"]["content"]
    assert "X-Run-ID" in responses["200"]["headers"]
    assert "application/json" in responses["202"]["content"]


async def test_real_http_flushes_before_completion_and_disconnect_keeps_run_alive(service):
    client, backend, manager = service
    thread = await create_thread(client)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        ready = asyncio.Event()

        class ReadyServer(uvicorn.Server):
            async def startup(self, sockets=None):
                await super().startup(sockets=sockets)
                ready.set()

        server = ReadyServer(
            uvicorn.Config(
                client._transport.app,
                lifespan="off",
                log_level="error",
                timeout_graceful_shutdown=1,
            )
        )
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            await asyncio.wait_for(ready.wait(), 5)
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}",
                headers=client.headers,
                timeout=5,
            ) as network:
                async with network.stream(
                    "POST",
                    f"/v1/threads/{thread}/messages",
                    json={"prompt": "final answer", "stream": True},
                ) as response:
                    assert response.status_code == 200
                    run_id = response.headers["x-run-id"]
                    run = await started(manager, thread)
                    assert run.id == run_id
                    await backend.queues[run.turn_id].put(
                        (
                            "item/agentMessage/delta",
                            {"delta": "early chunk"},
                        )
                    )
                    # Must receive bytes while the backend is still waiting, before completion.
                    partial = ""
                    async with asyncio.timeout(5):
                        async for line in response.aiter_lines():
                            partial += line + "\n"
                            if line.startswith("data: ") and "early chunk" in line:
                                break
                    assert "early chunk" in partial
                    assert "run.finished" not in partial
                    last_id = int(
                        [line[4:] for line in partial.splitlines() if line.startswith("id: ")][-1]
                    )
                # Closing the streaming connection must not cancel the underlying turn.
                assert (await manager.get(run_id)).status == "running"
                await backend.finish(run.turn_id)
                await wait_run(client, run_id, lambda r: r["status"] == "completed")
                replay = await network.get(
                    f"/v1/runs/{run_id}/events",
                    headers={"Last-Event-ID": str(last_id)},
                )
                assert "early chunk" not in replay.text
                assert sse_events(replay.text)[-1][1]["final_response"] == "final answer"
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, 5)
