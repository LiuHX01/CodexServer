"""Opt-in real SDK/runtime check; never starts an inference turn or uses an API key."""

import json
import os

import httpx
import pytest

from app.main import create_app
from tests.conftest import HEADERS, create_thread, submit, wait_run
from tests.mock_provider import REPLY, provider


@pytest.mark.skipif(os.getenv("RUN_CODEX_SMOKE") != "1", reason="Set RUN_CODEX_SMOKE=1")
async def test_real_sdk_thread_lifecycle(settings):
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers=HEADERS,
        ) as client:
            created = await client.post("/v1/threads", json={})
            assert created.status_code == 201, created.text
            thread_id = created.json()["id"]
            listing = await client.get("/v1/threads")
            assert listing.status_code == 200
            assert thread_id in [t["id"] for t in listing.json()["drafts"]]
            history = await client.get(f"/v1/threads/{thread_id}/history")
            assert history.status_code == 200, history.text
            resumed = await client.post(f"/v1/threads/{thread_id}/resume")
            assert resumed.status_code == 200, resumed.text
            assert (await client.post(f"/v1/threads/{thread_id}/archive")).status_code == 409


@pytest.mark.skipif(os.getenv("RUN_CODEX_SMOKE") != "1", reason="Set RUN_CODEX_SMOKE=1")
async def test_real_sdk_turn_persistence_and_resume_with_local_provider(settings):
    settings.codex_model = "mock-codex"
    settings.run_timeout_seconds = 15
    settings.codex_home.mkdir(parents=True)
    with provider() as base_url:
        # This is an isolated temporary CODEX_HOME and an unauthenticated loopback fixture.
        (settings.codex_home / "config.toml").write_text(
            'model_provider = "local_test"\n'
            "[model_providers.local_test]\n"
            'name = "Local test provider"\n'
            f'base_url = "{base_url}"\n'
            'wire_api = "responses"\n'
            "requires_openai_auth = false\n"
            "supports_websockets = false\n",
            encoding="utf-8",
        )
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers=HEADERS,
            ) as client:
                thread_id = await create_thread(client)
                run_id = await submit(client, thread_id, "Reply with a short greeting")
                run = await wait_run(
                    client, run_id, lambda r: r["status"] in {"completed", "failed"}
                )
                assert run["status"] == "completed", run
                assert run["final_response"] == REPLY
                events = (await client.get(f"/v1/runs/{run_id}/events")).text
                assert "event: item/agentMessage/delta" in events
                history = await client.get(f"/v1/threads/{thread_id}/history")
                assert history.status_code == 200, history.text
                assert history.json()["data"], history.text
                listing = (await client.get("/v1/threads")).json()
                assert thread_id in [t["id"] for t in listing["data"]], listing
                assert thread_id not in [t["id"] for t in listing["drafts"]]

        # A new SDK/runtime process must be able to resume the saved conversation.
        restarted = create_app(settings)
        async with restarted.router.lifespan_context(restarted):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=restarted),
                base_url="http://test",
                headers=HEADERS,
            ) as client:
                result = await client.post(f"/v1/threads/{thread_id}/resume")
                assert result.status_code == 200, result.text
                response = await client.post(
                    f"/v1/threads/{thread_id}/messages",
                    json={"prompt": "Continue the conversation", "stream": True},
                )
                assert response.status_code == 200, response.text
                events = [
                    json.loads(line[6:])
                    for line in response.text.splitlines()
                    if line.startswith("data: ")
                ]
                assert events[-1]["status"] == "completed", events[-1]
                assert events[-1]["final_response"] == REPLY
                assert (await client.post(f"/v1/threads/{thread_id}/archive")).status_code == 200
                listing = (await client.get("/v1/threads?archived=true")).json()
                assert thread_id in [t["id"] for t in listing["data"]]
