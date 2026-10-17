import asyncio
from collections import defaultdict
from copy import deepcopy
from uuid import uuid4

import httpx
import pytest

from app.config import Settings
from app.main import create_app

TOKEN = "test-token-with-at-least-24-characters"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


class FakeBackend:
    def __init__(self):
        self.healthy = False
        self.threads = {}
        self.queues = defaultdict(asyncio.Queue)
        self.started = {}
        self.active = 0
        self.peak_active = 0
        self.fail_next = False
        self.steered = []
        self.responses = []
        self.executions = {}

    async def start(self):
        self.healthy = True

    async def close(self):
        self.healthy = False

    async def create(self, cwd):
        thread = {
            "id": uuid4().hex,
            "cwd": cwd,
            "status": {"type": "idle"},
            "name": None,
            "archived": False,
        }
        self.threads[thread["id"]] = thread
        return deepcopy(thread)

    async def read(self, thread_id):
        return deepcopy(self.threads[thread_id])

    async def list(self, **params):
        return {
            "data": [
                deepcopy(t) for t in self.threads.values() if t["archived"] == params["archived"]
            ],
            "nextCursor": None,
        }

    async def history(self, thread_id, cursor, limit):
        return {"data": [], "nextCursor": None}

    async def resume(self, thread_id, execution=None):
        return await self.read(thread_id)

    async def rename(self, thread_id, name):
        self.threads[thread_id]["name"] = name
        return {}

    async def archive(self, thread_id):
        self.threads[thread_id]["archived"] = True
        return {}

    async def start_turn(self, thread_id, prompt, execution=None):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated transport failure")
        turn_id = uuid4().hex
        self.started[turn_id] = (thread_id, prompt)
        self.executions[turn_id] = execution
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        return turn_id

    async def finish(self, turn_id, status="completed"):
        thread_id, prompt = self.started[turn_id]
        if status == "completed":
            await self.queues[turn_id].put(("item/agentMessage/delta", {"delta": prompt}))
            await self.queues[turn_id].put(
                (
                    "item/completed",
                    {
                        "item": {"type": "agentMessage", "text": prompt, "phase": "final_answer"},
                        "threadId": thread_id,
                        "turnId": turn_id,
                    },
                )
            )
        await self.queues[turn_id].put(
            (
                "turn/completed",
                {
                    "turn": {"id": turn_id, "status": status, "error": None},
                    "threadId": thread_id,
                },
            )
        )

    async def events(self, turn_id):
        try:
            while True:
                event = await self.queues[turn_id].get()
                yield event
                if event[0] == "turn/completed":
                    return
        finally:
            self.active -= 1

    async def interrupt(self, thread_id, turn_id):
        await self.finish(turn_id, "interrupted")

    async def steer(self, thread_id, turn_id, prompt):
        self.steered.append((thread_id, turn_id, prompt))
        return {"turnId": turn_id}

    async def respond(self, wire_id, *, result=None, error=None):
        self.responses.append({"id": wire_id, "result": result, "error": error})

    async def ask(self, run, *, wire_id=1, method="item/tool/requestUserInput", **params):
        await self.on_control(
            {
                "id": wire_id,
                "method": method,
                "params": {"threadId": run["thread_id"], "turnId": run["turn_id"], **params},
            }
        )


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        api_token=TOKEN,
        data_dir=tmp_path / "data",
        codex_home=tmp_path / "codex",
        workspace_root=tmp_path / "workspaces",
        max_concurrent_runs=2,
        shutdown_timeout_seconds=1,
    )


@pytest.fixture
async def service(settings):
    backend = FakeBackend()
    app = create_app(settings, backend)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers=HEADERS,
        ) as client:
            yield client, backend, app.state.manager


async def create_thread(client, **body):
    response = await client.post("/v1/threads", json=body)
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def submit(client, thread_id, prompt="hello"):
    response = await client.post(f"/v1/threads/{thread_id}/messages", json={"prompt": prompt})
    assert response.status_code == 202, response.text
    return response.json()["id"]


async def wait_run(client, run_id, predicate=lambda r: r["turn_id"] is not None):
    async with asyncio.timeout(5):
        while True:
            response = await client.get(f"/v1/runs/{run_id}")
            response.raise_for_status()
            run = response.json()
            if predicate(run):
                return run
            await asyncio.sleep(0.005)
