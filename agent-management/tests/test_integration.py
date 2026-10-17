"""Hub → HTTP adapter → native server → real SDK → local model fixture."""

import asyncio
import os
import socket
import subprocess
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.adapters.codex import AdapterRegistry
from app.config import Settings
from app.main import create_app
from app.security import Security
from tests.conftest import HEADERS, TOKEN

SOURCE = Path(
    os.environ.get("CODEXSERVER_PATH", str(Path(__file__).parents[2] / "agent-execution"))
)


@pytest.mark.skipif(os.getenv("RUN_CODEX_INTEGRATION") != "1", reason="Opt-in real SDK integration")
async def test_real_native_question_reply_and_completion(database, tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    script = Path(__file__).with_name("native_fixture.py")
    with (tmp_path / "native.log").open("w") as output:
        process = await asyncio.to_thread(
            subprocess.Popen,
            [str(SOURCE / ".venv/bin/python"), str(script), str(SOURCE), str(tmp_path), str(port)],
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            async with httpx.AsyncClient() as probe:
                async with asyncio.timeout(20):
                    while True:
                        if process.poll() is not None:
                            pytest.fail((tmp_path / "native.log").read_text())
                        try:
                            if (
                                await probe.get(f"http://127.0.0.1:{port}/healthz")
                            ).status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        await asyncio.sleep(0.1)
            settings = Settings(
                _env_file=None,
                admin_token=TOKEN,
                secret_key="test-secret-" * 4,
                embedded_worker=True,
                poll_seconds=0.1,
                reconcile_seconds=1,
            )
            app = create_app(settings, database, AdapterRegistry(Security(settings)))
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url="http://test", headers=HEADERS
                ) as client:

                    async def post(path, body=None):
                        response = await client.post(
                            path, json=body, headers={"Idempotency-Key": uuid4().hex}
                        )
                        assert response.is_success, response.text
                        return response.json()

                    async def until(path, predicate):
                        async with asyncio.timeout(20):
                            while True:
                                response = await client.get(path)
                                assert response.is_success, response.text
                                value = response.json()
                                if predicate(value):
                                    return value
                                await asyncio.sleep(0.05)

                    agent = await post(
                        "/v1/agents",
                        {
                            "name": "Native integration",
                            "base_url": f"http://127.0.0.1:{port}",
                            "token": "native-integration-test-token-12345",
                        },
                    )
                    assert agent["connection"] == "online"
                    command = await post(
                        f"/v1/agents/{agent['id']}/sessions", {"name": "Question test"}
                    )
                    sid = command["session_id"]
                    detail_path = f"/v1/sessions/{sid}"
                    await until(detail_path, lambda row: row["provider_thread_id"])
                    await post(
                        detail_path + "/messages",
                        {
                            "prompt": "Ask the local test question",
                            "options": {"collaboration_mode": "plan"},
                        },
                    )
                    pending = await until("/v1/requests", bool)
                    assert pending[0]["method"] == "item/tool/requestUserInput"
                    await post(
                        f"/v1/requests/{pending[0]['id']}/response",
                        {
                            "result": {"answers": {"strategy": {"answers": ["A"]}}},
                        },
                    )
                    detail = await until(
                        detail_path,
                        lambda row: (
                            row["runs"] and row["runs"][-1]["status"] in {"completed", "failed"}
                        ),
                    )
                    assert detail["runs"][-1]["status"] == "completed", detail
                    assert detail["runs"][-1]["final_response"]
                    await until(detail_path, lambda row: row["requests"][0]["status"] == "resolved")
                    events = await client.get(f"/v1/events?session_id={sid}&follow=false")
                    assert "run.finished" in events.text
                    assert "request.pending" in events.text
                    assert "item/agentMessage/delta" in events.text
                    assert (await client.get(detail_path + "/history")).json()["data"]
        finally:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait)
