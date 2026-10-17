"""Opt-in real SDK/runtime check; never starts an inference turn or uses an API key."""

import asyncio
import json
import os
import threading
from pathlib import Path

import httpx
import pytest

from app.main import create_app
from tests.conftest import HEADERS, create_thread, submit, wait_run
from tests.mock_provider import REPLY, provider


def configure_local_provider(settings, base_url):
    settings.codex_home.mkdir(parents=True, exist_ok=True)
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


@pytest.mark.skipif(os.getenv("RUN_CODEX_SMOKE") != "1", reason="Set RUN_CODEX_SMOKE=1")
@pytest.mark.parametrize("interaction", ["question", "approval"])
async def test_real_runtime_interaction_and_parallel_execution(settings, interaction):
    settings.codex_model = "mock-codex"
    settings.run_timeout_seconds = 15
    calls = []

    def question_once(request, final_item):
        calls.append(request)
        if len(calls) > 1:
            return final_item
        item = {
            "id": "fc_question",
            "type": "function_call",
            "call_id": "question-call",
            "name": "request_user_input",
            "status": "completed",
            "arguments": json.dumps(
                {
                    "questions": [
                        {
                            "id": "strategy",
                            "header": "Choose",
                            "question": "Which strategy?",
                            "options": [
                                {"label": "A", "description": "First"},
                                {"label": "B", "description": "Second"},
                            ],
                        }
                    ]
                }
            ),
        }
        if interaction == "approval":
            item.update(
                name="exec_command", arguments=json.dumps({"cmd": "touch approval-probe.txt"})
            )
        return item

    with provider(response_factory=question_once) as base_url:
        configure_local_provider(settings, base_url)
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test", headers=HEADERS
            ) as client:
                thread = await create_thread(client)
                started = await client.post(
                    f"/v1/threads/{thread}/messages",
                    json={
                        "prompt": "Run the local probe",
                        "options": {
                            "collaboration_mode": "plan"
                            if interaction == "question"
                            else "default",
                            "approval_policy": "untrusted",
                        },
                    },
                )
                assert started.status_code == 202, started.text
                run_id = started.json()["id"]
                async with asyncio.timeout(8):
                    while True:
                        requests = (await client.get(f"/v1/threads/{thread}/requests")).json()
                        if requests:
                            break
                        run = (await client.get(f"/v1/runs/{run_id}")).json()
                        assert run["status"] not in {"completed", "failed"}, run
                        await asyncio.sleep(0.01)
                expected = (
                    "item/tool/requestUserInput"
                    if interaction == "question"
                    else "item/commandExecution/requestApproval"
                )
                assert requests[0]["method"] == expected
                # A real runtime must keep servicing other threads while this request waits.
                other = await create_thread(client)
                other_id = await submit(client, other, "parallel probe")
                completed = await wait_run(client, other_id, lambda r: r["status"] == "completed")
                assert completed["final_response"] == REPLY
                result = (
                    {"answers": {"strategy": {"answers": ["A"]}}}
                    if interaction == "question"
                    else {"decision": "accept"}
                )
                response = await client.post(
                    f"/v1/requests/{requests[0]['id']}/response",
                    json={"result": result},
                )
                assert response.status_code == 200, (
                    response.text,
                    requests[0]["params"].get("availableDecisions"),
                )
                run = await wait_run(
                    client, run_id, lambda r: r["status"] in {"completed", "failed"}
                )
                assert run["status"] == "completed", run
                assert run["final_response"] == REPLY
                if interaction == "question":
                    assert "A" in json.dumps(calls[-1]["input"])
                thread_data = (await client.get(f"/v1/threads/{thread}")).json()
                assert (Path(thread_data["cwd"]) / "approval-probe.txt").exists() == (
                    interaction == "approval"
                )
                records = (
                    await client.get(f"/v1/threads/{thread}/requests?pending_only=false")
                ).json()
                assert records[0]["status"] == "resolved"


@pytest.mark.skipif(os.getenv("RUN_CODEX_SMOKE") != "1", reason="Set RUN_CODEX_SMOKE=1")
async def test_real_runtime_steering(settings):
    settings.codex_model = "mock-codex"
    entered, release = threading.Event(), threading.Event()
    calls = []

    def held_response(request, final_item):
        calls.append(request)
        if len(calls) == 1:
            entered.set()
            release.wait(timeout=5)
        return final_item

    with provider(response_factory=held_response) as base_url:
        configure_local_provider(settings, base_url)
        app = create_app(settings)
        try:
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test", headers=HEADERS
                ) as client:
                    thread = await create_thread(client)
                    run_id = await submit(client, thread)
                    assert await asyncio.to_thread(entered.wait, 5)
                    response = await client.post(
                        f"/v1/runs/{run_id}/input", json={"prompt": "steering probe"}
                    )
                    assert response.status_code == 200, response.text
                    release.set()
                    run = await wait_run(
                        client, run_id, lambda r: r["status"] in {"completed", "failed"}
                    )
                    assert run["status"] == "completed", run
                    history = (await client.get(f"/v1/threads/{thread}/history")).json()
                    assert "steering probe" in json.dumps(history)
        finally:
            release.set()


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


@pytest.mark.skipif(os.getenv("RUN_CODEX_SMOKE") != "1", reason="Set RUN_CODEX_SMOKE=1")
async def test_real_sdk_ccswitch_provider_and_isolated_home(settings, tmp_path):
    settings.codex_auth_mode = "ccswitch"
    settings.codex_provider_config_dir = tmp_path / "ccswitch-source"
    settings.codex_provider_config_dir.mkdir()
    requests = []

    def capture(request, item):
        requests.append(request)
        return item

    with provider(
        response_factory=capture, expected_authorization="Bearer test-provider-key"
    ) as base_url:
        (settings.codex_provider_config_dir / "config.toml").write_text(
            'model="mock-codex"\nmodel_provider="gateway"\n'
            '[model_providers.gateway]\nname="Fixture"\n'
            f'base_url="{base_url}"\nwire_api="responses"\n'
            'experimental_bearer_token="test-provider-key"\n'
            "requires_openai_auth=false\nsupports_websockets=false\n"
        )
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test", headers=HEADERS
            ) as client:
                thread = await create_thread(client)
                run_id = await submit(client, thread, "Reply with the fixture message")
                result = await wait_run(
                    client, run_id, lambda row: row["status"] in {"completed", "failed"}
                )
                assert result["status"] == "completed", result
                assert result["final_response"] == REPLY
    assert requests and requests[0]["model"] == "mock-codex"
    assert not (settings.codex_home / "auth.json").exists()
